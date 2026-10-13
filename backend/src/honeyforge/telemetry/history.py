"""Snapshot/keyset история и ограниченная догрузка одного committed журнала."""

from datetime import UTC, datetime
from ipaddress import ip_address
from typing import Any
from uuid import UUID

from honeyforge.common import DomainError
from honeyforge.telemetry.cursors import CursorCodec, sequence
from honeyforge.telemetry.presentation import PublicEventPresenter
from honeyforge.telemetry.read_contract import HistoryFilters, PublicationReader

EVENT_TYPES = frozenset(
    {
        "connection.opened",
        "auth.attempt",
        "session.command",
        "session.output",
        "http.request",
        "tcp.request",
        "artifact.observed",
        "token.triggered",
        "session.closed",
        "runtime.state",
    }
)


class HistoryService:
    def __init__(
        self, reader: PublicationReader, codec: CursorCodec, presenter: PublicEventPresenter
    ) -> None:
        self.reader, self.codec, self.presenter = reader, codec, presenter

    @staticmethod
    def filters(
        deployment_id: str | None = None,
        source_ip: str | None = None,
        event_type: str | None = None,
        start: str | None = None,
        end: str | None = None,
        session_id: str | None = None,
        sort: str = "received_at",
        order: str = "desc",
    ) -> HistoryFilters:
        def stamp(value: str | None) -> datetime | None:
            if value is None:
                return None
            if len(value) > 64 or "T" not in value:
                raise ValueError
            result = datetime.fromisoformat(value)
            if result.tzinfo is None:
                raise ValueError
            return result.astimezone(UTC)

        try:
            begin, finish = stamp(start), stamp(end)
            if begin and finish and begin >= finish:
                raise ValueError
            if event_type is not None and event_type not in EVENT_TYPES:
                raise ValueError
            if (sort, order) not in {("received_at", "desc"), ("occurred_at", "asc")}:
                raise ValueError
            return HistoryFilters(
                str(UUID(deployment_id)) if deployment_id is not None else None,
                str(ip_address(source_ip)) if source_ip is not None else None,
                event_type,
                begin,
                finish,
                str(UUID(session_id)) if session_id is not None else None,
                sort,
                order,
            )
        except (ValueError, TypeError, OverflowError):
            raise DomainError(422, "Некорректные фильтры истории") from None

    def page(
        self, filters: HistoryFilters, cursor: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 200:
            raise DomainError(422, "Некорректный размер страницы")
        watermark, before = (
            self.codec.decode(cursor, filters)
            if cursor is not None
            else (self.reader.watermark(), None)
        )
        if watermark > self.reader.watermark():
            raise DomainError(422, "Некорректный курсор истории", "INVALID_CURSOR")
        rows = self.reader.history(filters, watermark, before, limit + 1)
        visible = rows[:limit]
        return {
            "items": [self.presenter.present(row) for row in visible],
            "next_cursor": self.codec.encode(
                watermark,
                (
                    datetime.fromisoformat(visible[-1].event["occurred_at"]),
                    visible[-1].event["event_id"],
                    visible[-1].event["agent_id"],
                )
                if filters.sort == "occurred_at"
                else visible[-1].key,
                filters,
            )
            if len(rows) > limit
            else None,
        }


class CatchUpService:
    def __init__(self, reader: PublicationReader, presenter: PublicEventPresenter) -> None:
        self.reader, self.presenter = reader, presenter

    def page(self, after: str = "0", until: str | None = None, limit: int = 100) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 500:
            raise DomainError(422, "Некорректный размер страницы")
        start, current = sequence(after), self.reader.watermark()
        stop = current if until is None else sequence(until)
        if start > stop or stop > current:
            raise DomainError(422, "Некорректная граница потока")
        floor = self.reader.retention_floor()
        if floor is not None and start < floor:
            raise DomainError(410, "История потока недоступна")
        rows = self.reader.stream(start, stop, limit + 1)
        visible = rows[:limit]
        return {
            "items": [
                {"stream_seq": str(row.seq), "event": self.presenter.present(row)}
                for row in visible
            ],
            "watermark": str(stop),
            "next_cursor": str(visible[-1].seq) if len(rows) > limit else None,
        }
