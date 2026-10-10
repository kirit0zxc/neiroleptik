"""Сериализованный committed журнал без сетевых отправок из транзакции."""

from sqlalchemy import Engine, insert, select, update
from sqlalchemy.orm import Session

from honeyforge.common import utcnow
from honeyforge.telemetry.tables import TelemetryTables
from honeyforge.telemetry.types import BIGINT_MAX


class OutboxPublisher:
    """Состояние → outbox → publication; lock удерживается до commit счётчика."""

    def __init__(self, engine: Engine, tables: TelemetryTables) -> None:
        self.engine = engine
        self.tables = tables

    def _checkpoint(self, stage: str, session: Session) -> None:
        """Точная граница управляемого отказа/ожидания в тестах."""

    def publish_once(self, limit: int = 100) -> int:
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("limit должен быть 1..500")
        t = self.tables
        with Session(self.engine) as session, session.begin():
            last_seq = session.execute(
                select(t.state.c.last_seq).where(t.state.c.id == 1).with_for_update()
            ).scalar_one()
            self._checkpoint("state_locked", session)
            pending = (
                session.execute(
                    select(t.outbox.c.event_pk)
                    .where(t.outbox.c.processed_at.is_(None))
                    .order_by(t.outbox.c.event_pk)
                    .limit(limit)
                    .with_for_update()
                )
                .scalars()
                .all()
            )
            if last_seq > BIGINT_MAX - len(pending):
                raise OverflowError("Исчерпан диапазон stream_seq")
            now = utcnow()
            for position, event_pk in enumerate(pending, 1):
                session.execute(
                    insert(t.publications).values(
                        stream_seq=last_seq + position,
                        event_pk=event_pk,
                        published_at=now,
                    )
                )
                self._checkpoint("publication_inserted", session)
                session.execute(
                    update(t.outbox).where(t.outbox.c.event_pk == event_pk).values(processed_at=now)
                )
            if pending:
                session.execute(
                    update(t.state)
                    .where(t.state.c.id == 1)
                    .values(last_seq=last_seq + len(pending))
                )
            self._checkpoint("before_commit", session)
        return len(pending)
