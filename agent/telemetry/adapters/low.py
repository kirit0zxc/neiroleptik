"""Адаптер предложенного Low JSONL; реальный формат согласуется с runtime."""

import json
from dataclasses import dataclass
from uuid import NAMESPACE_URL, UUID, uuid5

from agent.telemetry.schemas import Event, compact_json


@dataclass(frozen=True)
class SourceRecord:
    """Событие и позиция сохраняются spool одной транзакцией."""

    source_id: str
    offset: int
    next_offset: int
    event: Event


class LowAdapter:
    """Идентичность берётся из конфигурации хоста, не из журнала ловушки."""

    def __init__(
        self,
        agent_id: str,
        deployment_id: str,
        instance_id: str,
        profile_revision: int,
    ) -> None:
        self.agent_id = str(UUID(agent_id))
        self.deployment_id = str(UUID(deployment_id))
        self.instance_id = str(UUID(instance_id))
        self.profile_revision = profile_revision

    def parse_line(self, line: bytes, *, source_id: str, offset: int) -> SourceRecord:
        if not source_id or not source_id.strip() or type(offset) is not int or offset < 0:
            raise ValueError("Нужны source_id поколения журнала и неотрицательный offset")
        if not line.endswith(b"\n") or b"\n" in line[:-1]:
            raise ValueError("Нужна одна завершённая JSONL-строка")
        if len(line) > 64 * 1024:
            raise ValueError("Исходная строка превышает 64 КиБ")
        payload = json.loads(line.decode("utf-8"), object_pairs_hook=self._unique_object)
        if not isinstance(payload, dict):
            raise ValueError("Low JSONL должен содержать объект")
        allowed = {"occurred_at", "type", "source", "service", "data", "session_id", "artifact_ids"}
        if set(payload) - allowed:
            raise ValueError("Low JSONL содержит неизвестные или служебные поля")
        identity = compact_json(
            [
                self.agent_id,
                self.deployment_id,
                self.instance_id,
                source_id,
                offset,
            ]
        )
        event = Event.model_validate(
            {
                **payload,
                "artifact_ids": payload.get("artifact_ids", []),
                "schema_version": 1,
                "event_id": str(uuid5(NAMESPACE_URL, identity)),
                "agent_id": self.agent_id,
                "deployment_id": self.deployment_id,
                "instance_id": self.instance_id,
                "profile_revision": self.profile_revision,
                "source_seq": offset,
            }
        )
        return SourceRecord(source_id, offset, offset + len(line), event)

    @staticmethod
    def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        """Повторы на любой глубине неоднозначны и не должны терять исходные данные."""
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Low JSONL содержит повторяющиеся ключи JSON")
            result[key] = value
        return result
