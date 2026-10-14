"""Атомарная миграция raw/boundary в общей spool без восстановления доверия."""

import sqlite3


class StorageSchemaError(ValueError):
    """Безопасный отказ владельца или неподдерживаемой версии."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class StorageMigration:
    """Версия собственного пространства metadata не затрагивает другие repositories."""

    VERSION = 1
    KEY = "low_storage_version"
    STATEMENTS = (
        "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS low_raw_generations ("
        "source_key TEXT NOT NULL, journal_id TEXT NOT NULL, next_offset INTEGER NOT NULL, "
        "journal_seq TEXT NOT NULL, previous_journal_id TEXT, UNIQUE(source_key,journal_seq), "
        "PRIMARY KEY(source_key,journal_id))",
        "CREATE UNIQUE INDEX IF NOT EXISTS low_raw_successor ON "
        "low_raw_generations(source_key,previous_journal_id) WHERE previous_journal_id IS NOT NULL",
        "CREATE TABLE IF NOT EXISTS low_raw_usage "
        "(singleton INTEGER PRIMARY KEY CHECK(singleton=1), bytes INTEGER NOT NULL)",
        "CREATE TABLE IF NOT EXISTS low_raw_locations (source_key TEXT NOT NULL, "
        "path TEXT NOT NULL, journal_id TEXT NOT NULL, PRIMARY KEY(source_key,path))",
        "CREATE TABLE IF NOT EXISTS low_raw_records (source_key TEXT NOT NULL, "
        "journal_id TEXT NOT NULL, offset INTEGER NOT NULL, next_offset INTEGER NOT NULL, "
        "kind TEXT NOT NULL, raw BLOB NOT NULL, PRIMARY KEY(source_key,journal_id,offset))",
        "CREATE TABLE low_storage_usage "
        "(singleton INTEGER PRIMARY KEY CHECK(singleton=1), bytes INTEGER NOT NULL)",
        "CREATE TABLE low_boundary_bindings (scope TEXT PRIMARY KEY, revision INTEGER NOT NULL, "
        "version INTEGER NOT NULL, writer TEXT NOT NULL, container TEXT NOT NULL, "
        "boundary_id TEXT, boundary_seq INTEGER NOT NULL)",
        "CREATE TABLE low_boundary_writers (scope TEXT NOT NULL, writer TEXT NOT NULL, "
        "container TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('active','sealed')), "
        "opened_by TEXT, sealed_by TEXT, PRIMARY KEY(scope,writer), UNIQUE(scope,container))",
        "CREATE UNIQUE INDEX low_boundary_active ON low_boundary_writers(scope) "
        "WHERE state='active'",
        "CREATE TABLE low_boundary_proofs (scope TEXT NOT NULL, boundary_id TEXT NOT NULL, "
        "seq INTEGER NOT NULL, previous_id TEXT, proof BLOB NOT NULL, digest BLOB NOT NULL, "
        "PRIMARY KEY(scope,boundary_id), UNIQUE(scope,seq))",
        "CREATE TABLE low_boundary_journals (scope TEXT NOT NULL, journal_id TEXT NOT NULL, "
        "writer TEXT NOT NULL, boundary_id TEXT, PRIMARY KEY(scope,journal_id))",
        "CREATE TABLE low_boundary_pins (scope TEXT NOT NULL, boundary_id TEXT NOT NULL, "
        "journal_id TEXT NOT NULL, writer TEXT NOT NULL, source_key TEXT NOT NULL, "
        "journal_seq TEXT NOT NULL, snapshot_ref TEXT NOT NULL, snapshot_digest BLOB NOT NULL, "
        "snapshot_size INTEGER NOT NULL, lf_end INTEGER NOT NULL, prefix_digest BLOB NOT NULL, "
        "footer_present INTEGER NOT NULL, PRIMARY KEY(scope,journal_id))",
        "CREATE TABLE low_boundary_tails (scope TEXT NOT NULL, journal_id TEXT NOT NULL, "
        "offset INTEGER NOT NULL, length INTEGER NOT NULL, "
        "digest BLOB NOT NULL, raw BLOB NOT NULL, "
        "PRIMARY KEY(scope,journal_id))",
    )

    def _checkpoint(self, step: int) -> None:
        """Точка испытания отката после каждого изменяющего шага."""

    def check(self, db: sqlite3.Connection, agent_id: str) -> int:
        """Читает владельца и версию до любых изменений, в том числе PRAGMA WAL."""
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata'"
        ).fetchone()
        if not exists:
            return 0
        owner = db.execute("SELECT value FROM metadata WHERE key='agent_id'").fetchone()
        if owner is not None and owner[0] != agent_id:
            raise StorageSchemaError("OWNER_MISMATCH")
        row = db.execute("SELECT value FROM metadata WHERE key=?", (self.KEY,)).fetchone()
        if row is None:
            return 0
        if row[0] != str(self.VERSION):
            raise StorageSchemaError("UNSUPPORTED_STORAGE_VERSION")
        return self.VERSION

    def migrate(self, db: sqlite3.Connection, agent_id: str) -> None:
        """DDL и счётчики фиксируются вместе; executescript не используется."""
        self.check(db, agent_id)
        with db:
            db.execute("BEGIN IMMEDIATE")
            if self.check(db, agent_id) == self.VERSION:
                return
            for step, sql in enumerate(self.STATEMENTS):
                db.execute(sql)
                self._checkpoint(step)
            mutations = (
                ("INSERT OR IGNORE INTO metadata VALUES ('agent_id',?)", (agent_id,)),
                (
                    "INSERT OR IGNORE INTO low_raw_usage SELECT 1, "
                    "COALESCE(SUM(length(raw)),0) FROM low_raw_records",
                    (),
                ),
                (
                    "INSERT INTO low_storage_usage SELECT 1, "
                    "COALESCE(SUM(length(raw)),0) FROM low_raw_records",
                    (),
                ),
                ("INSERT INTO metadata VALUES (?,?)", (self.KEY, str(self.VERSION))),
            )
            for step, (sql, parameters) in enumerate(mutations, len(self.STATEMENTS)):
                db.execute(sql, parameters)
                self._checkpoint(step)
