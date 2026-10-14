"""Долговечные исходные строки Low, изолированные от очереди Event."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from uuid import UUID

from agent.telemetry.storage_schema import StorageMigration, StorageSchemaError

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
            migration = StorageMigration()
            migration.check(self.db, self.agent_id)
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            migration.migrate(self.db, self.agent_id)
        except (sqlite3.Error, RawJournalError, StorageSchemaError) as error:
            self.db.close()
            code = (
                error.code
                if isinstance(error, (RawJournalError, StorageSchemaError))
                else "DB_ERROR"
            )
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
        try:
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                return self.append_in_transaction(self.db, binding, record, path=path)
        except sqlite3.Error:
            raise RawJournalError("DB_ERROR") from None

    def _checkpoint(self, stage: str) -> None:
        """Точка проверки атомарности; транзакцией управляет вызывающий код."""

    def append_in_transaction(
        self,
        connection: sqlite3.Connection,
        binding: JournalBinding,
        record: JournalRecord,
        *,
        path: Path | None = None,
    ) -> bool:
        """Безопасная raw-операция; владелец откатывает транзакцию при любом отказе."""
        try:
            return self._append_in_transaction(connection, binding, record, path=path)
        except sqlite3.Error:
            raise RawJournalError("DB_ERROR") from None

    def _append_in_transaction(
        self,
        connection: sqlite3.Connection,
        binding: JournalBinding,
        record: JournalRecord,
        *,
        path: Path | None = None,
    ) -> bool:
        """Пишет в активную транзакцию этого repository без управления её завершением.

        При исключении владелец обязан откатить всю транзакцию. Чужой connection
        запрещён даже для той же БД: checkpoint и счётчики читаются через self.db.
        """
        if connection is not self.db or not connection.in_transaction:
            raise RawJournalError("INVALID_TRANSACTION")
        owner = connection.execute("SELECT value FROM metadata WHERE key='agent_id'").fetchone()
        if owner != (self.agent_id,):
            raise RawJournalError("OWNER_MISMATCH")
        if (
            record.offset < 0
            or record.next_offset != record.offset + len(record.raw)
            or not record.raw.endswith(b"\n")
            or (record.offset == 0) != (record.kind == "header")
        ):
            raise RawJournalError("INVALID_POSITION")
        key = self.source_key(binding)
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
        if self.db.execute(
            "SELECT 1 FROM low_boundary_pins WHERE source_key=? AND journal_id=?",
            (key, record.journal_id),
        ).fetchone():
            raise RawJournalError("GENERATION_SEALED")
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
        used = int(self.db.execute("SELECT bytes FROM low_storage_usage").fetchone()[0])
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
        self._checkpoint("raw_inserted")
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
                "UPDATE low_raw_generations SET next_offset=? WHERE source_key=? AND journal_id=?",
                (record.next_offset, key, record.journal_id),
            )
        self._checkpoint("raw_checkpoint")
        self.db.execute(
            "UPDATE low_storage_usage SET bytes=bytes+? WHERE singleton=1", (len(record.raw),)
        )
        self.db.execute(
            "UPDATE low_raw_usage SET bytes=bytes+? WHERE singleton=1", (len(record.raw),)
        )
        self._remember(key, path, record)
        return True
