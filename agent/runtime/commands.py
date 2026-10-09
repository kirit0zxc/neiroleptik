"""Транзакционный журнал команд одного слота без исполнения действий драйвера."""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from os import PathLike
from uuid import UUID

MAX_JSON_BYTES = 65_536
MAX_JSON_DEPTH = 32
MAX_GENERATION = 2**63 - 1


class CommandAction(StrEnum):
    START = "start"
    STOP = "stop"
    RESET = "reset"


class CommandStatus(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class JournalErrorCode(StrEnum):
    COMMAND_CONFLICT = "COMMAND_CONFLICT"
    STALE_GENERATION = "STALE_GENERATION"
    BUSY = "BUSY"
    UNKNOWN_COMMAND = "UNKNOWN_COMMAND"
    RESULT_CONFLICT = "RESULT_CONFLICT"


class JournalError(Exception):
    """Ожидаемый отказ журнала; code предназначен для локальной обработки."""

    def __init__(self, code: JournalErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True)
class Command:
    command_id: str
    action: CommandAction
    expected_generation: int
    payload: object = field(default_factory=dict)


@dataclass(frozen=True)
class RuntimeState:
    generation: int
    instance_id: str | None


@dataclass(frozen=True)
class CommandRecord:
    """Неизменяемый снимок: JSON хранится строкой без разделяемых mutable-объектов."""

    command_id: str
    action: CommandAction
    expected_generation: int
    payload_json: str
    status: CommandStatus
    result_json: str | None
    generation: int
    instance_id: str | None


def _uuid(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("UUID должен быть строкой")
    try:
        return str(UUID(value))
    except (ValueError, AttributeError) as exc:
        raise ValueError("Некорректный UUID") from exc


def _generation(value: int) -> int:
    if type(value) is not int or not 0 <= value <= MAX_GENERATION:
        raise ValueError("generation должен быть целым от 0 до 2**63 - 1")
    return value


def _canonical_json(value: object) -> str:
    """Ограничиваем также дерево до сериализации, включая циклы и глубину."""
    remaining = MAX_JSON_BYTES

    def validate(item: object, depth: int) -> None:
        nonlocal remaining
        remaining -= 1
        if depth > MAX_JSON_DEPTH or remaining < 0:
            raise ValueError("JSON превышает ограничения размера или глубины")
        if item is None or type(item) in (bool, int):
            return
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError("JSON не допускает NaN и Infinity")
            return
        if isinstance(item, str):
            try:
                remaining -= len(item.encode("utf-8"))
            except UnicodeError as exc:
                raise ValueError("JSON должен содержать корректный Unicode") from exc
            if remaining < 0:
                raise ValueError("JSON превышает ограничение размера")
            return
        if isinstance(item, list):
            for child in item:
                validate(child, depth + 1)
            return
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise ValueError("Ключи JSON должны быть строками")
                validate(key, depth + 1)
                validate(child, depth + 1)
            return
        raise ValueError("Допустимы только значения JSON")

    validate(value, 0)
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    if len(encoded.encode("utf-8")) > MAX_JSON_BYTES:
        raise ValueError("JSON превышает ограничение размера")
    return encoded


class CommandJournal:
    """Один объект на поток; конкурирующие объекты согласуются через SQLite.

    Путь задаёт вызывающий код. Каталоги не создаются автоматически. FULL и
    commit до возврата обеспечивают обычные гарантии SQLite на локальном диске;
    журнал не подтверждает выполнение VM и не заменяет reconcile драйвера.
    """

    def __init__(self, path: str | PathLike[str]) -> None:
        self._db = sqlite3.connect(path, isolation_level=None, timeout=10)
        self._db.row_factory = sqlite3.Row
        try:
            self._db.execute("PRAGMA synchronous = FULL")
            with self._transaction():
                self._db.execute("""
                    CREATE TABLE IF NOT EXISTS runtime_state (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        generation INTEGER NOT NULL CHECK (generation >= 0),
                        instance_id TEXT
                    )
                """)
                self._db.execute("""
                    INSERT OR IGNORE INTO runtime_state VALUES (1, 0, NULL)
                """)
                self._db.execute("""
                    CREATE TABLE IF NOT EXISTS commands (
                        command_id TEXT PRIMARY KEY,
                        action TEXT NOT NULL,
                        expected_generation INTEGER NOT NULL,
                        payload_json TEXT NOT NULL,
                        status TEXT NOT NULL,
                        result_json TEXT,
                        generation INTEGER NOT NULL,
                        instance_id TEXT,
                        completion_instance_id TEXT
                    )
                """)
                self._db.execute("""
                    CREATE UNIQUE INDEX IF NOT EXISTS one_pending_command
                    ON commands(status) WHERE status = 'pending'
                """)
        except BaseException:
            self._db.close()
            raise

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """Блокировку берём до чтения, исключая гонку проверки и записи."""
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._db.commit()
        except BaseException:
            self._db.rollback()
            raise

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> CommandJournal:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    @property
    def state(self) -> RuntimeState:
        row = self._db.execute("SELECT generation, instance_id FROM runtime_state").fetchone()
        return RuntimeState(row["generation"], row["instance_id"])

    @staticmethod
    def _record(row: sqlite3.Row) -> CommandRecord:
        return CommandRecord(
            command_id=row["command_id"],
            action=CommandAction(row["action"]),
            expected_generation=row["expected_generation"],
            payload_json=row["payload_json"],
            status=CommandStatus(row["status"]),
            result_json=row["result_json"],
            generation=row["generation"],
            instance_id=row["instance_id"],
        )

    def get(self, command_id: str) -> CommandRecord | None:
        row = self._db.execute(
            "SELECT * FROM commands WHERE command_id = ?", (_uuid(command_id),)
        ).fetchone()
        return None if row is None else self._record(row)

    @property
    def pending(self) -> CommandRecord | None:
        """Возвращаем восстановленную операцию; автоматически её не исполняем."""
        row = self._db.execute("SELECT * FROM commands WHERE status = 'pending'").fetchone()
        return None if row is None else self._record(row)

    def accept(self, command: Command) -> CommandRecord:
        command_id = _uuid(command.command_id)
        action = CommandAction(command.action)
        generation = _generation(command.expected_generation)
        payload = _canonical_json(command.payload)
        with self._transaction():
            existing = self.get(command_id)
            if existing is not None:
                if (existing.action, existing.expected_generation, existing.payload_json) != (
                    action,
                    generation,
                    payload,
                ):
                    raise JournalError(JournalErrorCode.COMMAND_CONFLICT)
                return existing
            state = self.state
            if generation != state.generation:
                raise JournalError(JournalErrorCode.STALE_GENERATION)
            if self.pending is not None:
                raise JournalError(JournalErrorCode.BUSY)
            self._db.execute(
                """
                INSERT INTO commands
                (command_id, action, expected_generation, payload_json, status,
                 generation, instance_id) VALUES (?, ?, ?, ?, 'pending', ?, ?)
            """,
                (command_id, action.value, generation, payload, generation, state.instance_id),
            )
            return CommandRecord(
                command_id,
                action,
                generation,
                payload,
                CommandStatus.PENDING,
                None,
                generation,
                state.instance_id,
            )

    def finish(
        self,
        command_id: str,
        status: CommandStatus,
        *,
        result: object = None,
        instance_id: str | None = None,
    ) -> CommandRecord:
        """Фиксируем исход драйвера атомарно с метаданными слота.

        Для успешного start/reset драйвер передаёт новый UUID и повторяет его при
        потере ответа. Для stop/ошибки новый UUID запрещён: состояние сохраняется.
        """
        command_id = _uuid(command_id)
        status = CommandStatus(status)
        if status is CommandStatus.PENDING:
            raise ValueError("Завершение должно быть succeeded или failed")
        result_json = _canonical_json(result)
        new_instance = None if instance_id is None else _uuid(instance_id)
        with self._transaction():
            row = self._db.execute(
                "SELECT * FROM commands WHERE command_id = ?", (command_id,)
            ).fetchone()
            if row is None:
                raise JournalError(JournalErrorCode.UNKNOWN_COMMAND)
            record = self._record(row)
            if record.status is not CommandStatus.PENDING:
                if (record.status, record.result_json, row["completion_instance_id"]) != (
                    status,
                    result_json,
                    new_instance,
                ):
                    raise JournalError(JournalErrorCode.RESULT_CONFLICT)
                return record
            state = self.state
            creates_instance = status is CommandStatus.SUCCEEDED and record.action in (
                CommandAction.START,
                CommandAction.RESET,
            )
            if creates_instance:
                if new_instance is None:
                    raise ValueError("Успешный start/reset требует нового instance_id")
                reused = self._db.execute(
                    "SELECT 1 FROM commands WHERE completion_instance_id = ?",
                    (new_instance,),
                ).fetchone()
                if reused is not None or new_instance == state.instance_id:
                    raise ValueError("instance_id уже использовался")
                state = RuntimeState(_generation(state.generation + 1), new_instance)
            elif new_instance is not None:
                raise ValueError("stop и ошибка не создают instance_id")
            self._db.execute(
                """
                UPDATE commands SET status = ?, result_json = ?, generation = ?,
                    instance_id = ?, completion_instance_id = ? WHERE command_id = ?
            """,
                (
                    status.value,
                    result_json,
                    state.generation,
                    state.instance_id,
                    new_instance,
                    command_id,
                ),
            )
            self._db.execute(
                "UPDATE runtime_state SET generation = ?, instance_id = ?",
                (state.generation, state.instance_id),
            )
            return CommandRecord(
                record.command_id,
                record.action,
                record.expected_generation,
                record.payload_json,
                status,
                result_json,
                state.generation,
                state.instance_id,
            )
