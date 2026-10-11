"""Извлечение только согласованных структурированных индикаторов."""

import csv
import io
import re
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from honeyforge.common import DomainError

HEADER = (
    "indicator_type",
    "indicator_value",
    "occurred_at",
    "agent_id",
    "event_id",
    "deployment_id",
    "session_id",
)


class IocExporter:
    """Материализованный snapshot исключает гонку между подсчётом и ответом."""

    @staticmethod
    def period(start: datetime, end: datetime) -> tuple[datetime, datetime]:
        if start.tzinfo is None or end.tzinfo is None:
            raise DomainError(422, "Период требует timezone")
        try:
            start, end = start.astimezone(UTC), end.astimezone(UTC)
        except OverflowError:
            raise DomainError(422, "Период выходит за диапазон UTC") from None
        if start >= end or end - start > timedelta(days=31):
            raise DomainError(422, "Период должен быть положительным и не больше 31 дня")
        return start, end

    @staticmethod
    def safe_url(value: object) -> str | None:
        """Секреты в произвольном path неразличимы: MVP экспортирует только origin."""
        if not isinstance(value, str) or any(ord(c) <= 32 for c in value):
            return None
        try:
            parts = urlsplit(value)
            if (
                parts.scheme not in {"http", "https"}
                or not parts.hostname
                or parts.username is not None
                or parts.password is not None
                or parts.query
                or parts.fragment
                or parts.path not in {"", "/"}
                or "?" in value
                or "#" in value
                or "\\" in value
            ):
                return None
            host = parts.hostname.encode("idna").decode().lower()
            try:
                address = ip_address(host)
                host = f"[{address}]" if address.version == 6 else str(address)
            except ValueError:
                if not re.fullmatch(r"[a-z0-9.-]+", host):
                    return None
            port = parts.port
            authority = host
            if port is not None and port != (443 if parts.scheme == "https" else 80):
                authority += f":{port}"
            return urlunsplit((parts.scheme, authority, "/", "", ""))
        except (ValueError, UnicodeError):
            return None

    def indicators(self, event: dict[str, Any]) -> set[tuple[str, str]]:
        result: set[tuple[str, str]] = set()
        if event["source"] is not None:
            address = ip_address(event["source"]["ip"])
            result.add((f"ipv{address.version}", str(address)))
        if event["type"] == "artifact.observed":
            digest = event["data"].get("sha256")
            if isinstance(digest, str) and re.fullmatch(r"[a-fA-F0-9]{64}", digest):
                result.add(("sha256", digest.lower()))
            url = self.safe_url(event["data"].get("source_url"))
            if url:
                result.add(("url", url))
        return result

    @staticmethod
    def cell(value: str) -> str:
        # Пробелы и управляющие символы не должны прятать маркер формулы.
        stripped = value
        while stripped and (
            stripped[0].isspace() or ord(stripped[0]) < 32 or stripped[0] == "\ufeff"
        ):
            stripped = stripped[1:]
        return "'" + value if stripped.startswith(("=", "+", "-", "@")) else value

    def render(self, events: Iterable[dict[str, Any]]) -> bytes:
        rows: dict[tuple[str, str, str, str], tuple[str, ...]] = {}
        for event in events:
            at = datetime.fromisoformat(event["occurred_at"]).astimezone(UTC).isoformat()
            for kind, value in self.indicators(event):
                key = kind, value, event["agent_id"], event["event_id"]
                rows[key] = (
                    kind,
                    value,
                    at,
                    event["agent_id"],
                    event["event_id"],
                    event["deployment_id"],
                    event.get("session_id") or "",
                )
                if len(rows) > 100_000:
                    raise DomainError(422, "Экспорт превышает 100000 строк")
        ordered = sorted(
            rows.values(), key=lambda r: (datetime.fromisoformat(r[2]), r[3], r[4], r[0], r[1])
        )
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer, lineterminator="\r\n")
        writer.writerow(HEADER)
        writer.writerows(tuple(self.cell(cell) for cell in row) for row in ordered)
        return buffer.getvalue().encode("utf-8-sig")
