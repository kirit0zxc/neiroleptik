"""Рабочий приём валидирует общий экспортированный контракт и историческую привязку."""

import json
import re
from datetime import UTC, datetime
from importlib.resources import files
from ipaddress import ip_address
from typing import Any
from uuid import UUID

from jsonschema import Draft202012Validator, FormatChecker  # type: ignore[import-untyped]
from sqlalchemy.orm import Session

from honeyforge.models import Deployment, Host, ProfileRevision, RuntimeInstance
from honeyforge.telemetry.storage import EventStorage
from honeyforge.telemetry.types import PreparedEvent


class IngressService:
    """Схема поставляется в wheel и проверяется на равенство агентскому экспорту."""

    def __init__(self, storage: EventStorage) -> None:
        self.storage = storage
        document = json.loads(
            files("honeyforge.telemetry")
            .joinpath("agent-event-v1.schema.json")
            .read_text(encoding="utf-8")
        )
        self.validator = Draft202012Validator(document, format_checker=FormatChecker())

    @staticmethod
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Повтор ключа JSON")
            result[key] = value
        return result

    def prepare(self, index: int, value: Any) -> PreparedEvent:
        self.validator.validate(value)
        event = dict(value)
        event.setdefault("schema_version", 1)
        event.setdefault("session_id", None)
        # JSON Schema считает 1.0 целым; Event v1 требует именно JSON integer.
        integers = [event[k] for k in ("schema_version", "source_seq", "profile_revision")]
        integers.append(event["service"]["port"])
        if event["source"] is not None:
            integers.append(event["source"]["port"])
        if any(type(v) is not int for v in integers):
            raise ValueError("Целочисленные поля требуют строгий integer")
        stamp = event["occurred_at"]
        if not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})",
            stamp,
        ):
            raise ValueError("Точность времени выше микросекунд")
        try:
            normalized = datetime.fromisoformat(stamp).astimezone(UTC)
        except OverflowError:
            raise ValueError("Время выходит за диапазон UTC") from None
        if normalized >= datetime(9999, 12, 31, 23, 59, tzinfo=UTC):
            raise ValueError("Время не допускает представимый конец минутного окна")
        # Совпадает с Pydantic JSON сериализацией Event и canonical_version=1.
        event["occurred_at"] = normalized.isoformat().replace("+00:00", "Z")
        for key in ("agent_id", "event_id", "deployment_id", "instance_id"):
            event[key] = str(UUID(event[key]))
        if event["session_id"] is not None:
            event["session_id"] = str(UUID(event["session_id"]))
        event["artifact_ids"] = [str(UUID(v)) for v in event["artifact_ids"]]
        if len(event["artifact_ids"]) != len(set(event["artifact_ids"])):
            raise ValueError("artifact_ids содержит повторы")
        if event["source"] is not None:
            event["source"] = dict(event["source"]) | {"ip": str(ip_address(event["source"]["ip"]))}

        def compact(value: object) -> str:
            return json.dumps(
                value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
            )

        if len(compact(event["data"]).encode()) > 8192:
            raise ValueError("Лимит data")
        return PreparedEvent(
            index,
            UUID(event["agent_id"]),
            UUID(event["event_id"]),
            UUID(event["deployment_id"]),
            UUID(event["instance_id"]),
            event["profile_revision"],
            compact(event).encode(),
        )

    @staticmethod
    def binding(session: Session, host: Host, event: PreparedEvent) -> str | None:
        if event.agent_id != host.id:
            return "DEPLOYMENT_MISMATCH"
        deployment = session.get(Deployment, event.deployment_id)
        if deployment is None or deployment.host_id != host.id:
            return "DEPLOYMENT_MISMATCH"
        instance = session.get(RuntimeInstance, event.instance_id)
        if instance is None:
            return "INSTANCE_NOT_REGISTERED"
        revision = session.get(ProfileRevision, instance.profile_revision_id)
        if (
            instance.deployment_id != deployment.id
            or revision is None
            or revision.revision != event.profile_revision
        ):
            return "DEPLOYMENT_MISMATCH"
        return None
