"""Рабочее подключение ingest, committed публикаций и расследования."""

import asyncio
import json
from contextlib import suppress
from typing import Any
from uuid import UUID

import anyio
from fastapi import APIRouter, FastAPI, Request
from jsonschema.exceptions import ValidationError  # type: ignore[import-untyped]
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from honeyforge.api.dependencies import Agent, Database
from honeyforge.api.investigation import create_router as investigation_router
from honeyforge.api.investigation_schemas import AgentAck
from honeyforge.api.schemas import ErrorResponse
from honeyforge.common import DomainError
from honeyforge.config import Settings
from honeyforge.investigation.projector import InvestigationProjector
from honeyforge.investigation.service import InvestigationService
from honeyforge.investigation.tables import InvestigationTables
from honeyforge.telemetry.cursors import CursorCodec
from honeyforge.telemetry.history import CatchUpService, HistoryService
from honeyforge.telemetry.ingress import IngressService
from honeyforge.telemetry.masking import PublicPolicy
from honeyforge.telemetry.presentation import PublicEventPresenter
from honeyforge.telemetry.publisher import OutboxPublisher
from honeyforge.telemetry.read_adapter import CommittedReadAdapter
from honeyforge.telemetry.reader import PublicationReader
from honeyforge.telemetry.storage import EventStorage
from honeyforge.telemetry.stream import LiveStreamService
from honeyforge.telemetry.tables import TelemetryTables
from honeyforge.telemetry.transport import SessionAccess, create_router


class TelemetryRuntime:
    """Poll outbox восстанавливается после рестарта; несколько workers сериализует БД."""

    def __init__(self, engine: Engine, settings: Settings) -> None:
        self.tables = TelemetryTables()
        self.investigation = InvestigationTables(self.tables)
        self.publisher = OutboxPublisher(
            engine, self.tables, InvestigationProjector(self.tables, self.investigation)
        )
        self.ingress = IngressService(EventStorage(engine, self.tables))
        presenter = PublicEventPresenter(PublicPolicy())
        adapter = CommittedReadAdapter(PublicationReader(engine, self.tables))
        key = settings.session_secret.get_secret_value().encode()
        self.read_router = create_router(
            HistoryService(adapter, CursorCodec(key), presenter),
            CatchUpService(adapter, presenter),
            LiveStreamService(adapter, presenter),
            SessionAccess(sessionmaker(engine), settings),
        )
        self.investigation_router = investigation_router(
            InvestigationService(engine, self.tables, self.investigation, presenter, key)
        )

    def install(self, app: FastAPI, errors: dict[int | str, dict[str, Any]]) -> None:
        errors = errors | {status: {"model": ErrorResponse} for status in (410, 413, 503)}
        app.include_router(self.read_router, responses=errors)
        app.include_router(self.investigation_router, responses=errors)
        router = APIRouter(prefix="/agent/v1", tags=["telemetry-agent"])

        @router.post(
            "/events",
            response_model=AgentAck,
            openapi_extra={
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/AgentBatch"}}
                    },
                }
            },
        )
        async def ingest(request: Request, agent: Agent, db: Database) -> dict[str, Any]:
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 256 * 1024:
                    raise DomainError(413, "Пакет слишком большой")
            try:
                document = json.loads(body, object_pairs_hook=self.ingress.unique_object)
                if (
                    not isinstance(document, dict)
                    or set(document) != {"schema_version", "batch_id", "events"}
                    or type(document["schema_version"]) is not int
                    or document["schema_version"] != 1
                    or not isinstance(document["events"], list)
                    or not 1 <= len(document["events"]) <= 100
                ):
                    raise ValueError
                batch_id = str(UUID(document["batch_id"]))
            except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
                raise DomainError(422, "Некорректный пакет") from None
            prepared, rejected = [], []
            seen = set()
            for index, value in enumerate(document["events"]):
                try:
                    event = self.ingress.prepare(index, value)
                except (ValidationError, ValueError, TypeError, KeyError, RecursionError):
                    rejected.append(
                        dict(index=index, event_id=None, code="INVALID_EVENT", retryable=False)
                    )
                    continue
                if event.event_id in seen:
                    raise DomainError(422, "Повтор event_id в пакете")
                seen.add(event.event_id)
                code = self.ingress.binding(db, agent, event)
                if code:
                    rejected.append(
                        dict(
                            index=index,
                            event_id=str(event.event_id),
                            code=code,
                            retryable=code == "INSTANCE_NOT_REGISTERED",
                        )
                    )
                else:
                    prepared.append(event)
            try:
                results = await anyio.to_thread.run_sync(self.ingress.storage.store, prepared)
            except SQLAlchemyError:
                raise DomainError(503, "Телеметрия временно недоступна") from None
            accepted = []
            for result in results:
                if result.status == "accepted":
                    accepted.append(str(result.event_id))
                else:
                    rejected.append(
                        dict(
                            index=result.index,
                            event_id=str(result.event_id),
                            code=result.status,
                            retryable=False,
                        )
                    )
            return dict(
                batch_id=batch_id,
                accepted_ids=accepted,
                rejected=sorted(rejected, key=lambda r: r["index"]),
            )

        app.include_router(router, responses=errors)

    async def run(self) -> None:
        while True:
            try:
                await anyio.to_thread.run_sync(self.publisher.publish_once)
            except SQLAlchemyError:
                # Отказ сохраняет outbox; readiness и повтор не выдают ложный ACK.
                pass
            await asyncio.sleep(0.2)

    async def stop(self, task: asyncio.Task[None]) -> None:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
