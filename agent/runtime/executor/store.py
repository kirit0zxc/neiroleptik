"""Устойчивое состояние исполнителя: lease закрыт, конфигурация профиля не хранится."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

SCHEMA_VERSION = 1
APPLICATION_ID = 0x48464558
# Определение индекса с нормализованными пробелами: так оно хранится в sqlite_master.
_OUTSTANDING_INDEX = (
    "CREATE UNIQUE INDEX one_outstanding_delivery ON commands ((1)) "
    "WHERE acked = 0 AND phase <> 'abandoned'"
)
MAX_GENERATION = 2**63 - 1
Phase = Literal["prepared", "executing", "completed", "abandoned"]


class StoreError(Exception):
    """Ожидаемый отказ без содержимого команды, результата или lease token."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class StoredCommand:
    """Снимок безопасных метаданных; изменяемый результат считывается заново из JSON."""

    command_id: str
    deployment_id: str
    action: str
    generation: int
    expected_generation: int
    revision: int
    expected_instance_id: str | None
    reconcile: bool
    fingerprint: str
    attempt_instance_id: str | None
    lease_token: str = field(repr=False)
    phase: Phase = "prepared"
    result: dict[str, object] | None = None
    acked: bool = False


@dataclass(frozen=True)
class DeploymentHistory:
    """История применения переживает остановку и очищенный DriverStatus.revision."""

    generation: int = 0
    instance_id: str | None = None
    applied_revision: int | None = None


def _uuid(value: object, code: str = "INVALID_COMMAND") -> str:
    if not isinstance(value, str):
        raise StoreError(code)
    try:
        return str(UUID(value))
    except (ValueError, AttributeError):
        raise StoreError(code) from None


def _optional_uuid(value: object, code: str) -> str | None:
    return None if value is None else _uuid(value, code)


def _integer(value: object, minimum: int, code: str) -> int:
    if type(value) is not int or not minimum <= value <= MAX_GENERATION:
        raise StoreError(code)
    return value


def _lease(value: object) -> str:
    # Формат HTTP-токена проверяет транспорт. Здесь остаётся только ограниченная строка.
    if not isinstance(value, str) or not 1 <= len(value) <= 256:
        raise StoreError("INVALID_COMMAND")
    return value


class ExecutionStore:
    """Один владелец каталога и одна незавершённая доставка на локальном диске.

    Замок удерживается до close, включая интервалы между SQLite-транзакциями.
    Физическая Docker-операция остаётся ответственностью исполнителя; executing
    никогда не превращается в prepared автоматически после перезапуска.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(os.path.abspath(path))
        self._db: sqlite3.Connection | None = None
        self._lock_fd: int | None = None
        try:
            self._prepare_directory()
            self._acquire_lock()
            self._prepare_file(self.path)
            for suffix in ("-journal", "-wal", "-shm"):
                self._check_file(Path(str(self.path) + suffix), missing_allowed=True)
            self._db = sqlite3.connect(self.path, isolation_level=None, timeout=5)
            self._db.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA synchronous = FULL")
            self._initialize_schema()
            # Постоянный режим чужой/старой SQLite нельзя менять до проверки схемы.
            self._connection.execute("PRAGMA journal_mode = DELETE")
            self._sync_directory()
        except BaseException as error:
            self.close()
            if isinstance(error, (OSError, sqlite3.Error)):
                raise StoreError("STORE_IO") from None
            raise

    @property
    def _connection(self) -> sqlite3.Connection:
        if self._db is None:
            raise StoreError("CLOSED")
        return self._db

    def _prepare_directory(self) -> None:
        """Не следуем symlink/reparse point, включая уже существующих предков."""
        for directory in reversed((self.path.parent, *self.path.parent.parents)):
            try:
                info = directory.lstat()
            except FileNotFoundError:
                continue
            self._require_regular_kind(info, directory=True)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.path.parent.lstat()
        self._require_regular_kind(info, directory=True)
        self._require_private(info, directory=True)

    @staticmethod
    def _require_regular_kind(info: os.stat_result, *, directory: bool) -> None:
        reparse = getattr(info, "st_file_attributes", 0) & getattr(
            stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0
        )
        expected = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
        if stat.S_ISLNK(info.st_mode) or reparse or not expected:
            raise StoreError("UNSAFE_STATE_PATH")
        if not directory and info.st_nlink != 1:
            raise StoreError("UNSAFE_STATE_PATH")

    @staticmethod
    def _require_private(info: os.stat_result, *, directory: bool) -> None:
        # На Windows доступ задаётся ACL оператора; POSIX mode не описывает эту защиту.
        if sys.platform != "win32":
            required = 0o700 if directory else 0o600
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != required:
                raise StoreError("UNSAFE_STATE_PATH")

    def _check_file(self, path: Path, *, missing_allowed: bool) -> None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            if missing_allowed:
                return
            raise
        self._require_regular_kind(info, directory=False)
        self._require_private(info, directory=False)

    def _prepare_file(self, path: Path) -> None:
        try:
            flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError:
            self._check_file(path, missing_allowed=False)
        else:
            os.close(descriptor)

    def _acquire_lock(self) -> None:
        """Файл замка не удаляется: удаление позволило бы двум владельцам иметь разные inode."""
        path = self.path.parent / "state.lock"
        self._prepare_file(path)
        descriptor = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
        self._lock_fd = descriptor
        self._require_regular_kind(os.fstat(descriptor), directory=False)
        self._require_private(os.fstat(descriptor), directory=False)
        try:
            if sys.platform == "win32":
                import msvcrt

                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"\0")
                    os.fsync(descriptor)
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(descriptor)
            self._lock_fd = None
            raise StoreError("STORE_LOCKED") from None

    def _sync_directory(self) -> None:
        if sys.platform != "win32":
            descriptor = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        connection = self._connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield
            connection.commit()
        except BaseException as error:
            with suppress(sqlite3.Error):
                connection.rollback()
            if isinstance(error, sqlite3.Error):
                raise StoreError("STORE_IO") from None
            raise

    def _initialize_schema(self) -> None:
        with self._transaction():
            version = self._connection.execute("PRAGMA user_version").fetchone()[0]
            application = self._connection.execute("PRAGMA application_id").fetchone()[0]
            tables = {
                row[0]
                for row in self._connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            if version == 0 and not tables and application == 0:
                self._connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                self._connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
                self._connection.execute("""
                    CREATE TABLE deployment_history (
                        deployment_id TEXT PRIMARY KEY,
                        generation INTEGER NOT NULL CHECK (generation >= 0),
                        instance_id TEXT,
                        applied_revision INTEGER
                    )
                """)
                self._connection.execute("""
                    CREATE TABLE commands (
                        command_id TEXT PRIMARY KEY,
                        deployment_id TEXT NOT NULL,
                        action TEXT NOT NULL,
                        generation INTEGER NOT NULL,
                        expected_generation INTEGER NOT NULL,
                        revision INTEGER NOT NULL,
                        expected_instance_id TEXT,
                        reconcile INTEGER NOT NULL,
                        fingerprint TEXT NOT NULL,
                        attempt_instance_id TEXT,
                        lease_token TEXT NOT NULL,
                        phase TEXT NOT NULL
                            CHECK (phase IN ('prepared', 'executing', 'completed', 'abandoned')),
                        result_json TEXT,
                        acked INTEGER NOT NULL DEFAULT 0 CHECK (acked IN (0, 1))
                    )
                """)
                self._connection.execute(_OUTSTANDING_INDEX)
            elif (
                version != SCHEMA_VERSION
                or application != APPLICATION_ID
                or tables != {"commands", "deployment_history"}
            ):
                raise StoreError("SCHEMA_MISMATCH")
            required = {
                "command_id",
                "deployment_id",
                "action",
                "generation",
                "expected_generation",
                "revision",
                "expected_instance_id",
                "reconcile",
                "fingerprint",
                "attempt_instance_id",
                "lease_token",
                "phase",
                "result_json",
                "acked",
            }
            actual = {row[1] for row in self._connection.execute("PRAGMA table_info(commands)")}
            history_columns = {
                row[1] for row in self._connection.execute("PRAGMA table_info(deployment_history)")
            }
            if actual != required or history_columns != {
                "deployment_id",
                "generation",
                "instance_id",
                "applied_revision",
            }:
                raise StoreError("SCHEMA_MISMATCH")
            # Частичный уникальный индекс — единственная защита от двух недоставленных
            # результатов одновременно; без него очередь ACK перестаёт быть одиночной.
            index = self._connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
                ("one_outstanding_delivery",),
            ).fetchone()
            if index is None or " ".join(str(index[0]).split()) != _OUTSTANDING_INDEX:
                raise StoreError("SCHEMA_MISMATCH")

    def close(self) -> None:
        """Освобождает SQLite и межпроцессный замок; повторное закрытие допустимо."""
        try:
            if self._db is not None:
                self._db.close()
        finally:
            self._db = None
            if self._lock_fd is not None:
                descriptor, self._lock_fd = self._lock_fd, None
                try:
                    if sys.platform == "win32":
                        import msvcrt

                        os.lseek(descriptor, 0, os.SEEK_SET)
                        with suppress(OSError):
                            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)

    def __enter__(self) -> ExecutionStore:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _query(self, sql: str, parameters: tuple[object, ...] = ()) -> sqlite3.Row | None:
        try:
            row: sqlite3.Row | None = self._connection.execute(sql, parameters).fetchone()
            return row
        except sqlite3.Error:
            raise StoreError("STORE_IO") from None

    @staticmethod
    def _record(row: sqlite3.Row) -> StoredCommand:
        try:
            result = None if row["result_json"] is None else json.loads(row["result_json"])
            if result is not None and not isinstance(result, dict):
                raise StoreError("SCHEMA_MISMATCH")
            return StoredCommand(
                command_id=row["command_id"],
                deployment_id=row["deployment_id"],
                action=row["action"],
                generation=row["generation"],
                expected_generation=row["expected_generation"],
                revision=row["revision"],
                expected_instance_id=row["expected_instance_id"],
                reconcile=bool(row["reconcile"]),
                fingerprint=row["fingerprint"],
                attempt_instance_id=row["attempt_instance_id"],
                lease_token=row["lease_token"],
                phase=row["phase"],
                result=result,
                acked=bool(row["acked"]),
            )
        except (ValueError, KeyError, TypeError):
            raise StoreError("SCHEMA_MISMATCH") from None

    def get(self, command_id: str) -> StoredCommand | None:
        """Поиск повтора выполняется до проверки текущего поколения."""
        row = self._query("SELECT * FROM commands WHERE command_id = ?", (_uuid(command_id),))
        return None if row is None else self._record(row)

    def pending(self) -> StoredCommand | None:
        """Завершённый результат до ACK также занимает единственный слот доставки."""
        row = self._query(
            "SELECT * FROM commands WHERE acked = 0 AND phase <> 'abandoned' ORDER BY rowid LIMIT 1"
        )
        return None if row is None else self._record(row)

    def has_history(self, deployment_id: str) -> bool:
        return (
            self._query(
                "SELECT 1 FROM deployment_history WHERE deployment_id = ?", (_uuid(deployment_id),)
            )
            is not None
        )

    def history(self, deployment_id: str) -> DeploymentHistory:
        """Отсутствующая история отличается от поколения 0 через has_history."""
        row = self._query(
            "SELECT * FROM deployment_history WHERE deployment_id = ?", (_uuid(deployment_id),)
        )
        if row is None:
            return DeploymentHistory()
        return DeploymentHistory(row["generation"], row["instance_id"], row["applied_revision"])

    def prepare_telemetry_path(self) -> Path:
        """Spool наследует приватный каталог и замок именно этого state-файла."""
        path = self.path.with_name(self.path.name + ".telemetry.sqlite3")
        self._prepare_file(path)
        for suffix in ("-wal", "-shm", "-journal"):
            self._check_file(Path(str(path) + suffix), missing_allowed=True)
        return path

    def telemetry_bindings(self) -> tuple[tuple[str, str, int], ...]:
        """Экспорт только подтверждённых start; попытка и header не являются доверием."""
        try:
            rows = self._connection.execute(
                "SELECT deployment_id, result_json FROM commands "
                "WHERE action IN ('start', 'reset') AND phase = 'completed' AND acked = 1 "
                "ORDER BY rowid"
            ).fetchall()
        except sqlite3.Error:
            raise StoreError("STORE_IO") from None
        bindings: dict[tuple[str, str], int] = {}
        for deployment, encoded in rows:
            try:
                result = self._result(json.loads(encoded))
            except (ValueError, TypeError):
                raise StoreError("SCHEMA_MISMATCH") from None
            if result["outcome"] == "succeeded" and result["runtime_state"] == "running":
                instance = _uuid(result["instance_id"], "SCHEMA_MISMATCH")
                revision = _integer(result["applied_revision"], 1, "SCHEMA_MISMATCH")
                key = (_uuid(deployment, "SCHEMA_MISMATCH"), instance)
                if key in bindings and bindings[key] != revision:
                    raise StoreError("SCHEMA_MISMATCH")
                bindings[key] = revision
        return tuple(
            (deployment, instance, rev) for (deployment, instance), rev in bindings.items()
        )

    def known_instances(self, deployment_id: str) -> set[str]:
        """Все UUID экземпляров назначения, созданные агентом или сообщённые центром.

        Источники: попытки start, ожидаемые центром экземпляры команд, итоговые ACK и
        текущая история. Контейнер любой из них остаётся управляемым для stop и start,
        какой бы UUID ни попал в ACK неудачной операции; чужие назначения и уровни
        по-прежнему отсекают метки контейнера.
        """
        deployment_id = _uuid(deployment_id)
        try:
            rows = self._connection.execute(
                "SELECT attempt_instance_id, expected_instance_id, result_json "
                "FROM commands WHERE deployment_id = ?",
                (deployment_id,),
            ).fetchall()
        except sqlite3.Error:
            raise StoreError("STORE_IO") from None
        known: set[str] = set()
        for attempt, expected, encoded in rows:
            known.update(value for value in (attempt, expected) if value is not None)
            if encoded is not None:
                try:
                    instance = json.loads(encoded).get("instance_id")
                except (ValueError, AttributeError):
                    raise StoreError("SCHEMA_MISMATCH") from None
                if instance is not None:
                    known.add(_uuid(instance, "SCHEMA_MISMATCH"))
        current = self.history(deployment_id).instance_id
        if current is not None:
            known.add(current)
        return known

    def seed_history(
        self,
        deployment_id: str,
        generation: int,
        instance_id: str | None,
        applied_revision: int | None,
    ) -> DeploymentHistory:
        """Первую историю берём из центра, не выводя revision из идентичности экземпляра."""
        deployment_id = _uuid(deployment_id)
        history = DeploymentHistory(
            _integer(generation, 0, "INVALID_COMMAND"),
            _optional_uuid(instance_id, "INVALID_COMMAND"),
            None if applied_revision is None else _integer(applied_revision, 1, "INVALID_COMMAND"),
        )
        if history.applied_revision is not None and history.instance_id is None:
            raise StoreError("INVALID_COMMAND")
        with self._transaction():
            if self.has_history(deployment_id):
                if self.history(deployment_id) != history:
                    raise StoreError("HISTORY_CONFLICT")
                return history
            self._write_history(deployment_id, history)
        return history

    @staticmethod
    def _metadata(metadata: dict[str, object]) -> dict[str, object]:
        """Белый список намеренно игнорирует config, profile и прочие сетевые поля."""
        try:
            generation = _integer(metadata["generation"], 1, "INVALID_COMMAND")
            expected = _integer(metadata["expected_generation"], 0, "INVALID_COMMAND")
            action = metadata["action"]
            reconcile = metadata["reconcile"]
            if action not in ("start", "stop", "reset") or type(reconcile) is not bool:
                raise StoreError("INVALID_COMMAND")
            if generation != expected + 1:
                raise StoreError("INVALID_COMMAND")
            return {
                "command_id": _uuid(metadata["command_id"]),
                "deployment_id": _uuid(metadata["deployment_id"]),
                "action": action,
                "generation": generation,
                "expected_generation": expected,
                "revision": _integer(metadata["revision"], 1, "INVALID_COMMAND"),
                "expected_instance_id": _optional_uuid(
                    metadata["expected_instance_id"], "INVALID_COMMAND"
                ),
                "reconcile": reconcile,
            }
        except (KeyError, TypeError):
            raise StoreError("INVALID_COMMAND") from None

    def accept(
        self,
        metadata: dict[str, object],
        fingerprint: str,
        lease_token: str,
    ) -> StoredCommand:
        """Сначала фиксируем попытку start; новая доставка никогда не повторяет известный ID."""
        safe = self._metadata(metadata)
        if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise StoreError("INVALID_COMMAND")
        lease_token = _lease(lease_token)
        command_id = str(safe["command_id"])
        with self._transaction():
            existing = self.get(command_id)
            if existing is not None:
                if existing.fingerprint != fingerprint or any(
                    getattr(existing, key) != value for key, value in safe.items()
                ):
                    raise StoreError("COMMAND_CONFLICT")
                return existing
            history = self.history(str(safe["deployment_id"]))
            if _integer(safe["expected_generation"], 0, "INVALID_COMMAND") < history.generation:
                raise StoreError("STALE_GENERATION")
            if self.pending() is not None:
                raise StoreError("BUSY")
            attempt = str(uuid4()) if safe["action"] in ("start", "reset") else None
            self._connection.execute(
                """
                INSERT INTO commands
                (command_id, deployment_id, action, generation, expected_generation,
                 revision, expected_instance_id, reconcile, fingerprint,
                 attempt_instance_id, lease_token, phase)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'prepared')
                """,
                (*safe.values(), fingerprint, attempt, lease_token),
            )
            return self._known(command_id)

    def _known(self, command_id: str) -> StoredCommand:
        record = self.get(command_id)
        if record is None:
            raise StoreError("UNKNOWN_COMMAND")
        return record

    def update_lease(self, command_id: str, lease_token: str) -> StoredCommand:
        """Новый token не меняет fingerprint и уже сохранённый итоговый ACK."""
        token = _lease(lease_token)
        with self._transaction():
            record = self._known(command_id)
            self._connection.execute(
                "UPDATE commands SET lease_token = ? WHERE command_id = ?",
                (token, record.command_id),
            )
            return self._known(record.command_id)

    def mark_executing(self, command_id: str) -> StoredCommand:
        """Commit перед первой мутацией отделяет безопасный prepared от неоднозначной попытки."""
        with self._transaction():
            record = self._known(command_id)
            if record.phase not in ("prepared", "executing"):
                raise StoreError("INVALID_PHASE")
            self._connection.execute(
                "UPDATE commands SET phase = 'executing' WHERE command_id = ?", (record.command_id,)
            )
            return self._known(record.command_id)

    @staticmethod
    def _result(result: dict[str, object]) -> dict[str, object]:
        required = {
            "deployment_id",
            "generation",
            "applied_revision",
            "instance_id",
            "outcome",
            "runtime_state",
            "error",
        }
        if set(result) != required:
            raise StoreError("INVALID_RESULT")
        normalized = dict(result)
        normalized["deployment_id"] = _uuid(result["deployment_id"], "INVALID_RESULT")
        normalized["generation"] = _integer(result["generation"], 1, "INVALID_RESULT")
        normalized["instance_id"] = _optional_uuid(result["instance_id"], "INVALID_RESULT")
        revision = result["applied_revision"]
        normalized["applied_revision"] = (
            None if revision is None else _integer(revision, 1, "INVALID_RESULT")
        )
        outcome, runtime, error = result["outcome"], result["runtime_state"], result["error"]
        if outcome not in ("succeeded", "failed") or runtime not in (
            "running",
            "stopped",
            "failed",
        ):
            raise StoreError("INVALID_RESULT")
        if outcome == "failed":
            if not isinstance(error, dict) or set(error) != {"code", "message"}:
                raise StoreError("INVALID_RESULT")
            code, message = error["code"], error["message"]
            if (
                not isinstance(code, str)
                or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code)
                or not isinstance(message, str)
                or not 1 <= len(message) <= 500
            ):
                raise StoreError("INVALID_RESULT")
        elif error is not None:
            raise StoreError("INVALID_RESULT")
        if revision is not None and normalized["instance_id"] is None:
            raise StoreError("INVALID_RESULT")
        if runtime == "running" and (revision is None or normalized["instance_id"] is None):
            raise StoreError("INVALID_RESULT")
        return normalized

    def complete(self, command_id: str, result: dict[str, object]) -> StoredCommand:
        """Результат и история меняются одной транзакцией до любой сетевой отправки ACK."""
        normalized = self._result(result)
        try:
            encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":"), allow_nan=False)
        except (ValueError, TypeError, UnicodeError):
            raise StoreError("INVALID_RESULT") from None
        with self._transaction():
            record = self._known(command_id)
            if record.result is not None:
                if record.result != normalized:
                    raise StoreError("RESULT_CONFLICT")
                return record
            if record.phase == "abandoned":
                raise StoreError("INVALID_PHASE")
            if (
                normalized["deployment_id"] != record.deployment_id
                or normalized["generation"] != record.generation
                or normalized["applied_revision"] not in (None, record.revision)
            ):
                raise StoreError("INVALID_RESULT")
            history = self.history(record.deployment_id)
            if history.generation >= record.generation:
                raise StoreError("STALE_GENERATION")
            if (
                record.action == "stop"
                and not record.reconcile
                and (
                    normalized["instance_id"] != history.instance_id
                    or normalized["applied_revision"] != history.applied_revision
                )
            ):
                raise StoreError("INVALID_RESULT")
            if normalized["outcome"] == "succeeded":
                if record.action in ("start", "reset") and (
                    normalized["runtime_state"] != "running"
                    or normalized["instance_id"] != record.attempt_instance_id
                    or normalized["applied_revision"] != record.revision
                ):
                    raise StoreError("INVALID_RESULT")
                if record.action == "stop" and normalized["runtime_state"] != "stopped":
                    raise StoreError("INVALID_RESULT")
            elif (
                record.action in ("start", "reset")
                and normalized["runtime_state"] == "running"
                and (normalized["instance_id"] != record.expected_instance_id)
            ):
                raise StoreError("INVALID_RESULT")
            self._connection.execute(
                "UPDATE commands SET phase = 'completed', result_json = ? WHERE command_id = ?",
                (encoded, record.command_id),
            )
            self._write_history(
                record.deployment_id,
                DeploymentHistory(
                    record.generation,
                    _optional_uuid(normalized["instance_id"], "INVALID_RESULT"),
                    None
                    if normalized["applied_revision"] is None
                    else _integer(normalized["applied_revision"], 1, "INVALID_RESULT"),
                ),
            )
            return self._known(record.command_id)

    def _write_history(self, deployment_id: str, history: DeploymentHistory) -> None:
        self._connection.execute(
            """
            INSERT INTO deployment_history VALUES (?, ?, ?, ?)
            ON CONFLICT(deployment_id) DO UPDATE SET
                generation = excluded.generation, instance_id = excluded.instance_id,
                applied_revision = excluded.applied_revision
            """,
            (deployment_id, history.generation, history.instance_id, history.applied_revision),
        )

    def acknowledge(self, command_id: str) -> StoredCommand:
        """Освобождаем очередь только после успешного ответа ACK центра."""
        with self._transaction():
            record = self._known(command_id)
            if record.phase != "completed" or record.result is None:
                raise StoreError("INVALID_PHASE")
            self._connection.execute(
                "UPDATE commands SET acked = 1 WHERE command_id = ?", (record.command_id,)
            )
            return self._known(record.command_id)

    def abandon(self, command_id: str) -> StoredCommand:
        """Истёкшая команда не мешает отдельному stop, но остаётся в истории навсегда."""
        with self._transaction():
            record = self._known(command_id)
            self._connection.execute(
                "UPDATE commands SET phase = 'abandoned' WHERE command_id = ?", (record.command_id,)
            )
            history = self.history(record.deployment_id)
            if history.generation < record.generation:
                self._write_history(
                    record.deployment_id,
                    DeploymentHistory(
                        record.generation, history.instance_id, history.applied_revision
                    ),
                )
            return self._known(record.command_id)
