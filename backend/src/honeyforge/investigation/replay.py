"""Постраничное чтение записанного TTY без открытия произвольных файлов сервера."""

import base64
import hashlib
import hmac
import json
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, select

from honeyforge.common import DomainError
from honeyforge.telemetry.tables import TelemetryTables


class ReplayService:
    """Источник — только committed публикации; права проверяет общий Reader dependency."""

    def __init__(self, engine: Engine, tables: TelemetryTables, key: bytes) -> None:
        self.engine, self.tables, self.key = engine, tables, key

    def cursor(self, session: UUID, seq: int) -> str:
        payload = json.dumps([str(session), seq], separators=(",", ":")).encode()
        return (
            base64.urlsafe_b64encode(hmac.digest(self.key, payload, hashlib.sha256) + payload)
            .decode()
            .rstrip("=")
        )

    def after(self, session: UUID, cursor: str | None) -> int:
        if cursor is None:
            return -1
        try:
            if len(cursor) > 512:
                raise ValueError
            raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
            if not hmac.compare_digest(raw[:32], hmac.digest(self.key, raw[32:], "sha256")):
                raise ValueError
            identity, seq = json.loads(raw[32:])
            if identity != str(session) or type(seq) is not int or not 0 <= seq <= 9007199254740991:
                raise ValueError
            return seq
        except (ValueError, TypeError):
            raise DomainError(422, "Некорректный курсор replay", "INVALID_CURSOR") from None

    @staticmethod
    def chunk(data: Any) -> dict[str, Any] | None:
        """Проверяем вложенные поля: обычный session.output не открывает произвольный JSON."""
        if not isinstance(data, dict):
            return None
        try:
            for key in ("seq", "capture_seq", "offset_ms"):
                if type(data[key]) is not int or not 0 <= data[key] <= 9007199254740991:
                    raise ValueError
            if data["capture_seq"] > 4000 or data["direction"] not in ("in", "out"):
                raise ValueError
            if type(data["truncated"]) is not bool or type(data["final"]) is not bool:
                raise ValueError
            if not isinstance(data["data_base64"], str) or len(data["data_base64"]) > 5464:
                raise ValueError
            if len(base64.b64decode(data["data_base64"], validate=True)) > 4096:
                raise ValueError
            UUID(data["recording_id"])
            stamp = datetime.fromisoformat(data["started_at"])
            if stamp.utcoffset() is None:
                raise ValueError
            return {
                key: data[key]
                for key in (
                    "seq",
                    "capture_seq",
                    "recording_id",
                    "offset_ms",
                    "direction",
                    "data_base64",
                    "truncated",
                    "started_at",
                    "final",
                )
            }
        except (KeyError, TypeError, ValueError, AttributeError):
            return None

    def page(self, session: UUID, after: str | None, limit: int) -> dict[str, Any]:
        boundary = self.after(session, after)
        e, p = self.tables.events, self.tables.publications
        with self.engine.connect().execution_options(isolation_level="REPEATABLE READ") as db:
            rows = (
                db.execute(
                    select(e.c.canonical_bytes)
                    .join(p, e.c.id == p.c.event_pk)
                    .where(e.c.session_id == session)
                    .order_by(p.c.stream_seq)
                    .limit(10001)
                )
                .scalars()
                .all()
            )
        if not rows:
            raise DomainError(404, "Сессия не найдена", "SESSION_NOT_FOUND")
        if len(rows) > 10000:
            raise DomainError(413, "Сессия превышает лимит локального replay", "REPLAY_LIMIT")
        events = [json.loads(bytes(row)) for row in rows]
        chunks: dict[int, dict[str, Any]] = {}
        recordings: dict[str, list[dict[str, Any]]] = {}
        invalid = False
        for event in events:
            if event["type"] != "session.output" or "replay" not in event["data"]:
                continue
            value = self.chunk(event["data"]["replay"])
            if value is None:
                invalid = True
                continue
            if value["seq"] in chunks:
                raise DomainError(409, "Конфликт порядка replay", "REPLAY_CONFLICT")
            chunks[value["seq"]] = value
            recordings.setdefault(value["recording_id"], []).append(value)
        manifests = [
            event["data"].get("replay_recordings")
            for event in events
            if event["type"] == "session.closed"
        ]
        expected = next(
            (
                value
                for value in manifests
                if isinstance(value, list)
                and len(value) <= 128
                and all(isinstance(item, str) for item in value)
            ),
            None,
        )
        origin = min(
            (datetime.fromisoformat(v["started_at"]) for v in chunks.values()),
            default=min(datetime.fromisoformat(e["occurred_at"]) for e in events),
        )
        started = origin.isoformat()
        complete = expected is not None and set(expected) == set(recordings) and not invalid
        for recording in recordings.values():
            recording.sort(key=lambda v: v["capture_seq"])
            complete = (
                complete
                and recording[-1]["final"]
                and [v["capture_seq"] for v in recording]
                == list(range(recording[-1]["capture_seq"] + 1))
            )
        # Закрытая сессия публикуется целиком: поздний чанк не может попасть за tail cursor.
        if not complete:
            chunks = {}
        # Каналы SSH могут закрыться в обратном порядке. После полного manifest
        # фиксируем общий порядок по времени, а не по смещению log.closed в JSON.
        ordered = sorted(
            chunks.values(),
            key=lambda chunk: (
                int((datetime.fromisoformat(chunk["started_at"]) - origin).total_seconds() * 1000)
                + chunk["offset_ms"],
                chunk["seq"],
            ),
        )
        selected = [dict(chunk, seq=seq) for seq, chunk in enumerate(ordered) if seq > boundary]
        visible = selected[:limit]
        public = []
        for chunk in visible:
            item = {
                key: chunk[key]
                for key in ("seq", "offset_ms", "direction", "data_base64", "truncated")
            }
            item["offset_ms"] += max(
                0,
                int((datetime.fromisoformat(chunk["started_at"]) - origin).total_seconds() * 1000),
            )
            public.append(item)
        tail = visible[-1]["seq"] if visible else boundary
        return {
            "session_id": str(session),
            "started_at": started,
            "chunks": public,
            "next_cursor": self.cursor(session, tail) if len(selected) > limit else None,
            "tail_cursor": self.cursor(session, tail) if tail >= 0 else None,
            "complete": bool(complete),
        }
