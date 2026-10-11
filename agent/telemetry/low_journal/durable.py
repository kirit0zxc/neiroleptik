"""Долговечные исходные строки Low, изолированные от очереди Event."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from uuid import UUID

from .models import JournalBinding, JournalReadError, JournalRecord
from .validation import RecordValidator


class RawJournalError(ValueError):
    """Безопасная категория ошибки без bytes, путей и сообщения SQLite."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class RawJournalRepository:
    """Один объект на поток; конкурентные записи сериализуются SQLite."""

    def __init__(
        self, path: str | Path, agent_id: str, *, max_raw_bytes: int = 256 * 1024 * 1024
    ) -> None:
        self.agent_id = str(UUID(agent_id))
        if type(max_raw_bytes) is not int or max_raw_bytes < 1:
            raise ValueError("Нужна положительная квота сырья")
        self.max_raw_bytes = max_raw_bytes
        try:
            self.db = sqlite3.connect(path, timeout=0.1)
        except sqlite3.Error:
            raise RawJournalError("DB_ERROR") from None
        try:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                self.db.execute(
                    "INSERT OR IGNORE INTO metadata VALUES ('agent_id', ?)", (self.agent_id,)
                )
                owner = self.db.execute(
                    "SELECT value FROM metadata WHERE key='agent_id'"
                ).fetchone()[0]
                if owner != self.agent_id:
                    raise RawJournalError("OWNER_MISMATCH")
                self.db.execute("""
                    CREATE TABLE IF NOT EXISTS low_raw_generations (
                        source_key TEXT NOT NULL, journal_id TEXT NOT NULL,
                        next_offset INTEGER NOT NULL,
                        journal_seq TEXT NOT NULL, previous_journal_id TEXT,
                        UNIQUE (source_key, journal_seq),
                        PRIMARY KEY (source_key, journal_id)
                    )
                """)
                self.db.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS low_raw_successor "
                    "ON low_raw_generations(source_key,previous_journal_id) "
                    "WHERE previous_journal_id IS NOT NULL"
                )
                self.db.execute(
                    "CREATE TABLE IF NOT EXISTS low_raw_usage "
                    "(singleton INTEGER PRIMARY KEY CHECK(singleton=1), bytes INTEGER NOT NULL)"
                )
                self.db.execute("INSERT OR IGNORE INTO low_raw_usage VALUES (1,0)")
                self.db.execute("""
                    CREATE TABLE IF NOT EXISTS low_raw_locations (
                        source_key TEXT NOT NULL, path TEXT NOT NULL, journal_id TEXT NOT NULL,
                        PRIMARY KEY(source_key,path)
                    )
                """)
                self.db.execute("""
                    CREATE TABLE IF NOT EXISTS low_raw_records (
                        source_key TEXT NOT NULL, journal_id TEXT NOT NULL,
                        offset INTEGER NOT NULL, next_offset INTEGER NOT NULL,
                        kind TEXT NOT NULL, raw BLOB NOT NULL,
                        PRIMARY KEY (source_key, journal_id, offset)
                    )
                """)
        except (sqlite3.Error, RawJournalError) as error:
            self.db.close()
            code = error.code if isinstance(error, RawJournalError) else "DB_ERROR"
            raise RawJournalError(code) from None
        except BaseException:
            self.db.close()
            raise

    def close(self) -> None:
        """Закрывает соединение; незавершённая транзакция не фиксируется."""
        self.db.close()

    def source_key(self, binding: JournalBinding) -> str:
        """Идентичность задана конфигурацией, а не именем файла или payload."""
        return json.dumps(
            [self.agent_id, binding.deployment_id, binding.instance_id, binding.profile_revision],
            separators=(",", ":"),
        )

    def checkpoint(self, binding: JournalBinding, journal_id: str) -> int:
        """Возвращает конец последней атомарно сохранённой строки поколения."""
        try:
            row = self.db.execute(
                "SELECT next_offset FROM low_raw_generations WHERE source_key=? AND journal_id=?",
                (self.source_key(binding), journal_id),
            ).fetchone()
            return int(row[0]) if row else 0
        except sqlite3.Error:
            raise RawJournalError("DB_ERROR") from None

    def known_position(self, binding: JournalBinding, path: Path) -> int:
        """Путь — только подсказка для диагностики усечения без полного header."""
        try:
            row = self.db.execute(
                "SELECT journal_id FROM low_raw_locations WHERE source_key=? AND path=?",
                (self.source_key(binding), str(path.absolute())),
            ).fetchone()
            return self.checkpoint(binding, row[0]) if row else 0
        except sqlite3.Error:
            raise RawJournalError("DB_ERROR") from None

    def _remember(self, key: str, path: Path | None, record: JournalRecord) -> None:
        """Подсказка имени фиксируется вместе с header, без отдельного окна commit."""
        if path is not None and record.kind == "header":
            self.db.execute(
                "INSERT INTO low_raw_locations VALUES (?,?,?) "
                "ON CONFLICT(source_key,path) DO UPDATE SET journal_id=excluded.journal_id",
                (key, str(path.absolute()), record.journal_id),
            )

    def append(
        self, binding: JournalBinding, record: JournalRecord, *, path: Path | None = None
    ) -> bool:
        """Принимает результат безопасного reader; полный повтор не расходует квоту."""
        if (
            record.offset < 0
            or record.next_offset != record.offset + len(record.raw)
            or not record.raw.endswith(b"\n")
            or (record.offset == 0) != (record.kind == "header")
        ):
            raise RawJournalError("INVALID_POSITION")
        key = self.source_key(binding)
        try:
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                previous = self.db.execute(
                    "SELECT next_offset,kind,raw FROM low_raw_records "
                    "WHERE source_key=? AND journal_id=? AND offset=?",
                    (key, record.journal_id, record.offset),
                ).fetchone()
                if previous is not None:
                    if previous != (record.next_offset, record.kind, record.raw):
                        raise RawJournalError("POSITION_CONFLICT")
                    self._remember(key, path, record)
                    return False
                if self.checkpoint(binding, record.journal_id) != record.offset:
                    raise RawJournalError("POSITION_CONFLICT")
                # Прямой вызов repository также проверяет форму и доверенную привязку.
                try:
                    document = RecordValidator().parse(record.raw, record.offset)
                except JournalReadError:
                    raise RawJournalError("INVALID_RECORD") from None
                if document["record"] != record.kind:
                    raise RawJournalError("INVALID_RECORD")
                if record.kind == "footer" and document["journal_id"] != record.journal_id:
                    raise RawJournalError("IDENTITY_MISMATCH")
                if record.kind == "header" and (
                    document["journal_id"] != record.journal_id
                    or document["deployment_id"] != binding.deployment_id
                    or document["instance_id"] != binding.instance_id
                    or document["profile_revision"] != binding.profile_revision
                ):
                    raise RawJournalError("IDENTITY_MISMATCH")
                used = int(self.db.execute("SELECT bytes FROM low_raw_usage").fetchone()[0])
                if used + len(record.raw) > self.max_raw_bytes:
                    raise RawJournalError("STORAGE_LIMIT")
                self.db.execute(
                    "INSERT INTO low_raw_records VALUES (?,?,?,?,?,?)",
                    (
                        key,
                        record.journal_id,
                        record.offset,
                        record.next_offset,
                        record.kind,
                        record.raw,
                    ),
                )
                if record.kind == "header":
                    conflict = self.db.execute(
                        "SELECT journal_id FROM low_raw_generations WHERE source_key=? "
                        "AND (journal_seq=? OR previous_journal_id=?)",
                        (key, str(document["journal_seq"]), document["previous_journal_id"]),
                    ).fetchone()
                    if conflict is not None:
                        raise RawJournalError("GENERATION_CONFLICT")
                    self.db.execute(
                        "INSERT INTO low_raw_generations VALUES (?,?,?,?,?)",
                        (
                            key,
                            record.journal_id,
                            record.next_offset,
                            str(document["journal_seq"]),
                            document["previous_journal_id"],
                        ),
                    )
                else:
                    self.db.execute(
                        "UPDATE low_raw_generations SET next_offset=? "
                        "WHERE source_key=? AND journal_id=?",
                        (record.next_offset, key, record.journal_id),
                    )
                self.db.execute(
                    "UPDATE low_raw_usage SET bytes=bytes+? WHERE singleton=1", (len(record.raw),)
                )
                self._remember(key, path, record)
            return True
        except sqlite3.Error:
            raise RawJournalError("DB_ERROR") from None
