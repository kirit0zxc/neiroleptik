"""Ограниченный подписанный cursor связывает keyset со snapshot и фильтрами."""

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from honeyforge.common import DomainError
from honeyforge.telemetry.read_contract import HistoryFilters

MAX_SEQ = 2**63 - 1


def sequence(value: str) -> int:
    """Bigint разбирается без float и без неограниченного преобразования строки."""
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 19
        or not value.isascii()
        or not value.isdecimal()
    ):
        raise DomainError(422, "Некорректная граница потока")
    number = int(value)
    if number > MAX_SEQ:
        raise DomainError(422, "Некорректная граница потока")
    return number


class CursorCodec:
    """Ключ внедряется явно; секрет не содержится в cursor или диагностике."""

    def __init__(self, key: bytes) -> None:
        if len(key) < 32:
            raise ValueError("Ключ cursor должен содержать минимум 32 байта")
        self.key = key

    def encode(
        self,
        watermark: int,
        last: tuple[datetime, str, str],
        filters: HistoryFilters,
    ) -> str:
        payload = json.dumps(
            {
                "v": 1,
                "order": "received_at/event_id/agent_id:desc",
                "w": str(watermark),
                "last": [last[0].isoformat(), last[1], last[2]],
                "filters": filters.normalized(),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        signature = hmac.digest(self.key, payload, hashlib.sha256)
        return base64.urlsafe_b64encode(signature + payload).decode().rstrip("=")

    def decode(
        self,
        cursor: str,
        filters: HistoryFilters,
    ) -> tuple[int, tuple[datetime, str, str]]:
        try:
            if not isinstance(cursor, str) or not 1 <= len(cursor) <= 4096:
                raise ValueError
            raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
            signature, payload = raw[:32], raw[32:]
            if not hmac.compare_digest(signature, hmac.digest(self.key, payload, "sha256")):
                raise ValueError
            data: dict[str, Any] = json.loads(payload)
            if (
                set(data) != {"v", "order", "w", "last", "filters"}
                or type(data["v"]) is not int
                or data["v"] != 1
                or data["order"] != "received_at/event_id/agent_id:desc"
                or data["filters"] != filters.normalized()
            ):
                raise ValueError
            last = data["last"]
            if not isinstance(last, list) or len(last) != 3:
                raise ValueError
            stamp = datetime.fromisoformat(last[0])
            if stamp.tzinfo is None:
                raise ValueError
            return sequence(data["w"]), (
                stamp.astimezone(UTC),
                str(UUID(last[1])),
                str(UUID(last[2])),
            )
        except (ValueError, TypeError, KeyError, OverflowError, DomainError):
            raise DomainError(422, "Некорректный курсор истории") from None
