"""Долговечная runtime registry границы процесса Low (§8.4 предложения).

Registry — единственный источник идентичности перехода: launch intents и их состояния,
полные container ID, идентичность Engine/daemon, выделения первого поколения, перечень
снимков, сожжённые идентичности и **точные канонические байты** выданных anchor и proof.
Недоверенные поля опубликованного документа её не заменяют.

Файл базы лежит в приватном каталоге состояния runtime, вне корней публикации и
недоступен collector. Каждое состояние фиксируется транзакцией **до** соответствующего
внешнего действия, поэтому после сбоя продолжение определяется записанным состоянием,
а не наблюдением Docker. Байты документа собираются один раз при commit и далее только
повторяются: повторная публикация никогда не пересобирает поля.

Проверки пути и межпроцессный замок повторяют подход `executor/store.py`. Хранилища
намеренно разные: у исполнителя свои инварианты (одна недоставленная доставка, lease),
и объединение схем связало бы рестарт исполнителя с историей переходов.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast
from uuid import UUID

from agent.runtime.boundary.documents import (
    MAX_SAFE_INTEGER,
    FirstJournal,
    NextWriter,
    PreviousWriter,
    ProcessBoundary,
    SnapshotEntry,
    WriterAnchor,
    WriterBinding,
    utc_timestamp,
)
from agent.runtime.boundary.errors import BoundaryError, BoundaryErrorCode

SCHEMA_VERSION = 1
# Отдельный application_id: файл исполнителя и файл границы процесса не взаимозаменяемы.
APPLICATION_ID = 0x48464250

IntentKind = Literal["anchor", "boundary"]
IntentState = Literal[
    "intent",
    "fenced",
    "snapshots_published",
    "next_created",
    "next_started",
    "next_bound",
    "committed",
    "exported",
    "failed_after_launch",
]
# Состояния, после которых intent больше не продолжается автоматически (§8.4).
TERMINAL_STATES = ("exported", "failed_after_launch")
_STATES = (
    "intent",
    "fenced",
    "snapshots_published",
    "next_created",
    "next_started",
    "next_bound",
    "committed",
    "exported",
    "failed_after_launch",
)
_OUTSTANDING_INDEX = (
    "CREATE UNIQUE INDEX one_outstanding_intent ON intents "
    "(agent_id, deployment_id, instance_id) "
    "WHERE state NOT IN ('exported', 'failed_after_launch')"
)
_TABLES = {"bindings", "intents", "snapshots", "documents", "burned"}


def _unavailable(detail: str) -> BoundaryError:
    """Registry недоступна: новые документы binding не выдаются (§8.4)."""
    return BoundaryError(BoundaryErrorCode.REGISTRY_UNAVAILABLE, detail)


def _uuid(value: object) -> str:
    if not isinstance(value, str):
        raise _unavailable("ожидается UUID")
    try:
        return str(UUID(value))
    except (ValueError, AttributeError):
        raise _unavailable("ожидается UUID") from None


def _counter(value: object, minimum: int = 1) -> int:
    if type(value) is not int or not minimum <= value <= MAX_SAFE_INTEGER:
        raise _unavailable("ожидается целое в допустимых границах")
    return value


@dataclass(frozen=True)
class WriterChainHead:
    """Текущая привязанная цепочка binding: writer, его контейнер и первое поколение."""

    writer_id: str
    container_id: str
    first_journal_id: str
    first_journal_seq: int
    journal_max_bytes: int
    journal_quota_bytes: int


@dataclass(frozen=True)
class BindingState:
    """Состояние привязки: anchor, текущая цепочка и счётчик принятых переходов."""

    binding: WriterBinding
    anchor_id: str | None
    head: WriterChainHead | None
    engine_identity: str | None
    last_boundary_seq: int
    last_boundary_id: str | None
    blocked_reason: str

    @property
    def supported(self) -> bool:
        """Поддержка границы снимается только явной записью о неудаче после запуска."""
        return not self.blocked_reason


@dataclass(frozen=True)
class StoredIntent:
    """Launch intent, сохранённый до внешнего действия; поля заполняются по состояниям."""

    intent_id: str
    binding: WriterBinding
    kind: IntentKind
    state: IntentState
    writer_id: str
    first_journal_id: str
    journal_max_bytes: int
    journal_quota_bytes: int
    engine_identity: str
    created_at: str
    document_id: str
    container_id: str | None = None
    first_journal_seq: int | None = None
    previous_writer_id: str | None = None
    previous_container_id: str | None = None
    fenced_at: str | None = None
    header_sha256: str | None = None


@dataclass(frozen=True)
class StoredDocument:
    """Выданный документ: канонические байты и их SHA-256 неизменны после commit."""

    document_id: str
    kind: IntentKind
    canonical: bytes
    sha256: str
    published: bool


class BoundaryRegistry:
    """Один владелец каталога состояния и один незавершённый intent на binding."""

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
            self._connection.execute("PRAGMA journal_mode = DELETE")
            self._sync_directory()
        except BaseException as error:
            self.close()
            if isinstance(error, (OSError, sqlite3.Error)):
                raise _unavailable("каталог состояния недоступен") from None
            raise

    # --- жизненный цикл файла ---------------------------------------------------------

    @property
    def _connection(self) -> sqlite3.Connection:
        if self._db is None:
            raise _unavailable("registry закрыта")
        return self._db

    def _prepare_directory(self) -> None:
        """Приватный каталог состояния 0700 без переходов по ссылкам у предков."""
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
            raise _unavailable("небезопасный путь состояния")
        if not directory and info.st_nlink != 1:
            raise _unavailable("небезопасный путь состояния")

    @staticmethod
    def _require_private(info: os.stat_result, *, directory: bool) -> None:
        # На Windows доступ задаёт ACL оператора; POSIX mode эту защиту не описывает.
        if sys.platform != "win32":
            required = 0o700 if directory else 0o600
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != required:
                raise _unavailable("небезопасные права пути состояния")

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
        """Замок берётся до reconciliation и держится до close (§8.4)."""
        path = self.path.parent / "boundary.lock"
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
            raise _unavailable("registry уже занята другим процессом") from None

    def _sync_directory(self) -> None:
        if sys.platform != "win32":
            descriptor = os.open(self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
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
                raise _unavailable("транзакция registry не выполнена") from None
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
                self._create_schema()
            elif version != SCHEMA_VERSION or application != APPLICATION_ID or tables != _TABLES:
                raise _unavailable("схема registry не совпадает")
            index = self._connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
                ("one_outstanding_intent",),
            ).fetchone()
            if index is None or " ".join(str(index[0]).split()) != _OUTSTANDING_INDEX:
                raise _unavailable("схема registry не совпадает")

    def _create_schema(self) -> None:
        """Создаёт схему; счётчик переходов и сожжённые идентичности живут здесь же."""
        self._connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self._connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
        self._connection.execute("""
            CREATE TABLE bindings (
                agent_id TEXT NOT NULL,
                deployment_id TEXT NOT NULL,
                instance_id TEXT NOT NULL,
                profile_revision INTEGER NOT NULL,
                anchor_id TEXT,
                writer_id TEXT,
                container_id TEXT,
                first_journal_id TEXT,
                first_journal_seq INTEGER,
                journal_max_bytes INTEGER,
                journal_quota_bytes INTEGER,
                engine_identity TEXT,
                last_boundary_seq INTEGER NOT NULL DEFAULT 0
                    CHECK (last_boundary_seq >= 0),
                last_boundary_id TEXT,
                blocked_reason TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (agent_id, deployment_id, instance_id)
            )
        """)
        self._connection.execute(f"""
            CREATE TABLE intents (
                intent_id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL,
                deployment_id TEXT NOT NULL,
                instance_id TEXT NOT NULL,
                profile_revision INTEGER NOT NULL,
                kind TEXT NOT NULL CHECK (kind IN ('anchor', 'boundary')),
                state TEXT NOT NULL CHECK (state IN {_STATES}),
                document_id TEXT NOT NULL,
                writer_id TEXT NOT NULL,
                container_id TEXT,
                first_journal_id TEXT NOT NULL,
                first_journal_seq INTEGER,
                header_sha256 TEXT,
                journal_max_bytes INTEGER NOT NULL,
                journal_quota_bytes INTEGER NOT NULL,
                engine_identity TEXT NOT NULL,
                previous_writer_id TEXT,
                previous_container_id TEXT,
                fenced_at TEXT,
                created_at TEXT NOT NULL
            )
        """)
        self._connection.execute(_OUTSTANDING_INDEX)
        self._connection.execute("""
            CREATE TABLE snapshots (
                intent_id TEXT NOT NULL,
                journal_seq INTEGER NOT NULL,
                journal_id TEXT NOT NULL,
                snapshot_ref TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                complete_lf_end INTEGER NOT NULL,
                footer_present INTEGER NOT NULL CHECK (footer_present IN (0, 1)),
                PRIMARY KEY (intent_id, journal_seq)
            )
        """)
        self._connection.execute("""
            CREATE TABLE documents (
                document_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL CHECK (kind IN ('anchor', 'boundary')),
                agent_id TEXT NOT NULL,
                deployment_id TEXT NOT NULL,
                instance_id TEXT NOT NULL,
                canonical BLOB NOT NULL,
                sha256 TEXT NOT NULL,
                published INTEGER NOT NULL DEFAULT 0 CHECK (published IN (0, 1))
            )
        """)
        self._connection.execute("""
            CREATE TABLE burned (
                identity TEXT PRIMARY KEY,
                kind TEXT NOT NULL
            )
        """)

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

    def __enter__(self) -> BoundaryRegistry:
        return self

    def __exit__(self, *_exception: object) -> None:
        self.close()

    # --- привязки и intents -----------------------------------------------------------

    def binding_state(self, binding: WriterBinding) -> BindingState | None:
        """Состояние привязки либо None, если instance ещё не известен registry."""
        row = self._connection.execute(
            "SELECT * FROM bindings WHERE agent_id = ? AND deployment_id = ? AND instance_id = ?",
            (binding.agent_id, binding.deployment_id, binding.instance_id),
        ).fetchone()
        if row is None:
            return None
        if row["profile_revision"] != binding.profile_revision:
            raise BoundaryError(
                BoundaryErrorCode.BOUNDARY_BINDING_MISMATCH,
                "registry знает другую применённую revision этого instance",
            )
        head = None
        if row["writer_id"] is not None:
            head = WriterChainHead(
                writer_id=row["writer_id"],
                container_id=row["container_id"],
                first_journal_id=row["first_journal_id"],
                first_journal_seq=_counter(row["first_journal_seq"]),
                journal_max_bytes=_counter(row["journal_max_bytes"]),
                journal_quota_bytes=_counter(row["journal_quota_bytes"]),
            )
        return BindingState(
            binding=binding,
            anchor_id=row["anchor_id"],
            head=head,
            engine_identity=row["engine_identity"],
            last_boundary_seq=_counter(row["last_boundary_seq"], 0),
            last_boundary_id=row["last_boundary_id"],
            blocked_reason=str(row["blocked_reason"]),
        )

    def outstanding_intent(self, binding: WriterBinding) -> StoredIntent | None:
        """Единственный незавершённый intent привязки, если он есть."""
        placeholders = ", ".join("?" for _ in TERMINAL_STATES)
        row = self._connection.execute(
            "SELECT * FROM intents WHERE agent_id = ? AND deployment_id = ? "
            f"AND instance_id = ? AND state NOT IN ({placeholders})",
            (binding.agent_id, binding.deployment_id, binding.instance_id, *TERMINAL_STATES),
        ).fetchone()
        return None if row is None else self._intent_from_row(row)

    def intent(self, intent_id: str) -> StoredIntent | None:
        """Читает intent по идентификатору, включая завершённые состояния."""
        row = self._connection.execute(
            "SELECT * FROM intents WHERE intent_id = ?", (_uuid(intent_id),)
        ).fetchone()
        return None if row is None else self._intent_from_row(row)

    @staticmethod
    def _intent_from_row(row: sqlite3.Row) -> StoredIntent:
        binding = WriterBinding(
            row["agent_id"], row["deployment_id"], row["instance_id"], row["profile_revision"]
        )
        return StoredIntent(
            intent_id=row["intent_id"],
            binding=binding,
            kind=row["kind"],
            state=row["state"],
            writer_id=row["writer_id"],
            first_journal_id=row["first_journal_id"],
            journal_max_bytes=_counter(row["journal_max_bytes"]),
            journal_quota_bytes=_counter(row["journal_quota_bytes"]),
            engine_identity=str(row["engine_identity"]),
            created_at=str(row["created_at"]),
            document_id=row["document_id"],
            container_id=row["container_id"],
            first_journal_seq=(
                None if row["first_journal_seq"] is None else _counter(row["first_journal_seq"])
            ),
            previous_writer_id=row["previous_writer_id"],
            previous_container_id=row["previous_container_id"],
            fenced_at=row["fenced_at"],
            header_sha256=row["header_sha256"],
        )

    def begin_intent(
        self,
        binding: WriterBinding,
        *,
        kind: IntentKind,
        intent_id: str,
        document_id: str,
        writer_id: str,
        first_journal_id: str,
        journal_max_bytes: int,
        journal_quota_bytes: int,
        engine_identity: str,
        first_journal_seq: int | None = None,
        previous_writer_id: str | None = None,
        previous_container_id: str | None = None,
        now: datetime | None = None,
    ) -> StoredIntent:
        """Сохраняет launch intent до запуска и сжигает его идентичности (§3 шаг 1, §8.4).

        Повторное использование boundary_id, anchor_id, writer_id или journal_id
        запрещено даже после отказа, поэтому они попадают в таблицу сожжённых сразу.
        """
        if kind not in ("anchor", "boundary"):
            raise _unavailable("неизвестный вид intent")
        identities = {
            _uuid(intent_id): "intent",
            _uuid(document_id): "document",
            _uuid(writer_id): "writer",
            _uuid(first_journal_id): "journal",
        }
        if len(identities) != 4:
            raise _unavailable("идентичности intent должны различаться")
        created = utc_timestamp(now if now is not None else datetime.now(UTC))
        with self._transaction():
            self._require_binding_row(binding)
            if kind == "anchor" and self._binding_row(binding)["anchor_id"] is not None:
                # Instance имеет ровно один anchor; отказ до запуска контейнера (§8.1).
                raise BoundaryError(
                    BoundaryErrorCode.BOUNDARY_SEQUENCE_CONFLICT,
                    "instance уже имеет начальную привязку писателя",
                )
            if self.outstanding_intent(binding) is not None:
                raise BoundaryError(
                    BoundaryErrorCode.BOUNDARY_SEQUENCE_CONFLICT,
                    "на привязке уже есть незавершённый intent",
                )
            for identity, identity_kind in identities.items():
                try:
                    self._connection.execute(
                        "INSERT INTO burned (identity, kind) VALUES (?, ?)",
                        (identity, identity_kind),
                    )
                except sqlite3.IntegrityError:
                    raise BoundaryError(
                        BoundaryErrorCode.BOUNDARY_SEQUENCE_CONFLICT,
                        "идентичность intent уже использовалась",
                    ) from None
            self._connection.execute(
                "INSERT INTO intents (intent_id, agent_id, deployment_id, instance_id, "
                "profile_revision, kind, state, document_id, writer_id, container_id, "
                "first_journal_id, first_journal_seq, journal_max_bytes, journal_quota_bytes, "
                "engine_identity, previous_writer_id, previous_container_id, fenced_at, "
                "header_sha256, created_at) VALUES (?, ?, ?, ?, ?, ?, 'intent', ?, ?, NULL, "
                "?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)",
                (
                    _uuid(intent_id),
                    binding.agent_id,
                    binding.deployment_id,
                    binding.instance_id,
                    binding.profile_revision,
                    kind,
                    _uuid(document_id),
                    _uuid(writer_id),
                    _uuid(first_journal_id),
                    None if first_journal_seq is None else _counter(first_journal_seq),
                    _counter(journal_max_bytes),
                    _counter(journal_quota_bytes),
                    engine_identity,
                    None if previous_writer_id is None else _uuid(previous_writer_id),
                    previous_container_id,
                    created,
                ),
            )
        stored = self.intent(intent_id)
        assert stored is not None
        return stored

    def _require_binding_row(self, binding: WriterBinding) -> None:
        """Создаёт запись привязки при первом обращении; revision не переписывается."""
        row = self._connection.execute(
            "SELECT profile_revision, blocked_reason FROM bindings WHERE agent_id = ? "
            "AND deployment_id = ? AND instance_id = ?",
            (binding.agent_id, binding.deployment_id, binding.instance_id),
        ).fetchone()
        if row is None:
            self._connection.execute(
                "INSERT INTO bindings (agent_id, deployment_id, instance_id, profile_revision) "
                "VALUES (?, ?, ?, ?)",
                (
                    binding.agent_id,
                    binding.deployment_id,
                    binding.instance_id,
                    binding.profile_revision,
                ),
            )
            return
        if row["profile_revision"] != binding.profile_revision:
            raise BoundaryError(
                BoundaryErrorCode.BOUNDARY_BINDING_MISMATCH,
                "registry знает другую применённую revision этого instance",
            )
        if row["blocked_reason"]:
            raise BoundaryError(
                BoundaryErrorCode.BOUNDARY_SEQUENCE_CONFLICT,
                "для этого instance граница процесса больше не поддерживается",
            )

    def advance(
        self,
        intent_id: str,
        state: IntentState,
        *,
        container_id: str | None = None,
        first_journal_seq: int | None = None,
        fenced_at: str | None = None,
        header_sha256: str | None = None,
    ) -> StoredIntent:
        """Переводит intent в следующее состояние строго по порядку §8.4.

        Движение только вперёд: повтор и откат состояния запрещены, а неудачу после
        запуска нового writer фиксирует отдельный метод.
        """
        if state not in _STATES or state in TERMINAL_STATES:
            raise _unavailable("неизвестное состояние intent")
        with self._transaction():
            current = self.intent(intent_id)
            if current is None:
                raise _unavailable("intent не найден")
            if _STATES.index(state) <= _STATES.index(current.state):
                raise _unavailable("состояние intent не может повторяться или откатываться")
            if container_id is not None and current.container_id not in (None, container_id):
                raise BoundaryError(
                    BoundaryErrorCode.BOUNDARY_BINDING_MISMATCH,
                    "intent уже привязан к другому контейнеру",
                )
            self._connection.execute(
                "UPDATE intents SET state = ?, "
                "container_id = COALESCE(?, container_id), "
                "first_journal_seq = COALESCE(?, first_journal_seq), "
                "fenced_at = COALESCE(?, fenced_at), "
                "header_sha256 = COALESCE(?, header_sha256) WHERE intent_id = ?",
                (
                    state,
                    container_id,
                    None if first_journal_seq is None else _counter(first_journal_seq),
                    fenced_at,
                    header_sha256,
                    _uuid(intent_id),
                ),
            )
        updated = self.intent(intent_id)
        assert updated is not None
        return updated

    def block_binding(self, binding: WriterBinding, reason: str) -> None:
        """Запрещает новые переходы instance, не закрывая текущий intent.

        Применяется, когда наблюдение противоречит заявленному ограждению: такой
        переход по §8.3 становится неизвестным, а неизвестный исход по §2 запрещает
        новые переходы до решения оператора. Данные и опубликованные документы
        остаются на месте.
        """
        with self._transaction():
            self._require_binding_row_exists(binding)
            self._connection.execute(
                "UPDATE bindings SET blocked_reason = ? WHERE agent_id = ? "
                "AND deployment_id = ? AND instance_id = ?",
                (
                    reason[:200] or "ограждение закрываемого писателя не подтверждено",
                    binding.agent_id,
                    binding.deployment_id,
                    binding.instance_id,
                ),
            )

    def _require_binding_row_exists(self, binding: WriterBinding) -> None:
        """Привязка обязана существовать: блокировать нечего, если её нет."""
        row = self._connection.execute(
            "SELECT 1 FROM bindings WHERE agent_id = ? AND deployment_id = ? AND instance_id = ?",
            (binding.agent_id, binding.deployment_id, binding.instance_id),
        ).fetchone()
        if row is None:
            raise _unavailable("привязка перехода не найдена")

    def reallocate(self, intent_id: str, writer_id: str, first_journal_id: str) -> StoredIntent:
        """Выдаёт новые writer_id и journal_id снятой попытке запуска (§8.4).

        Прежние идентичности остаются сожжёнными: контейнер, который ни разу не
        запускался, удалён, но его UUID больше никогда не используются. Повторное
        выделение допустимо только до выдачи RW, то есть в состоянии next_created.
        """
        identities = {_uuid(writer_id): "writer", _uuid(first_journal_id): "journal"}
        if len(identities) != 2:
            raise _unavailable("идентичности повторного выделения должны различаться")
        with self._transaction():
            current = self._require_state(intent_id, "next_created", None)
            for identity, kind in identities.items():
                try:
                    self._connection.execute(
                        "INSERT INTO burned (identity, kind) VALUES (?, ?)", (identity, kind)
                    )
                except sqlite3.IntegrityError:
                    raise BoundaryError(
                        BoundaryErrorCode.BOUNDARY_SEQUENCE_CONFLICT,
                        "идентичность повторного выделения уже использовалась",
                    ) from None
            # Возврат к стадии до create: для перехода снимки уже закреплены и
            # повторно не создаются, для anchor ограждения и снимков не было.
            restart = "intent" if current.kind == "anchor" else "snapshots_published"
            self._connection.execute(
                "UPDATE intents SET writer_id = ?, first_journal_id = ?, container_id = NULL, "
                "header_sha256 = NULL, state = ? WHERE intent_id = ?",
                (_uuid(writer_id), _uuid(first_journal_id), restart, current.intent_id),
            )
        updated = self.intent(intent_id)
        assert updated is not None
        return updated

    def fail_after_launch(self, intent_id: str, reason: str) -> None:
        """Фиксирует неудачу после выдачи RW новому writer: binding больше не продолжается."""
        with self._transaction():
            current = self.intent(intent_id)
            if current is None:
                raise _unavailable("intent не найден")
            if current.state in TERMINAL_STATES:
                raise _unavailable("завершённый intent повторно не закрывается")
            self._connection.execute(
                "UPDATE intents SET state = 'failed_after_launch' WHERE intent_id = ?",
                (current.intent_id,),
            )
            self._connection.execute(
                "UPDATE bindings SET blocked_reason = ? WHERE agent_id = ? "
                "AND deployment_id = ? AND instance_id = ?",
                (
                    reason[:200] or "неудача после запуска нового writer",
                    current.binding.agent_id,
                    current.binding.deployment_id,
                    current.binding.instance_id,
                ),
            )

    # --- снимки -----------------------------------------------------------------------

    def record_snapshots(self, intent_id: str, entries: Sequence[SnapshotEntry]) -> None:
        """Фиксирует перечень закреплённых копий вместе с состоянием intent (§8.3).

        Перечень записывается один раз: повторная публикация использует существующий
        набор без новой копии и двойного учёта места.
        """
        if not entries:
            raise BoundaryError(
                BoundaryErrorCode.BOUNDARY_SNAPSHOT_UNAVAILABLE, "перечень снимков пуст"
            )
        with self._transaction():
            current = self._require_state(intent_id, "fenced", None)
            if self._connection.execute(
                "SELECT 1 FROM snapshots WHERE intent_id = ?", (current.intent_id,)
            ).fetchone():
                raise BoundaryError(
                    BoundaryErrorCode.BOUNDARY_SEQUENCE_CONFLICT,
                    "перечень снимков этого перехода уже записан",
                )
            for entry in entries:
                self._connection.execute(
                    "INSERT INTO snapshots (intent_id, journal_seq, journal_id, snapshot_ref, "
                    "size_bytes, sha256, complete_lf_end, footer_present) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        current.intent_id,
                        entry.journal_seq,
                        entry.journal_id,
                        entry.snapshot_ref,
                        entry.size_bytes,
                        entry.sha256,
                        entry.complete_lf_end,
                        int(entry.footer_present),
                    ),
                )

    def snapshots(self, intent_id: str) -> tuple[SnapshotEntry, ...]:
        """Перечень копий перехода в порядке journal_seq."""
        rows = self._connection.execute(
            "SELECT * FROM snapshots WHERE intent_id = ? ORDER BY journal_seq",
            (_uuid(intent_id),),
        ).fetchall()
        return tuple(
            SnapshotEntry(
                journal_id=row["journal_id"],
                journal_seq=_counter(row["journal_seq"]),
                snapshot_ref=str(row["snapshot_ref"]),
                size_bytes=_counter(row["size_bytes"], 0),
                sha256=str(row["sha256"]),
                complete_lf_end=_counter(row["complete_lf_end"], 0),
                footer_present=bool(row["footer_present"]),
            )
            for row in rows
        )

    # --- фиксация документов ----------------------------------------------------------

    def commit_anchor(
        self, intent_id: str, publisher_epoch: str, now: datetime | None = None
    ) -> StoredDocument:
        """Фиксирует anchor и привязывает первую цепочку instance (§8.1).

        Канонические байты создаются в этой транзакции один раз и сохраняются вместе
        со своим SHA-256: дальше документ только повторяется.
        """
        epoch = _uuid(publisher_epoch)
        committed_at = utc_timestamp(now if now is not None else datetime.now(UTC))
        with self._transaction():
            current = self._require_state(intent_id, "next_bound", "anchor")
            header = self._require_header(current)
            row = self._binding_row(current.binding)
            if row["anchor_id"] is not None or row["writer_id"] is not None:
                raise BoundaryError(
                    BoundaryErrorCode.BOUNDARY_SEQUENCE_CONFLICT,
                    "instance уже имеет anchor",
                )
            assert current.container_id is not None and current.first_journal_seq is not None
            anchor = WriterAnchor(
                binding=current.binding,
                anchor_id=current.document_id,
                publisher_epoch=epoch,
                writer_id=current.writer_id,
                container_id=current.container_id,
                launch_intent_id=current.intent_id,
                first_journal=FirstJournal(
                    journal_id=current.first_journal_id,
                    journal_seq=current.first_journal_seq,
                    header_sha256=header,
                ),
                committed_at=committed_at,
            )
            self._store_document(current, anchor.canonical())
            self._connection.execute(
                "UPDATE bindings SET anchor_id = ?, writer_id = ?, container_id = ?, "
                "first_journal_id = ?, first_journal_seq = ?, journal_max_bytes = ?, "
                "journal_quota_bytes = ?, engine_identity = ? WHERE agent_id = ? "
                "AND deployment_id = ? AND instance_id = ?",
                (
                    current.document_id,
                    current.writer_id,
                    current.container_id,
                    current.first_journal_id,
                    current.first_journal_seq,
                    current.journal_max_bytes,
                    current.journal_quota_bytes,
                    current.engine_identity,
                    current.binding.agent_id,
                    current.binding.deployment_id,
                    current.binding.instance_id,
                ),
            )
            self._connection.execute(
                "UPDATE intents SET state = 'committed' WHERE intent_id = ?",
                (current.intent_id,),
            )
        document = self.document(current.document_id)
        assert document is not None
        return document

    def commit_boundary(
        self, intent_id: str, publisher_epoch: str, now: datetime | None = None
    ) -> StoredDocument:
        """Фиксирует переход: boundary_seq выделяется только здесь (§8.4).

        Сбойные intents номер не расходуют, поэтому счётчик увеличивается в той же
        транзакции, в которой сохраняются канонические байты документа.
        """
        epoch = _uuid(publisher_epoch)
        committed_at = utc_timestamp(now if now is not None else datetime.now(UTC))
        with self._transaction():
            current = self._require_state(intent_id, "next_bound", "boundary")
            header = self._require_header(current)
            row = self._binding_row(current.binding)
            entries = self.snapshots(current.intent_id)
            if not entries:
                raise BoundaryError(
                    BoundaryErrorCode.BOUNDARY_SNAPSHOT_UNAVAILABLE,
                    "перехода без закреплённых копий не бывает",
                )
            if (
                current.previous_writer_id is None
                or current.previous_container_id is None
                or current.previous_writer_id != row["writer_id"]
                or current.previous_container_id != row["container_id"]
            ):
                raise BoundaryError(
                    BoundaryErrorCode.BOUNDARY_BINDING_MISMATCH,
                    "закрываемый writer не совпадает с текущей цепочкой привязки",
                )
            if current.fenced_at is None:
                raise BoundaryError(
                    BoundaryErrorCode.PREVIOUS_WRITER_NOT_FENCED,
                    "в intent нет времени ограждения прежнего writer",
                )
            assert current.container_id is not None and current.first_journal_seq is not None
            boundary_seq = _counter(row["last_boundary_seq"], 0) + 1
            boundary = ProcessBoundary(
                binding=current.binding,
                boundary_id=current.document_id,
                boundary_seq=boundary_seq,
                previous_boundary_id=row["last_boundary_id"],
                publisher_epoch=epoch,
                previous_writer=PreviousWriter(
                    writer_id=current.previous_writer_id,
                    container_id=current.previous_container_id,
                    fenced_at=current.fenced_at,
                ),
                previous_journals=entries,
                next_writer=NextWriter(
                    writer_id=current.writer_id,
                    container_id=current.container_id,
                    first_journal=FirstJournal(
                        journal_id=current.first_journal_id,
                        journal_seq=current.first_journal_seq,
                        header_sha256=header,
                    ),
                ),
                committed_at=committed_at,
            )
            self._store_document(current, boundary.canonical())
            self._connection.execute(
                "UPDATE bindings SET writer_id = ?, container_id = ?, first_journal_id = ?, "
                "first_journal_seq = ?, journal_max_bytes = ?, journal_quota_bytes = ?, "
                "engine_identity = ?, last_boundary_seq = ?, last_boundary_id = ? "
                "WHERE agent_id = ? AND deployment_id = ? AND instance_id = ?",
                (
                    current.writer_id,
                    current.container_id,
                    current.first_journal_id,
                    current.first_journal_seq,
                    current.journal_max_bytes,
                    current.journal_quota_bytes,
                    current.engine_identity,
                    boundary_seq,
                    current.document_id,
                    current.binding.agent_id,
                    current.binding.deployment_id,
                    current.binding.instance_id,
                ),
            )
            self._connection.execute(
                "UPDATE intents SET state = 'committed' WHERE intent_id = ?",
                (current.intent_id,),
            )
        document = self.document(current.document_id)
        assert document is not None
        return document

    def document(self, document_id: str) -> StoredDocument | None:
        """Выданный документ по идентификатору; байты никогда не пересобираются."""
        row = self._connection.execute(
            "SELECT * FROM documents WHERE document_id = ?", (_uuid(document_id),)
        ).fetchone()
        if row is None:
            return None
        return StoredDocument(
            document_id=row["document_id"],
            kind=row["kind"],
            canonical=bytes(row["canonical"]),
            sha256=str(row["sha256"]),
            published=bool(row["published"]),
        )

    def mark_published(self, intent_id: str) -> StoredDocument:
        """Отмечает публикацию документа и завершает intent состоянием exported."""
        with self._transaction():
            current = self._require_state(intent_id, "committed", None)
            self._connection.execute(
                "UPDATE documents SET published = 1 WHERE document_id = ?",
                (current.document_id,),
            )
            self._connection.execute(
                "UPDATE intents SET state = 'exported' WHERE intent_id = ?",
                (current.intent_id,),
            )
        document = self.document(current.document_id)
        assert document is not None
        return document

    def _store_document(self, intent: StoredIntent, canonical: bytes) -> None:
        """Сохраняет точные байты документа и их SHA-256 в транзакции commit."""
        digest = hashlib.sha256(canonical).hexdigest()
        try:
            self._connection.execute(
                "INSERT INTO documents (document_id, kind, agent_id, deployment_id, "
                "instance_id, canonical, sha256) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    intent.document_id,
                    intent.kind,
                    intent.binding.agent_id,
                    intent.binding.deployment_id,
                    intent.binding.instance_id,
                    canonical,
                    digest,
                ),
            )
        except sqlite3.IntegrityError:
            raise BoundaryError(
                BoundaryErrorCode.BOUNDARY_SEQUENCE_CONFLICT,
                "документ с этим идентификатором уже зафиксирован",
            ) from None

    def _require_state(
        self, intent_id: str, state: IntentState, kind: IntentKind | None
    ) -> StoredIntent:
        """Проверяет, что intent существует, имеет нужный вид и ожидаемое состояние."""
        current = self.intent(intent_id)
        if current is None:
            raise _unavailable("intent не найден")
        if current.state != state or (kind is not None and current.kind != kind):
            raise _unavailable("состояние intent не допускает эту операцию")
        return current

    @staticmethod
    def _require_header(intent: StoredIntent) -> str:
        """Хеш первой header нового writer сохраняется при переходе в next_bound."""
        header = intent.header_sha256
        if not isinstance(header, str) or not header:
            raise BoundaryError(
                BoundaryErrorCode.NEXT_WRITER_UNBOUND,
                "в intent нет хеша первой header нового writer",
            )
        return header

    def _binding_row(self, binding: WriterBinding) -> sqlite3.Row:
        """Строка привязки обязана существовать к моменту commit."""
        found = self._connection.execute(
            "SELECT * FROM bindings WHERE agent_id = ? AND deployment_id = ? AND instance_id = ?",
            (binding.agent_id, binding.deployment_id, binding.instance_id),
        ).fetchone()
        if found is None:
            raise _unavailable("привязка перехода не найдена")
        row = cast(sqlite3.Row, found)
        if row["blocked_reason"]:
            raise BoundaryError(
                BoundaryErrorCode.BOUNDARY_SEQUENCE_CONFLICT,
                "для этого instance граница процесса больше не поддерживается",
            )
        return row
