"""Атомарная конверсия проверенного raw через внедрённый mapper в ledger и pending."""

import json
import sqlite3
from dataclasses import dataclass
from hashlib import sha256
from typing import Literal

from agent.telemetry.schemas import Event
from agent.telemetry.spool import SpoolRepository

from .mapping import RawEventMapper
from .models import JournalBinding, JournalRecord


class ConversionError(ValueError):
    """Категория отказа без содержимого сырья, SQL и сообщений mapper."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ConversionLimits:
    """Бюджет одного синхронного шага; oversized BLOB не читается."""

    max_records: int = 100
    max_bytes: int = 4 * 1024 * 1024

    def __post_init__(self) -> None:
        if any(type(v) is not int or v < 1 for v in (self.max_records, self.max_bytes)):
            raise ValueError("Нужны положительные целые лимиты")


@dataclass(frozen=True)
class ConversionProgress:
    """Прогресс отражает завершённые позиции, а не доставку в центр."""

    state: Literal["progress", "idle", "limited", "retry", "error"]
    records: int = 0
    raw_bytes: int = 0
    code: str | None = None


class LowRawConverter:
    """Один поток на spool; SQLite сериализует конкурирующие конверсии."""

    def __init__(self, spool: SpoolRepository, mapper: RawEventMapper, version: str) -> None:
        if not isinstance(version, str) or not version.strip() or len(version) > 128:
            raise ValueError("Нужна явная версия mapper до 128 символов")
        self.spool, self.mapper, self.version = spool, mapper, version

    def _key(self, binding: JournalBinding) -> str:
        return json.dumps(
            [
                str(self.spool.agent_id),
                binding.deployment_id,
                binding.instance_id,
                binding.profile_revision,
            ],
            separators=(",", ":"),
        )

    def _checkpoint(self, stage: str) -> None:
        """Граница реальной транзакции для аварийных испытаний; рабочий метод пуст."""

    def convert(
        self, binding: JournalBinding, record: JournalRecord
    ) -> Literal["created", "repeated"]:
        """Проверяет позицию и результат; один commit для pending и ledger."""
        db = self.spool.db
        position = (self._key(binding), record.journal_id, record.offset)
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                stored = db.execute(
                    "SELECT next_offset,kind,raw FROM low_raw_records "
                    "WHERE source_key=? AND journal_id=? AND offset=?",
                    position,
                ).fetchone()
                if stored is None:
                    raise ConversionError("RAW_NOT_SAVED")
                if stored != (record.next_offset, record.kind, record.raw):
                    raise ConversionError("RAW_CONFLICT")
                try:
                    mapped = self.mapper.convert(binding, record)
                except Exception:
                    raise ConversionError("MAPPER_ERROR") from None
                payload = None
                event_id = None
                if mapped is None:
                    if record.kind == "event":
                        raise ConversionError("INVALID_RESULT")
                else:
                    try:
                        if not isinstance(mapped, Event) or record.kind != "event":
                            raise ValueError("Неверный результат")
                        payload = self.spool.validated_payload(mapped)
                        checked = Event.model_validate_json(payload)
                        if (
                            str(checked.deployment_id),
                            str(checked.instance_id),
                            checked.profile_revision,
                        ) != (binding.deployment_id, binding.instance_id, binding.profile_revision):
                            raise ValueError("Чужая привязка")
                        event_id = str(checked.event_id)
                    except Exception:
                        raise ConversionError("INVALID_EVENT") from None
                evidence = (
                    record.next_offset,
                    record.kind,
                    sha256(record.raw).digest(),
                    self.version,
                    "event" if payload is not None else "service",
                    event_id,
                    payload,
                )
                prior = db.execute(
                    "SELECT next_offset,kind,raw_digest,mapper_version,result,event_id,"
                    "canonical_payload FROM low_conversion_ledger "
                    "WHERE source_key=? AND journal_id=? AND offset=?",
                    position,
                ).fetchone()
                if prior is not None:
                    if prior != evidence:
                        raise ConversionError("RESULT_CONFLICT")
                    return "repeated"
                if event_id is not None:
                    # Снимки также защищают ID событий старого append после удаления по ACK.
                    used = db.execute(
                        "SELECT event_id FROM pending WHERE event_id=? UNION ALL "
                        "SELECT event_id FROM quarantine WHERE event_id=? UNION ALL "
                        "SELECT event_id FROM low_conversion_ledger WHERE event_id=? UNION ALL "
                        "SELECT json_extract(j.value,'$.event_id') FROM batch_snapshots b, "
                        "json_each(b.payload,'$.events') j "
                        "WHERE json_extract(j.value,'$.event_id')=? LIMIT 1",
                        (event_id,) * 4,
                    ).fetchone()
                    if used is not None:
                        raise ConversionError("EVENT_ID_CONFLICT")
                    assert payload is not None
                    self.spool.insert_pending(checked.event_id, payload)
                    self._checkpoint("pending_inserted")
                db.execute(
                    "INSERT INTO low_conversion_ledger VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (*position, *evidence),
                )
                self._checkpoint("ledger_inserted")
                self._checkpoint("before_commit")
            self._checkpoint("after_commit")
            return "created"
        except sqlite3.Error as error:
            code = (
                "WRITER_BUSY"
                if getattr(error, "sqlite_errorcode", 0) & 0xFF
                in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
                else "DB_ERROR"
            )
            raise ConversionError(code) from None

    def step(
        self, binding: JournalBinding, limits: ConversionLimits | None = None
    ) -> ConversionProgress:
        """Не обходит ошибочную позицию; возвращает прогресс прежних commit."""
        limits = limits or ConversionLimits()
        count = size = 0
        db = self.spool.db
        try:
            while True:
                row = db.execute(
                    "SELECT r.journal_id,r.offset,r.next_offset,r.kind,length(r.raw) "
                    "FROM low_raw_records r LEFT JOIN low_conversion_ledger l "
                    "ON r.source_key=l.source_key AND r.journal_id=l.journal_id "
                    "AND r.offset=l.offset "
                    "WHERE r.source_key=? AND l.offset IS NULL ORDER BY r.rowid LIMIT 1",
                    (self._key(binding),),
                ).fetchone()
                if row is None:
                    return ConversionProgress("progress" if count else "idle", count, size)
                if count >= limits.max_records or size + row[4] > limits.max_bytes:
                    return ConversionProgress("limited", count, size, "STEP_LIMIT")
                raw = db.execute(
                    "SELECT raw FROM low_raw_records "
                    "WHERE source_key=? AND journal_id=? AND offset=?",
                    (self._key(binding), row[0], row[1]),
                ).fetchone()[0]
                record = JournalRecord(row[3], row[0], row[1], row[2], raw)
                self.convert(binding, record)
                count += 1
                size += len(raw)
        except (ConversionError, sqlite3.Error) as error:
            code = error.code if isinstance(error, ConversionError) else "DB_ERROR"
            return ConversionProgress(
                "retry" if code == "WRITER_BUSY" else "error", count, size, code
            )
