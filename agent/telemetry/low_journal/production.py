"""Детерминированное преобразование проверенного Low JSONL по согласованному MVP."""

import base64
import re
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import JsonValue

from agent.telemetry.schemas import MAX_DATA_BYTES, Event, compact_json

from .models import JournalBinding, JournalRecord
from .validation import RecordValidator

HTTP_METHODS = frozenset(
    {"GET", "HEAD", "POST", "PUT", "DELETE", "OPTIONS", "PATCH", "TRACE", "CONNECT"}
)


class LowProductionMapper:
    """Не читает файлы и не пишет очередь: атомарность обеспечивает LowRawConverter."""

    version = "low-journal-v1/1"

    def __init__(self, agent_id: str | UUID) -> None:
        self.agent_id = str(UUID(str(agent_id)))

    @staticmethod
    def _http(payload: bytes) -> dict[str, JsonValue]:
        """Ограниченный синтаксический разбор без интерпретации или публикации headers."""
        sample = payload[:4096]
        end = sample.find(b"\r\n")
        if end < 0:
            return {"http_parse_state": "limited" if len(sample) > 1024 else "incomplete"}
        if end > 1024:
            return {"http_parse_state": "limited"}
        match = re.fullmatch(rb"([A-Z]+) ([\x21-\x7e]+) HTTP/(1\.[01])", sample[:end])
        if match is None or match[1].decode("ascii") not in HTTP_METHODS:
            return {"http_parse_state": "invalid"}
        metadata: dict[str, JsonValue] = {
            "method": match[1].decode("ascii"),
            "http_version": match[3].decode("ascii"),
            "path": "/" if match[2] == b"/" else "[скрыто]",
        }
        cursor = end + 2
        headers = 0
        while True:
            end = sample.find(b"\r\n", cursor)
            if end < 0:
                limited = len(sample) == 4096 or len(sample) - cursor > 1024
                state = "limited" if limited else "incomplete"
                break
            if end == cursor:
                state = "parsed"
                break
            headers += 1
            if headers > 32 or end - cursor > 1024:
                state = "limited"
                break
            pattern = rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+:[\x09\x20-\x7e\x80-\xff]*"
            if re.fullmatch(pattern, sample[cursor:end]) is None:
                state = "invalid"
                break
            cursor = end + 2
        return metadata | {"http_parse_state": state}

    @staticmethod
    def _capture(payload: bytes, metadata: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """Максимальный base64-префикс при точном ограничении размера всего data."""

        def candidate(length: int) -> dict[str, JsonValue]:
            return metadata | {
                "captured_bytes": len(payload),
                "capture_completeness": "unknown",
                "mapper_truncated": length < len(payload),
                "payload_b64": base64.b64encode(payload[:length]).decode("ascii"),
            }

        low, high = 0, len(payload)
        if len(compact_json(candidate(0)).encode("utf-8")) > MAX_DATA_BYTES:
            raise ValueError("METADATA_LIMIT")
        while low < high:
            middle = (low + high + 1) // 2
            if len(compact_json(candidate(middle)).encode("utf-8")) <= MAX_DATA_BYTES:
                low = middle
            else:
                high = middle - 1
        return candidate(low)

    def convert(self, binding: JournalBinding, record: JournalRecord) -> Event | None:
        """Повторно проверяет форму raw; доверие к порядку/привязке даёт durable reader."""
        row = RecordValidator().parse(record.raw, record.offset)
        if row["record"] != record.kind or record.next_offset != record.offset + len(record.raw):
            raise ValueError("INVALID_RECORD")
        if record.kind != "event":
            return None
        scope = [self.agent_id, binding.deployment_id, binding.instance_id]
        data: dict[str, JsonValue] = {"low_seq": str(row["seq"])}
        event_type = "connection.opened"
        if row["event"] == "request":
            payload = base64.b64decode(row["payload_b64"], validate=True)
            event_type = "http.request" if row["protocol"] == "http" else "tcp.request"
            metadata = data | (self._http(payload) if row["protocol"] == "http" else {})
            data = self._capture(payload, metadata)
        peer = row["peer"]
        return Event.model_validate(
            {
                "event_id": uuid5(
                    NAMESPACE_URL, compact_json([*scope, record.journal_id, record.offset])
                ),
                "agent_id": self.agent_id,
                "deployment_id": binding.deployment_id,
                "instance_id": binding.instance_id,
                "profile_revision": binding.profile_revision,
                "session_id": uuid5(
                    NAMESPACE_URL, compact_json([*scope, "low", row["connection_id"]])
                ),
                "source_seq": record.offset,
                "occurred_at": row["timestamp"],
                "type": event_type,
                "source": None if peer is None else {"ip": peer["host"], "port": peer["port"]},
                "service": row["service"],
                "data": data,
                "artifact_ids": [],
            }
        )
