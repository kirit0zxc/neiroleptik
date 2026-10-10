"""Чтение только committed publication с неизменным snapshot watermark."""

from sqlalchemy import DateTime, Engine, RowMapping, Uuid, func, literal, select, tuple_

from honeyforge.telemetry.tables import TelemetryTables
from honeyforge.telemetry.types import BIGINT_MAX, EventFilters, HistoryKey, StoredEvent


class PublicationReader:
    """Каждый запрос освобождает connection; reader не удерживает publisher lock."""

    def __init__(self, engine: Engine, tables: TelemetryTables) -> None:
        self.engine = engine
        self.tables = tables

    def watermark(self) -> int:
        with self.engine.connect() as connection:
            return int(
                connection.execute(
                    select(func.coalesce(func.max(self.tables.publications.c.stream_seq), 0))
                ).scalar_one()
            )

    def _record(self, row: RowMapping) -> StoredEvent:
        return StoredEvent(
            row["id"],
            row["agent_id"],
            row["event_id"],
            row["received_at"],
            row["stream_seq"],
            bytes(row["canonical_bytes"]),
        )

    def stream(self, after: int, until: int, limit: int = 100) -> list[StoredEvent]:
        self._bounds(until, limit, 501)
        if type(after) is not int or not 0 <= after <= until:
            raise ValueError("Некорректная граница after")
        t = self.tables
        statement = (
            select(t.events, t.publications.c.stream_seq)
            .join(t.publications, t.events.c.id == t.publications.c.event_pk)
            .where(t.publications.c.stream_seq > after, t.publications.c.stream_seq <= until)
            .order_by(t.publications.c.stream_seq)
            .limit(limit)
        )
        with self.engine.connect() as connection:
            return [self._record(row) for row in connection.execute(statement).mappings()]

    def history(
        self,
        watermark: int,
        limit: int = 50,
        before: HistoryKey | None = None,
        filters: EventFilters | None = None,
    ) -> list[StoredEvent]:
        self._bounds(watermark, limit, 201)
        filters = filters or EventFilters()
        if filters.from_time and filters.to_time and filters.from_time >= filters.to_time:
            raise ValueError("from должен быть меньше to")
        t = self.tables
        statement = (
            select(t.events, t.publications.c.stream_seq)
            .join(t.publications, t.events.c.id == t.publications.c.event_pk)
            .where(t.publications.c.stream_seq <= watermark)
        )
        if before:
            statement = statement.where(
                tuple_(t.events.c.received_at, t.events.c.event_id, t.events.c.agent_id)
                < tuple_(
                    literal(before.received_at, type_=DateTime(timezone=True)),
                    literal(before.event_id, type_=Uuid()),
                    literal(before.agent_id, type_=Uuid()),
                )
            )
        for column, value in (
            (t.events.c.deployment_id, filters.deployment_id),
            (t.events.c.source_ip, filters.source_ip),
            (t.events.c.event_type, filters.event_type),
        ):
            if value is not None:
                statement = statement.where(column == value)
        if filters.from_time is not None:
            statement = statement.where(t.events.c.occurred_at >= filters.from_time)
        if filters.to_time is not None:
            statement = statement.where(t.events.c.occurred_at < filters.to_time)
        statement = statement.order_by(
            t.events.c.received_at.desc(), t.events.c.event_id.desc(), t.events.c.agent_id.desc()
        ).limit(limit)
        with self.engine.connect() as connection:
            return [self._record(row) for row in connection.execute(statement).mappings()]

    def _bounds(self, watermark: int, limit: int, maximum: int) -> None:
        if type(watermark) is not int or not 0 <= watermark <= BIGINT_MAX:
            raise ValueError("Некорректный watermark")
        if type(limit) is not int or not 1 <= limit <= maximum:
            raise ValueError("Некорректный limit")
