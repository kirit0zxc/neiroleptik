"""Адаптация настоящего committed SQL reader к read-only сервисам stream."""

from datetime import datetime
from uuid import UUID

from honeyforge.telemetry.read_contract import HistoryFilters, PublishedEvent
from honeyforge.telemetry.reader import PublicationReader
from honeyforge.telemetry.types import EventFilters, HistoryKey, StoredEvent


class CommittedReadAdapter:
    """Не держит Session и не копирует внутренние PK/bytes в публичный объект."""

    def __init__(self, reader: PublicationReader) -> None:
        self.reader = reader

    def watermark(self) -> int:
        return self.reader.watermark()

    def retention_floor(self) -> int | None:
        return None

    @staticmethod
    def convert(record: StoredEvent) -> PublishedEvent:
        return PublishedEvent(record.stream_seq, record.received_at, record.original())

    def history(
        self,
        filters: HistoryFilters,
        watermark: int,
        before: tuple[datetime, str, str] | None,
        limit: int,
    ) -> list[PublishedEvent]:
        normalized = EventFilters(
            UUID(filters.deployment_id) if filters.deployment_id is not None else None,
            filters.start,
            filters.end,
            filters.source_ip,
            filters.event_type,
            UUID(filters.session_id) if filters.session_id is not None else None,
            filters.sort == "occurred_at",
        )
        key = HistoryKey(before[0], UUID(before[1]), UUID(before[2])) if before else None
        return [self.convert(row) for row in self.reader.history(watermark, limit, key, normalized)]

    def stream(self, after: int, until: int, limit: int) -> list[PublishedEvent]:
        return [self.convert(row) for row in self.reader.stream(after, until, limit)]
