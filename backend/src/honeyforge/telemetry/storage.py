"""Атомарная запись уже подготовленного оригинала и задания outbox."""

from collections.abc import Callable, Sequence
from hashlib import sha256
from typing import Literal

from sqlalchemy import Engine, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from honeyforge.common import utcnow
from honeyforge.telemetry.tables import TelemetryTables
from honeyforge.telemetry.types import PreparedEvent, WriteResult


class EventStorage:
    """Собственная короткая транзакция READ COMMITTED; сессия не разделяется."""

    def __init__(
        self,
        engine: Engine,
        tables: TelemetryTables,
        digest: Callable[[bytes], bytes] = lambda value: sha256(value).digest(),
    ) -> None:
        self.engine = engine
        self.tables = tables
        self.digest = digest

    def _checkpoint(self, stage: str, session: Session) -> None:
        """Точная граница для испытания отказов; рабочий метод ничего не делает."""

    def store(self, events: Sequence[PreparedEvent]) -> list[WriteResult]:
        prepared = tuple(events)
        identities = {(e.agent_id, e.event_id) for e in prepared}
        if len(identities) != len(prepared) or len({e.index for e in prepared}) != len(prepared):
            raise ValueError("Повтор идентичности или index во внутреннем списке")
        if not prepared:
            return []
        results = []
        # Единый порядок уникальных ключей исключает цикл ожиданий пересекающихся batch.
        with Session(self.engine) as session, session.begin():
            if session.connection().get_isolation_level() != "READ COMMITTED":
                raise ValueError("Хранилище требует READ COMMITTED")
            for event in sorted(prepared, key=lambda e: (e.agent_id, e.event_id)):
                original = event.original()
                statement = (
                    insert(self.tables.events)
                    .values(
                        agent_id=event.agent_id,
                        event_id=event.event_id,
                        deployment_id=event.deployment_id,
                        instance_id=event.instance_id,
                        profile_revision=event.profile_revision,
                        canonical_bytes=event.canonical_bytes,
                        canonical_version=1,
                        digest=self.digest(event.canonical_bytes),
                        received_at=utcnow(),
                        occurred_at=event.occurred_at,
                        source_ip=original["source"]["ip"],
                        event_type=original["type"],
                    )
                    .on_conflict_do_nothing(constraint="uq_event_identity")
                    .returning(self.tables.events.c.id)
                )
                event_pk = session.execute(statement).scalar_one_or_none()
                status: Literal["accepted", "EVENT_ID_CONFLICT"] = "accepted"
                if event_pk is not None:
                    self._checkpoint("event_inserted", session)
                    session.execute(insert(self.tables.outbox).values(event_pk=event_pk))
                else:
                    # Новый statement получает snapshot победителя после ожидания UNIQUE.
                    stored = session.execute(
                        select(self.tables.events.c.canonical_bytes).where(
                            self.tables.events.c.agent_id == event.agent_id,
                            self.tables.events.c.event_id == event.event_id,
                        )
                    ).scalar_one()
                    if bytes(stored) != event.canonical_bytes:
                        status = "EVENT_ID_CONFLICT"
                results.append(WriteResult(event.index, event.agent_id, event.event_id, status))
            self._checkpoint("before_commit", session)
        return sorted(results, key=lambda result: result.index)
