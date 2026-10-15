"""Собственные raw/checkpoint/ledger Cowrie на соединении существующего spool."""

import sqlite3
from uuid import UUID

from agent.telemetry.adapters.cowrie import CowrieBinding
from agent.telemetry.spool import SpoolRepository

from .models import CowrieJournalError, CowrieJournalSource, binding_scope


class CowrieJournalRepository:
    """Один поток; версии и владелец проверяются до изменения таблиц Cowrie."""

    def __init__(
        self, spool: SpoolRepository, *, max_storage_bytes: int = 256 * 1024 * 1024
    ) -> None:
        if type(max_storage_bytes) is not int or max_storage_bytes < 1:
            raise ValueError("Нужна положительная квота хранения")
        self.spool, self.db, self.quota = spool, spool.db, max_storage_bytes
        owner = self.db.execute("SELECT value FROM metadata WHERE key='agent_id'").fetchone()
        if owner != (str(spool.agent_id),):
            raise CowrieJournalError("OWNER_MISMATCH")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            version = self.db.execute(
                "SELECT value FROM metadata WHERE key='cowrie_storage_version'"
            ).fetchone()
            if version is not None:
                if version != ("1",):
                    raise CowrieJournalError("UNSUPPORTED_STORAGE_VERSION")
                return
            for statement in (
                "CREATE TABLE cowrie_scopes (scope TEXT PRIMARY KEY, revision INTEGER NOT NULL, "
                "port INTEGER NOT NULL)",
                "CREATE TABLE cowrie_generations (journal_id TEXT PRIMARY KEY, "
                "scope TEXT NOT NULL, "
                "root TEXT NOT NULL, path TEXT NOT NULL, device TEXT, inode TEXT, "
                "next_offset INTEGER NOT NULL DEFAULT 0, tail BLOB NOT NULL DEFAULT X'', "
                "blocked TEXT, UNIQUE(device,inode))",
                "CREATE TABLE cowrie_raw (journal_id TEXT NOT NULL, offset INTEGER NOT NULL, "
                "next_offset INTEGER NOT NULL, raw BLOB NOT NULL, PRIMARY KEY(journal_id,offset))",
                "CREATE TABLE cowrie_ledger (journal_id TEXT NOT NULL, offset INTEGER NOT NULL, "
                "raw_digest BLOB NOT NULL, mapper_version TEXT NOT NULL, result TEXT NOT NULL, "
                "event_id TEXT UNIQUE, payload TEXT, PRIMARY KEY(journal_id,offset))",
                "CREATE TABLE cowrie_usage (singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
                "bytes INTEGER NOT NULL)",
                "INSERT INTO cowrie_usage VALUES (1,0)",
                "INSERT INTO metadata VALUES ('cowrie_storage_version','1')",
            ):
                self.db.execute(statement)

    def _checkpoint(self, stage: str) -> None:
        """Точка аварийных испытаний на настоящей границе транзакции."""

    def _usage(self, delta: int) -> None:
        current = self.db.execute("SELECT bytes FROM cowrie_usage WHERE singleton=1").fetchone()[0]
        if current + delta > self.quota:
            raise CowrieJournalError("STORAGE_LIMIT")
        self.db.execute("UPDATE cowrie_usage SET bytes=bytes+? WHERE singleton=1", (delta,))

    def register(self, source: CowrieJournalSource) -> None:
        """Внешний worker подтверждает ACK; здесь фиксируется неизменяемый scope."""
        if source.binding.agent_id != self.spool.agent_id:
            raise CowrieJournalError("OWNER_MISMATCH")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            previous = self.db.execute(
                "SELECT revision,port FROM cowrie_scopes WHERE scope=?", (source.scope,)
            ).fetchone()
            expected = (source.binding.profile_revision, source.binding.service_port)
            if previous is not None and previous != expected:
                raise CowrieJournalError("BINDING_CONFLICT")
            legacy = f"cowrie:{source.binding.deployment_id}:{source.binding.instance_id}"
            if self.spool.checkpoint(legacy):
                raise CowrieJournalError("LEGACY_CONFLICT")
            self.db.execute(
                "INSERT OR IGNORE INTO cowrie_scopes VALUES (?,?,?)", (source.scope, *expected)
            )
            prior = self.db.execute(
                "SELECT scope,root FROM cowrie_generations WHERE journal_id=?",
                (str(source.journal_id),),
            ).fetchone()
            if prior is not None and prior != (source.scope, str(source.root)):
                raise CowrieJournalError("GENERATION_CONFLICT")
            if (
                prior is None
                and self.db.execute("SELECT count(*) FROM cowrie_generations").fetchone()[0] >= 4096
            ):
                raise CowrieJournalError("REGISTRY_LIMIT")
            self.db.execute(
                "INSERT INTO cowrie_generations (journal_id,scope,root,path) VALUES (?,?,?,?) "
                "ON CONFLICT(journal_id) DO UPDATE SET path=excluded.path",
                (str(source.journal_id), source.scope, str(source.root), source.relative_path),
            )

    def claimed(self, binding: CowrieBinding) -> bool:
        return (
            self.db.execute(
                "SELECT 1 FROM cowrie_scopes WHERE scope=?", (binding_scope(binding),)
            ).fetchone()
            is not None
        )

    def state(self, journal_id: UUID) -> tuple[str | None, str | None, int, bytes, str | None]:
        row = self.db.execute(
            "SELECT device,inode,next_offset,tail,blocked FROM cowrie_generations "
            "WHERE journal_id=?",
            (str(journal_id),),
        ).fetchone()
        if row is None:
            raise CowrieJournalError("UNKNOWN_GENERATION")
        return row[0], row[1], row[2], row[3], row[4]

    def identify(self, journal_id: UUID, identity: tuple[str, str]) -> None:
        device, inode, _, _, _ = self.state(journal_id)
        if device is not None and (device, inode) != identity:
            raise CowrieJournalError("SOURCE_REPLACED")
        try:
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                self.db.execute(
                    "UPDATE cowrie_generations SET device=?,inode=? WHERE journal_id=?",
                    (*identity, str(journal_id)),
                )
        except sqlite3.IntegrityError:
            raise CowrieJournalError("GENERATION_CONFLICT") from None

    def verify(self, journal_id: UUID, offset: int, raw: bytes) -> None:
        stored = self.db.execute(
            "SELECT raw FROM cowrie_raw WHERE journal_id=? AND offset=?", (str(journal_id), offset)
        ).fetchone()
        if stored != (raw,):
            raise CowrieJournalError("POSITION_CONFLICT")

    def append(self, journal_id: UUID, offset: int, raw: bytes) -> None:
        """Raw и физический checkpoint фиксируются до любых вызовов mapper."""
        if type(offset) is not int or not 0 <= offset <= 9007199254740991:
            raise CowrieJournalError("INVALID_POSITION")
        if (
            type(raw) is not bytes
            or not raw.endswith(b"\n")
            or b"\n" in raw[:-1]
            or len(raw) > 65536
        ):
            raise CowrieJournalError("INVALID_RECORD")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            _, _, end, tail, blocked = self.state(journal_id)
            if blocked:
                raise CowrieJournalError(blocked)
            if offset < end:
                self.verify(journal_id, offset, raw)
                return
            if offset != end or not raw.startswith(tail):
                raise CowrieJournalError("POSITION_CONFLICT")
            self._usage(len(raw) - len(tail))
            self.db.execute(
                "INSERT INTO cowrie_raw VALUES (?,?,?,?)",
                (str(journal_id), offset, offset + len(raw), raw),
            )
            self._checkpoint("raw_inserted")
            self.db.execute(
                "UPDATE cowrie_generations SET next_offset=?,tail=X'' WHERE journal_id=?",
                (offset + len(raw), str(journal_id)),
            )
            self._checkpoint("raw_before_commit")
        self._checkpoint("raw_committed")

    def tail(self, journal_id: UUID, value: bytes) -> None:
        if type(value) is not bytes or len(value) > 65536 or b"\n" in value:
            raise CowrieJournalError("INVALID_RECORD")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            old = self.state(journal_id)[3]
            if not value.startswith(old):
                raise CowrieJournalError("POSITION_CONFLICT")
            self._usage(len(value) - len(old))
            self.db.execute(
                "UPDATE cowrie_generations SET tail=? WHERE journal_id=?", (value, str(journal_id))
            )

    def block(self, journal_id: UUID, code: str) -> None:
        """Первый отказ устойчиво запрещает обход повреждённой позиции."""
        with self.db:
            self.db.execute(
                "UPDATE cowrie_generations SET blocked=COALESCE(blocked,?) WHERE journal_id=?",
                (code, str(journal_id)),
            )

    def bindings(self) -> tuple[tuple[UUID, CowrieBinding], ...]:
        """Исторические raw восстанавливаются даже без файла и provider snapshot."""
        import json

        rows = self.db.execute(
            "SELECT g.journal_id,s.scope,s.revision,s.port FROM cowrie_generations g "
            "JOIN cowrie_scopes s ON s.scope=g.scope ORDER BY g.rowid"
        ).fetchall()
        result = []
        for journal, scope, revision, port in rows:
            agent, deployment, instance = (UUID(value) for value in json.loads(scope))
            result.append(
                (UUID(journal), CowrieBinding(agent, deployment, instance, revision, port))
            )
        return tuple(result)
