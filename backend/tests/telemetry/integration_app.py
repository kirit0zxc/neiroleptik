"""Тестовая сборка HTTP → SQL → publisher → REST/WS; production wiring не меняется."""

import json
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import anyio
from agent.telemetry.schemas import MAX_BATCH_BYTES, MAX_BATCH_EVENTS, Ack, Event, RejectedEvent
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import ValidationError
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from honeyforge.config import Settings
from honeyforge.main import create_app
from honeyforge.telemetry.cursors import CursorCodec
from honeyforge.telemetry.history import CatchUpService, HistoryService
from honeyforge.telemetry.presentation import PublicEventPresenter
from honeyforge.telemetry.publisher import OutboxPublisher
from honeyforge.telemetry.read_adapter import CommittedReadAdapter
from honeyforge.telemetry.reader import PublicationReader
from honeyforge.telemetry.storage import EventStorage
from honeyforge.telemetry.stream import LiveStreamService
from honeyforge.telemetry.tables import TelemetryTables
from honeyforge.telemetry.transport import SessionAccess, create_router
from honeyforge.telemetry.types import PreparedEvent

TEST_TOKEN = "hf_agent_test-only-token"
TEST_ORIGIN = "http://localhost:5173"


@dataclass
class IngressControl:
    """Управление только тестовым каналом; не меняет сеть или Settings."""

    mode: str = "normal"
    payloads: list[bytes] = field(default_factory=list)


def mask_test_secrets(public: dict[str, Any]) -> dict[str, Any]:
    """Синтетическая политика испытаний, не согласованная production проекция."""
    if "password" in public["data"]:
        public["data"]["password"] = "[test-masked]"
    return public


class TestIngress:
    """Тестовая авторизация фиксирует точные разрешённые связи, без runtime side effects."""

    __test__ = False

    def __init__(
        self,
        storage: EventStorage,
        control: IngressControl,
        identities: dict[str, tuple[UUID, UUID, UUID, int]],
    ) -> None:
        self.storage, self.control, self.identities = storage, control, identities

    @staticmethod
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Повтор ключа")
            value[key] = item
        return value

    async def post(self, request: Request) -> Response:
        identity = self.identities.get(request.headers.get("authorization", ""))
        if identity is None:
            return JSONResponse({"detail": "test authorization required"}, status_code=401)
        payload = bytearray()
        async for part in request.stream():
            if len(payload) + len(part) > 2 * 1024 * 1024:
                return Response(status_code=413)
            payload.extend(part)
        self.control.payloads.append(bytes(payload))
        if self.control.mode == "outage":
            return Response(status_code=503, headers={"Retry-After": "1"})
        try:
            document = json.loads(payload, object_pairs_hook=self.unique_object)
            if (
                not isinstance(document, dict)
                or set(document) != {"batch_id", "events", "schema_version"}
                or type(document["schema_version"]) is not int
                or document["schema_version"] != 1
                or not isinstance(document["batch_id"], str)
                or not isinstance(document["events"], list)
                or not 1 <= len(document["events"]) <= MAX_BATCH_EVENTS
                or len(json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode())
                > MAX_BATCH_BYTES
            ):
                raise ValueError("Неверный конверт")
            batch_id = UUID(document["batch_id"])
        except (ValueError, TypeError, RecursionError):
            return Response(status_code=422)
        prepared, rejected = [], []
        seen: set[UUID] = set()
        for index, value in enumerate(document["events"]):
            try:
                event = Event.model_validate(value)
            except (ValidationError, ValueError):
                rejected.append(
                    RejectedEvent(index=index, event_id=None, code="INVALID_EVENT", retryable=False)
                )
                continue
            if event.event_id in seen:
                return Response(status_code=422)
            seen.add(event.event_id)
            actual = (
                event.agent_id,
                event.deployment_id,
                event.instance_id,
                event.profile_revision,
            )
            if actual != identity:
                rejected.append(
                    RejectedEvent(
                        index=index,
                        event_id=event.event_id,
                        code="DEPLOYMENT_MISMATCH",
                        retryable=False,
                    )
                )
                continue
            prepared.append(
                PreparedEvent(
                    index,
                    event.agent_id,
                    event.event_id,
                    event.deployment_id,
                    event.instance_id,
                    event.profile_revision,
                    event.canonical_json().encode("utf-8"),
                )
            )
        try:
            results = await anyio.to_thread.run_sync(self.storage.store, prepared)
        except SQLAlchemyError:
            return Response(status_code=503)
        accepted = []
        for result in results:
            if result.status == "accepted":
                accepted.append(result.event_id)
            else:
                rejected.append(
                    RejectedEvent(
                        index=result.index,
                        event_id=result.event_id,
                        code="EVENT_ID_CONFLICT",
                        retryable=False,
                    )
                )
        ack = (
            Ack(
                batch_id=batch_id,
                accepted_ids=accepted,
                rejected=sorted(rejected, key=lambda item: item.index),
            )
            .model_dump_json()
            .encode()
        )
        if self.control.mode == "invalid_ack":
            return Response(b"{}", media_type="application/json")
        if self.control.mode == "timeout_after_commit":
            # Commit уже завершён; общий HTTP deadline должен сохранить pending.
            await anyio.sleep(1)
        if self.control.mode == "lost_response":
            self.control.mode = "normal"

            async def incomplete_body():
                yield b""
                raise RuntimeError("Управляемая потеря ответа после commit")

            return StreamingResponse(
                incomplete_body(),
                media_type="application/json",
                headers={"Content-Length": str(len(ack))},
            )
        return Response(ack, media_type="application/json")


@dataclass
class IntegratedServices:
    """Ссылки на настоящие компоненты для проверок транзакционных границ."""

    app: FastAPI
    storage: EventStorage
    publisher: OutboxPublisher
    reader: PublicationReader
    control: IngressControl


def build_test_app(
    settings: Settings,
    engine: Engine,
    tables: TelemetryTables,
    identities: dict[str, tuple[UUID, UUID, UUID, int]],
    storage: EventStorage | None = None,
) -> IntegratedServices:
    """Routes добавляются только здесь; read/auth зависимости берутся из рабочего кода."""
    app = create_app(settings)
    app.state.engine = engine
    reader = PublicationReader(engine, tables)
    adapter = CommittedReadAdapter(reader)
    presenter = PublicEventPresenter(mask_test_secrets)
    history = HistoryService(adapter, CursorCodec(b"test-cursor-key-00000000000000000"), presenter)
    catchup = CatchUpService(adapter, presenter)
    app.include_router(
        create_router(
            history,
            catchup,
            LiveStreamService(adapter, presenter, poll_seconds=0.01),
            SessionAccess(sessionmaker(engine), settings),
        )
    )
    control = IngressControl()
    storage = storage or EventStorage(engine, tables)
    app.add_api_route(
        "/test/ingest", TestIngress(storage, control, identities).post, methods=["POST"]
    )
    return IntegratedServices(app, storage, OutboxPublisher(engine, tables), reader, control)
