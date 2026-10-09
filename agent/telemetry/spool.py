"""SQLite WAL: атомарные event+checkpoint и ACK с долговечным карантином."""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import ValidationError

from agent.telemetry.adapters.low import SourceRecord
from agent.telemetry.schemas import MAX_BATCH_BYTES, MAX_BATCH_EVENTS, Ack, Batch, Event


class OffsetConflict(ValueError):
    """Источник перечитан или пропущен: checkpoint нельзя продвинуть молча."""


@dataclass(frozen=True)
class QuarantineEntry:
    """Снимок доказательства отказа; вложенный Event заново читается из БД."""

    event: Event
    code: str
    batch_id: UUID
    index: int
    rejected_at: datetime


class SpoolRepository:
    """Один объект на поток; SQLite сериализует записи через BEGIN IMMEDIATE."""

    def __init__(self, path: str | Path, agent_id: str) -> None:
        self.agent_id = UUID(agent_id)
        self.db = sqlite3.connect(path)
        try:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS checkpoints (
                    source_id TEXT PRIMARY KEY, next_offset INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pending (
                    queue_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS quarantine (
                    quarantine_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE, payload TEXT NOT NULL,
                    batch_id TEXT NOT NULL, code TEXT NOT NULL,
                    batch_index INTEGER NOT NULL CHECK (batch_index >= 0),
                    rejected_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS batch_snapshots (
                    batch_id TEXT PRIMARY KEY, payload BLOB NOT NULL
                );
            """)
            with self.db:
                self.db.execute(
                    "INSERT OR IGNORE INTO metadata(key,value) VALUES ('agent_id',?)",
                    (str(self.agent_id),),
                )
                owner = self.db.execute(
                    "SELECT value FROM metadata WHERE key='agent_id'"
                ).fetchone()[0]
                if owner != str(self.agent_id):
                    raise ValueError("Spool принадлежит другому агенту")
        except Exception:
            self.db.close()
            raise

    def close(self) -> None:
        self.db.close()

    def checkpoint(self, source_id: str) -> int:
        row = self.db.execute(
            "SELECT next_offset FROM checkpoints WHERE source_id=?",
            (source_id,),
        ).fetchone()
        return int(row[0]) if row else 0

    def append(self, record: SourceRecord) -> None:
        """Ошибка в любом шаге откатывает и INSERT, и продвижение checkpoint."""
        if record.event.agent_id != self.agent_id:
            raise ValueError("Событие принадлежит другому агенту")
        if (
            not record.source_id
            or type(record.offset) is not int
            or type(record.next_offset) is not int
            or record.offset < 0
            or record.next_offset <= record.offset
        ):
            raise ValueError("Некорректные границы записи")
        # Повторная валидация защищает от изменения вложенного data после создания модели.
        payload = Event.model_validate_json(record.event.canonical_json()).canonical_json()
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if self.checkpoint(record.source_id) != record.offset:
                raise OffsetConflict("Начало записи не равно сохранённому checkpoint")
            self.db.execute(
                "INSERT INTO pending(event_id,payload) VALUES (?,?)",
                (str(record.event.event_id), payload),
            )
            self.db.execute(
                "INSERT INTO checkpoints(source_id,next_offset) VALUES (?,?) "
                "ON CONFLICT(source_id) DO UPDATE SET next_offset=excluded.next_offset",
                (record.source_id, record.next_offset),
            )

    def count(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM pending").fetchone()[0])

    def quarantine_count(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM quarantine").fetchone()[0])

    def list_quarantine(self, limit: int = 100) -> list[QuarantineEntry]:
        """Ограниченная выборка по порядку первого сохранения, без requeue/очистки."""
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("limit должен быть целым числом от 1 до 500")
        rows = self.db.execute(
            "SELECT payload,code,batch_id,batch_index,rejected_at FROM quarantine "
            "ORDER BY quarantine_seq LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            QuarantineEntry(
                event=Event.model_validate_json(payload),
                code=code,
                batch_id=UUID(batch_id),
                index=index,
                rejected_at=datetime.fromisoformat(rejected_at).astimezone(UTC),
            )
            for payload, code, batch_id, index, rejected_at in rows
        ]

    def next_batch(self) -> Batch | None:
        """Фиксирует исходную пачку до выдачи; pending остаётся до корректного ACK."""
        with self.db:
            # Состав pending и снимок фиксируются под одной блокировкой writer.
            # Ошибка INSERT не выдаёт batch_id без привязки к исходным байтам.
            self.db.execute("BEGIN IMMEDIATE")
            batch = self._read_batch()
            if batch is not None:
                self.db.execute(
                    "INSERT INTO batch_snapshots(batch_id,payload) VALUES (?,?)",
                    (str(batch.batch_id), batch.to_bytes()),
                )
        return batch

    def _read_batch(self) -> Batch | None:
        """Формирует ограниченный по количеству и байтам конверт внутри транзакции."""
        rows = self.db.execute(
            "SELECT payload FROM pending ORDER BY queue_seq LIMIT ?",
            (MAX_BATCH_EVENTS,),
        ).fetchall()
        if not rows:
            return None
        events: list[Event] = []
        batch_id = uuid4()
        # Считаем весь конверт, включая запятые и UTF-8, а не только data.
        size = len(
            ('{"batch_id":"' + str(batch_id) + '","events":[],"schema_version":1}').encode("utf-8")
        )
        for (payload,) in rows:
            event = Event.model_validate_json(payload)
            increment = len(event.canonical_json().encode("utf-8")) + bool(events)
            if size + increment > MAX_BATCH_BYTES:
                if not events:
                    raise ValueError("Одиночное событие превышает лимит batch")
                break
            events.append(event)
            size += increment
        return Batch(batch_id=batch_id, events=events)

    def acknowledge(self, batch: Batch, ack: Ack) -> int:
        """Полный ACK применяется одной транзакцией; возвращает число accepted-удалений."""
        # frozen не защищает вложенные списки/data. Проверяем их заново до любых записей.
        # Python-значения сохраняют NaN для отказа валидатора, вместо JSON null.
        try:
            batch = Batch.model_validate(batch.model_dump(mode="python", warnings=False))
            ack = Ack.model_validate(ack.model_dump(mode="python", warnings=False))
        except ValidationError:
            raise ValueError("Некорректная модель batch или ACK") from None
        if ack.batch_id != batch.batch_id:
            raise ValueError("ACK относится к другому batch")
        if any(event.agent_id != self.agent_id for event in batch.events):
            raise ValueError("Batch принадлежит другому агенту")
        snapshot = self.db.execute(
            "SELECT payload FROM batch_snapshots WHERE batch_id=?", (str(batch.batch_id),)
        ).fetchone()
        if snapshot is None:
            raise ValueError("Для batch_id отсутствует исходный снимок пачки")
        if batch.to_bytes() != snapshot[0]:
            raise ValueError("Batch изменён относительно исходного снимка пачки")
        # БД не обновляет и не удаляет снимки. Полнота/index проверяются по
        # независимой модели исходных байтов, включая уже обработанные события.
        batch = Batch.model_validate_json(snapshot[0])
        expected = {event.event_id for event in batch.events}
        covered = set(ack.accepted_ids)
        if not covered <= expected:
            raise ValueError("ACK содержит ID вне отправленной пачки")
        for item in ack.rejected:
            if item.index >= len(batch.events):
                raise ValueError("Индекс отказа вне отправленной пачки")
            event_id = batch.events[item.index].event_id
            if item.event_id is not None and item.event_id != event_id:
                raise ValueError("ID отказа не соответствует исходному индексу")
            if event_id in covered:
                raise ValueError("Событие имеет несколько результатов ACK")
            covered.add(event_id)
        if covered != expected:
            raise ValueError("ACK не содержит результат для каждого события")

        removed = 0
        with self.db:
            # Блокировка охватывает проверку снимка и изменения: другой writer не
            # может заменить pending между сравнением и удалением/карантином.
            self.db.execute("BEGIN IMMEDIATE")
            saved: dict[UUID, str] = {}
            for event in batch.events:
                row = self.db.execute(
                    "SELECT payload FROM pending WHERE event_id=?", (str(event.event_id),)
                ).fetchone()
                if row is not None:
                    payload = Event.model_validate_json(row[0]).canonical_json()
                    if payload != event.canonical_json():
                        raise ValueError("Payload batch не совпадает с сохранённым событием")
                    saved[event.event_id] = payload
            for event_id in ack.accepted_ids:
                removed += self.db.execute(
                    "DELETE FROM pending WHERE event_id=?",
                    (str(event_id),),
                ).rowcount
            for item in ack.rejected:
                event_id = batch.events[item.index].event_id
                if item.retryable or event_id not in saved:
                    continue
                # Поздний ответ не создаёт доказательство из внешнего batch и не
                # переписывает первичную причину/время уже сохранённого отказа.
                self.db.execute(
                    "INSERT INTO quarantine"
                    "(event_id,payload,batch_id,code,batch_index,rejected_at) "
                    "VALUES (?,?,?,?,?,?) ON CONFLICT(event_id) DO NOTHING",
                    (
                        str(event_id),
                        saved[event_id],
                        str(batch.batch_id),
                        item.code,
                        item.index,
                        datetime.now(UTC).isoformat(),
                    ),
                )
                self.db.execute("DELETE FROM pending WHERE event_id=?", (str(event_id),))
        return removed
