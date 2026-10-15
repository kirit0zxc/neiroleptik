"""Изолированное принятие подготовленных bytes; не валидатор доверия или consumer."""

import json
import sqlite3
from dataclasses import dataclass
from hashlib import sha256
from typing import Literal

from .durable import RawJournalError, RawJournalRepository
from .models import JournalBinding, JournalReadError, JournalRecord
from .validation import RecordValidator


class BoundaryError(ValueError):
    """Стабильный безопасный код; WRITER_BUSY допускает повтор."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class PreparedJournal:
    """Закрепление внешнего snapshot и точный непрозрачный хвост после LF."""

    source_key: str
    journal_id: str
    journal_seq: int
    snapshot_ref: str
    snapshot_digest: bytes
    snapshot_size: int
    lf_end: int
    prefix_digest: bytes
    footer_present: bool
    tail: bytes


@dataclass(frozen=True, slots=True)
class PreparedTransition:
    """Внутренний пакет внешнего валидатора; frozen не является доказательством доверия."""

    agent_id: str
    binding: JournalBinding
    expected_version: int
    boundary_id: str
    seq: int
    previous_id: str | None
    proof: bytes
    old_writer: str
    old_container: str
    new_writer: str
    new_container: str
    journals: tuple[PreparedJournal, ...]
    header: JournalRecord
    new_journal_seq: int


@dataclass(frozen=True, slots=True)
class BoundaryLimits:
    """Отдельные пределы подготовленных bytes, а не будущей схемы proof."""

    proof_bytes: int = 1024 * 1024
    journals: int = 64
    tail_bytes: int = 4 * 1024 * 1024

    def __post_init__(self) -> None:
        if any(
            type(v) is not int or v < 1 for v in (self.proof_bytes, self.journals, self.tail_bytes)
        ):
            raise ValueError("Нужны положительные целые лимиты")


class BoundaryRepository:
    """Владеет единственной транзакцией raw repository без DDL и файлового ввода."""

    def __init__(self, raw: RawJournalRepository, limits: BoundaryLimits | None = None) -> None:
        self.raw = raw
        self.db = raw.db
        self.limits = limits or BoundaryLimits()

    def scope(self, binding: JournalBinding) -> str:
        """Revision проверяется отдельно и не создаёт новую историю процесса."""
        return json.dumps(
            [self.raw.agent_id, binding.deployment_id, binding.instance_id],
            separators=(",", ":"),
        )

    def _checkpoint(self, stage: str) -> None:
        """Точка fault/crash injection без изменения рабочего поведения."""

    def _commit(self) -> None:
        """Узкий адаптер позволяет проверить отказ настоящей границы COMMIT."""
        self.db.commit()

    def _validate(self, p: PreparedTransition) -> None:
        """Проверяет структуру, но не автора proof и не внешние snapshots."""
        text = (p.boundary_id, p.old_writer, p.old_container, p.new_writer, p.new_container)
        if (
            p.agent_id != self.raw.agent_id
            or any(type(v) is not str or not v or len(v) > 1024 for v in text)
            or type(p.expected_version) is not int
            or p.expected_version < 0
            or type(p.seq) is not int
            or not 1 <= p.seq <= 2**63 - 1
            or (p.previous_id is not None and (type(p.previous_id) is not str or not p.previous_id))
            or type(p.proof) is not bytes
            or not p.proof
            or type(p.journals) is not tuple
            or type(p.new_journal_seq) is not int
            or p.new_journal_seq < 1
        ):
            raise BoundaryError("INVALID_PREPARED")
        if (
            len(p.proof) > self.limits.proof_bytes
            or not 1 <= len(p.journals) <= self.limits.journals
        ):
            raise BoundaryError("STORAGE_LIMIT")
        ids: set[str] = set()
        sequences: set[int] = set()
        refs: set[str] = set()
        for j in p.journals:
            if type(j) is not PreparedJournal:
                raise BoundaryError("INVALID_PREPARED")
            if (
                j.source_key != self.raw.source_key(p.binding)
                or type(j.journal_id) is not str
                or not j.journal_id
                or j.journal_id in ids
                or type(j.journal_seq) is not int
                or j.journal_seq < 1
                or j.journal_seq in sequences
                or type(j.snapshot_ref) is not str
                or not j.snapshot_ref
                or j.snapshot_ref in refs
                or type(j.snapshot_digest) is not bytes
                or len(j.snapshot_digest) != 32
                or type(j.prefix_digest) is not bytes
                or len(j.prefix_digest) != 32
                or type(j.lf_end) is not int
                or j.lf_end < 1
                or type(j.snapshot_size) is not int
                or type(j.tail) is not bytes
                or j.snapshot_size != j.lf_end + len(j.tail)
                or type(j.footer_present) is not bool
            ):
                raise BoundaryError("INVALID_PREPARED")
            ids.add(j.journal_id)
            sequences.add(j.journal_seq)
            refs.add(j.snapshot_ref)
        if sum(len(j.tail) for j in p.journals) > self.limits.tail_bytes:
            raise BoundaryError("STORAGE_LIMIT")
        h = p.header
        if type(h) is not JournalRecord or type(h.raw) is not bytes:
            raise BoundaryError("INVALID_PREPARED")
        if (
            h.kind != "header"
            or h.offset != 0
            or h.next_offset != len(h.raw)
            or not h.raw.endswith(b"\n")
            or h.journal_id in ids
        ):
            raise BoundaryError("INVALID_PREPARED")
        try:
            doc = RecordValidator().parse(h.raw, 0)
        except JournalReadError:
            raise BoundaryError("INVALID_PREPARED") from None
        if (
            doc["record"] != "header"
            or doc["journal_id"] != h.journal_id
            or doc["journal_seq"] != p.new_journal_seq
            or doc["deployment_id"] != p.binding.deployment_id
            or doc["instance_id"] != p.binding.instance_id
            or doc["profile_revision"] != p.binding.profile_revision
            or doc["previous_journal_id"] is not None
        ):
            raise BoundaryError("INVALID_PREPARED")

    def _verify_raw(self, scope: str, p: PreparedTransition) -> None:
        """Сравнивает digest с непрерывными долговечными raw, а не claimed hash."""
        for j in p.journals:
            generation = self.db.execute(
                "SELECT next_offset,journal_seq FROM low_raw_generations "
                "WHERE source_key=? AND journal_id=?",
                (j.source_key, j.journal_id),
            ).fetchone()
            if generation != (j.lf_end, str(j.journal_seq)):
                raise BoundaryError("RAW_CONFLICT")
            digest = sha256()
            end = 0
            last_kind = None
            for offset, next_offset, kind, raw in self.db.execute(
                "SELECT offset,next_offset,kind,raw FROM low_raw_records "
                "WHERE source_key=? AND journal_id=? ORDER BY offset",
                (j.source_key, j.journal_id),
            ):
                if offset != end or next_offset != offset + len(raw) or not raw.endswith(b"\n"):
                    raise BoundaryError("RAW_CONFLICT")
                digest.update(raw)
                end, last_kind = next_offset, kind
            if (
                end != j.lf_end
                or digest.digest() != j.prefix_digest
                or (last_kind == "footer") != j.footer_present
            ):
                raise BoundaryError("RAW_CONFLICT")
            used = self.db.execute(
                # Все bytes уже есть в raw и пакете; это не проверка автора snapshot.
                "SELECT writer,boundary_id FROM low_boundary_journals "
                "WHERE scope=? AND journal_id=?",
                (scope, j.journal_id),
            ).fetchone()
            if used is not None and used != (p.old_writer, None):
                raise BoundaryError("CONFLICT")
            digest.update(j.tail)
            if digest.digest() != j.snapshot_digest:
                raise BoundaryError("INVALID_PREPARED")
        reserved = {
            row[0]
            for row in self.db.execute(
                "SELECT journal_id FROM low_boundary_journals "
                "WHERE scope=? AND writer=? AND boundary_id IS NULL",
                (scope, p.old_writer),
            )
        }
        if not reserved <= {j.journal_id for j in p.journals}:
            raise BoundaryError("CONFLICT")

    def accept_prepared_transition(self, p: PreparedTransition) -> Literal["accepted", "repeated"]:
        """Точный replay проверяется раньше версии и лимитов; accepted только после COMMIT."""
        if self.db.in_transaction:
            raise BoundaryError("INVALID_TRANSACTION")
        # Проверяем область до replay: чужой агент не может получить repeated.
        if (
            type(p) is not PreparedTransition
            or p.agent_id != self.raw.agent_id
            or type(p.binding) is not JournalBinding
            or type(p.boundary_id) is not str
            or type(p.proof) is not bytes
        ):
            raise BoundaryError("INVALID_PREPARED")
        scope = self.scope(p.binding)
        try:
            self.db.execute("BEGIN IMMEDIATE")
            prior = self.db.execute(
                "SELECT proof FROM low_boundary_proofs WHERE scope=? AND boundary_id=?",
                (scope, p.boundary_id),
            ).fetchone()
            if prior is not None:
                if prior != (p.proof,):
                    raise BoundaryError("CONFLICT")
                self._commit()
                return "repeated"
            self._validate(p)
            current = self.db.execute(
                "SELECT revision,version,writer,container,boundary_id,boundary_seq "
                "FROM low_boundary_bindings WHERE scope=?",
                (scope,),
            ).fetchone()
            if current is None:
                raise BoundaryError("BINDING_REQUIRED")
            expected = (
                p.binding.profile_revision,
                p.expected_version,
                p.old_writer,
                p.old_container,
                p.previous_id,
                p.seq - 1,
            )
            if current != expected:
                raise BoundaryError("CONFLICT")
            old = self.db.execute(
                "SELECT container,state FROM low_boundary_writers WHERE scope=? AND writer=?",
                (scope, p.old_writer),
            ).fetchone()
            if old != (p.old_container, "active"):
                raise BoundaryError("CONFLICT")
            if (
                self.db.execute(
                    "SELECT 1 FROM low_boundary_writers "
                    "WHERE scope=? AND (writer=? OR container=?)",
                    (scope, p.new_writer, p.new_container),
                ).fetchone()
                or self.db.execute(
                    "SELECT 1 FROM low_boundary_journals WHERE scope=? AND journal_id=?",
                    (scope, p.header.journal_id),
                ).fetchone()
                or self.db.execute(
                    "SELECT 1 FROM low_raw_generations WHERE source_key=? AND journal_id=?",
                    (self.raw.source_key(p.binding), p.header.journal_id),
                ).fetchone()
            ):
                raise BoundaryError("CONFLICT")
            self._verify_raw(scope, p)
            extra = len(p.proof) + sum(len(j.tail) for j in p.journals)
            used = self.db.execute("SELECT bytes FROM low_storage_usage").fetchone()[0]
            if used + extra + len(p.header.raw) > self.raw.max_raw_bytes:
                raise BoundaryError("STORAGE_LIMIT")
            self.db.execute(
                "INSERT INTO low_boundary_proofs VALUES (?,?,?,?,?,?)",
                (
                    scope,
                    p.boundary_id,
                    p.seq,
                    p.previous_id,
                    p.proof,
                    sha256(p.proof).digest(),
                ),
            )
            self._checkpoint("proof")
            for j in p.journals:
                self.db.execute(
                    "INSERT INTO low_boundary_pins VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        scope,
                        p.boundary_id,
                        j.journal_id,
                        p.old_writer,
                        j.source_key,
                        str(j.journal_seq),
                        j.snapshot_ref,
                        j.snapshot_digest,
                        j.snapshot_size,
                        j.lf_end,
                        j.prefix_digest,
                        int(j.footer_present),
                    ),
                )
                self.db.execute(
                    "INSERT INTO low_boundary_journals VALUES (?,?,?,?) "
                    "ON CONFLICT(scope,journal_id) DO UPDATE SET boundary_id=excluded.boundary_id",
                    (scope, j.journal_id, p.old_writer, p.boundary_id),
                )
                self._checkpoint("pins")
                self.db.execute(
                    "INSERT INTO low_boundary_tails VALUES (?,?,?,?,?,?)",
                    (
                        scope,
                        j.journal_id,
                        j.lf_end,
                        len(j.tail),
                        sha256(j.tail).digest(),
                        j.tail,
                    ),
                )
                self._checkpoint("tails")
            self.db.execute(
                "UPDATE low_boundary_writers SET state='sealed',sealed_by=? "
                "WHERE scope=? AND writer=?",
                (p.boundary_id, scope, p.old_writer),
            )
            self._checkpoint("sealed")
            self.db.execute(
                "INSERT INTO low_boundary_writers VALUES (?,?,?,'active',?,NULL)",
                (
                    scope,
                    p.new_writer,
                    p.new_container,
                    p.boundary_id,
                ),
            )
            self.db.execute(
                "INSERT INTO low_boundary_journals VALUES (?,?,?,NULL)",
                (
                    scope,
                    p.header.journal_id,
                    p.new_writer,
                ),
            )
            self._checkpoint("writer")
            self.raw.append_in_transaction(self.db, p.binding, p.header)
            self._checkpoint("header")
            self.db.execute(
                "UPDATE low_boundary_bindings SET version=version+1,writer=?,container=?,"
                "boundary_id=?,boundary_seq=? WHERE scope=?",
                (
                    p.new_writer,
                    p.new_container,
                    p.boundary_id,
                    p.seq,
                    scope,
                ),
            )
            self._checkpoint("binding")
            self.db.execute(
                "UPDATE low_storage_usage SET bytes=bytes+? WHERE singleton=1", (extra,)
            )
            self._checkpoint("usage")
            self._checkpoint("before_commit")
            self._commit()
        except BaseException as error:
            try:
                self.db.rollback()
            except sqlite3.Error:
                raise BoundaryError("DB_ERROR") from None
            if isinstance(error, sqlite3.Error):
                code = (
                    "WRITER_BUSY"
                    if getattr(error, "sqlite_errorcode", 0) & 0xFF
                    in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
                    else "DB_ERROR"
                )
                if isinstance(error, sqlite3.IntegrityError):
                    code = "CONFLICT"
                raise BoundaryError(code) from None
            if isinstance(error, RawJournalError):
                raise BoundaryError(error.code) from None
            raise
        self._checkpoint("after_commit")
        return "accepted"
