"""Исполняемый контракт из handoff: настоящая SQLite, независимый оракул и IPC crash."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from threading import Barrier, Event

import pytest
from test_low_journal_reader_contract import JOURNAL, binding, event, footer, header, line, read

from agent.telemetry.low_journal.boundary import (
    BoundaryError,
    BoundaryLimits,
    BoundaryRepository,
    PreparedJournal,
    PreparedTransition,
)
from agent.telemetry.low_journal.collector import LowJournalCollector
from agent.telemetry.low_journal.conversion import LowRawConverter
from agent.telemetry.low_journal.durable import RawJournalError, RawJournalRepository
from agent.telemetry.low_journal.models import JournalBinding, JournalRecord
from agent.telemetry.low_journal.production import LowProductionMapper
from agent.telemetry.schemas import Ack, RejectedEvent
from agent.telemetry.spool import SpoolRepository
from agent.telemetry.storage_schema import StorageMigration

AGENT = "00000000-0000-4000-8000-000000000099"
NEW = "00000000-0000-4000-8000-000000000088"
THIRD = "00000000-0000-4000-8000-000000000077"
TAIL = b'\xff\x80{"unfinished":true'


def record(document, offset=0, journal_id=None):
    raw = line(document)
    return JournalRecord(
        document["record"],
        journal_id or document.get("journal_id", JOURNAL),
        offset,
        offset + len(raw),
        raw,
    )


def snapshot(path):
    """Снимок из отдельного connection; порядок логических строк, не байты DB/WAL."""
    with sqlite3.connect(path) as db:
        tables = [
            r[0]
            for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        ]
        result = {}
        for table in tables:
            rows = db.execute(f'SELECT * FROM "{table}"').fetchall()
            result[table] = sorted(rows, key=repr)
        result["schema"] = db.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
        ).fetchall()
        return result


class Scenario:
    """Явная тестовая привязка не моделирует production anchor или доверие."""

    def __init__(self, path, *, seed=True):
        self.path = Path(path)
        self.raw = RawJournalRepository(path, AGENT)
        self.repo = BoundaryRepository(self.raw)
        self.binding = binding()
        self.old = record(header())
        if seed:
            self.raw.append(self.binding, self.old)
            scope = self.repo.scope(self.binding)
            with self.raw.db:
                self.raw.db.execute(
                    "INSERT INTO low_boundary_bindings VALUES (?,?,?,?,?,?,?)",
                    (scope, 1, 0, "A", "container-A", None, 0),
                )
                self.raw.db.execute(
                    "INSERT INTO low_boundary_writers VALUES (?,?,?,'active',NULL,NULL)",
                    (scope, "A", "container-A"),
                )
        self.p = self.transition()

    def pin(self, old=None, seq=1, tail=TAIL):
        old = old or self.old
        return PreparedJournal(
            self.raw.source_key(self.binding),
            old.journal_id,
            seq,
            f"snapshot-{seq}",
            sha256(old.raw + tail).digest(),
            len(old.raw) + len(tail),
            len(old.raw),
            sha256(old.raw).digest(),
            False,
            tail,
        )

    def transition(self):
        return PreparedTransition(
            AGENT,
            self.binding,
            0,
            "P1",
            1,
            None,
            b'{"boundary":"P1"}',
            "A",
            "container-A",
            "B",
            "container-B",
            (self.pin(),),
            record(header(journal_id=NEW, journal_seq=2)),
            2,
        )

    def second(self):
        return replace(
            self.p,
            expected_version=1,
            boundary_id="P2",
            seq=2,
            previous_id="P1",
            proof=b'{"boundary":"P2"}',
            old_writer="B",
            old_container="container-B",
            new_writer="C",
            new_container="container-C",
            journals=(self.pin(self.p.header, 2),),
            header=record(header(journal_id=THIRD, journal_seq=3)),
            new_journal_seq=3,
        )

    def close(self):
        self.raw.close()


@pytest.fixture
def scenario(tmp_path):
    s = Scenario(tmp_path / "spool.db")
    yield s
    s.close()


def reject_unchanged(s, prepared, code=None):
    before = snapshot(s.path)
    with pytest.raises(BoundaryError) as caught:
        s.repo.accept_prepared_transition(prepared)
    if code:
        assert caught.value.code == code
    assert snapshot(s.path) == before
    assert not s.raw.db.in_transaction


def legacy(path):
    """Старая схема задана буквально; текущие constructors не создают legacy fixture."""
    with sqlite3.connect(path) as db:
        db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE checkpoints(source_id TEXT PRIMARY KEY,next_offset INTEGER NOT NULL);
            CREATE TABLE pending(queue_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                                 event_id TEXT UNIQUE NOT NULL,payload TEXT NOT NULL);
            CREATE TABLE quarantine(quarantine_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT UNIQUE NOT NULL,payload TEXT NOT NULL,batch_id TEXT NOT NULL,
                code TEXT NOT NULL,batch_index INTEGER NOT NULL,rejected_at TEXT NOT NULL);
            CREATE TABLE batch_snapshots(batch_id TEXT PRIMARY KEY,payload BLOB NOT NULL);
            CREATE TABLE low_conversion_ledger(source_key TEXT NOT NULL,journal_id TEXT NOT NULL,
                offset INTEGER NOT NULL,next_offset INTEGER NOT NULL,kind TEXT NOT NULL,
                raw_digest BLOB NOT NULL,mapper_version TEXT NOT NULL,result TEXT NOT NULL,
                event_id TEXT UNIQUE,canonical_payload TEXT,
                PRIMARY KEY(source_key,journal_id,offset));
            CREATE TABLE low_raw_generations(source_key TEXT NOT NULL,journal_id TEXT NOT NULL,
                next_offset INTEGER NOT NULL,journal_seq TEXT NOT NULL,previous_journal_id TEXT,
                UNIQUE(source_key,journal_seq),PRIMARY KEY(source_key,journal_id));
            CREATE UNIQUE INDEX low_raw_successor ON
                low_raw_generations(source_key,previous_journal_id)
                WHERE previous_journal_id IS NOT NULL;
            CREATE TABLE low_raw_usage(singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                bytes INTEGER NOT NULL);
            CREATE TABLE low_raw_records(source_key TEXT NOT NULL,journal_id TEXT NOT NULL,
                offset INTEGER NOT NULL,next_offset INTEGER NOT NULL,
                kind TEXT NOT NULL,raw BLOB NOT NULL,
                PRIMARY KEY(source_key,journal_id,offset));
            CREATE TABLE low_raw_locations(source_key TEXT NOT NULL,path TEXT NOT NULL,
                journal_id TEXT NOT NULL,PRIMARY KEY(source_key,path));
        """)
        raw = line(header())
        key = json.dumps(
            [AGENT, binding().deployment_id, binding().instance_id, 1], separators=(",", ":")
        )
        db.executemany(
            "INSERT INTO metadata VALUES (?,?)", [("agent_id", AGENT), ("foreign", "kept")]
        )
        db.execute("INSERT INTO checkpoints VALUES ('source',123)")
        db.executemany(
            "INSERT INTO pending VALUES (?,?,?)",
            [(9, "e2", "canonical-2"), (3, "e1", "canonical-1")],
        )
        db.execute("INSERT INTO quarantine VALUES (4,'q','canonical-q','batch','CODE',0,'now')")
        db.execute("INSERT INTO batch_snapshots VALUES ('batch',?)", (b"\x00\xffsnapshot",))
        db.execute(
            "INSERT INTO low_conversion_ledger VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                key,
                JOURNAL,
                0,
                len(raw),
                "header",
                sha256(raw).digest(),
                "v1",
                "service",
                None,
                None,
            ),
        )
        db.execute(
            "INSERT INTO low_raw_records VALUES (?,?,?,?,?,?)",
            (key, JOURNAL, 0, len(raw), "header", raw),
        )
        db.execute(
            "INSERT INTO low_raw_generations VALUES (?,?,?,?,?)",
            (key, JOURNAL, len(raw), "1", None),
        )
        db.execute("INSERT INTO low_raw_locations VALUES (?,?,?)", (key, "original-path", JOURNAL))
        db.execute("INSERT INTO low_raw_usage VALUES (1,?)", (len(raw),))
    return raw


def test_m01_m02_m05_m06_migration_and_no_bootstrap(tmp_path):
    empty = tmp_path / "empty.db"
    r = RawJournalRepository(empty, AGENT)
    r.close()
    before = snapshot(empty)
    r = RawJournalRepository(empty, AGENT)
    r.close()
    assert snapshot(empty) == before
    assert before["low_boundary_bindings"] == []
    path = tmp_path / "legacy.db"
    raw = legacy(path)
    before = snapshot(path)
    r = RawJournalRepository(path, AGENT, max_raw_bytes=1)
    try:
        after = snapshot(path)
        for table, rows in before.items():
            if table not in {"schema", "metadata"}:
                assert after[table] == rows
        assert set(before["metadata"]) <= set(after["metadata"])
        assert after["low_storage_usage"] == [(1, len(raw))]
        assert r.append(binding(), record(header())) is False
        with pytest.raises(RawJournalError, match="STORAGE_LIMIT"):
            r.append(binding(), record(event(), len(raw)))
        j = PreparedJournal(
            r.source_key(binding()),
            JOURNAL,
            1,
            "ref",
            sha256(raw).digest(),
            len(raw),
            len(raw),
            sha256(raw).digest(),
            False,
            b"",
        )
        p = PreparedTransition(
            AGENT,
            binding(),
            0,
            "P1",
            1,
            None,
            b"proof",
            "A",
            "ca",
            "B",
            "cb",
            (j,),
            record(header(journal_id=NEW, journal_seq=2)),
            2,
        )
        with pytest.raises(BoundaryError, match="BINDING_REQUIRED"):
            BoundaryRepository(r).accept_prepared_transition(p)
        assert snapshot(path) == after
    finally:
        r.close()


@pytest.mark.parametrize("step", range(len(StorageMigration.STATEMENTS) + 4))
def test_m03_atomic_migration_each_mutation(tmp_path, monkeypatch, step):
    path = tmp_path / "legacy.db"
    legacy(path)
    before = snapshot(path)

    def fail(self, actual):
        if actual == step:
            raise RuntimeError("injected")

    with monkeypatch.context() as patch:
        patch.setattr(StorageMigration, "_checkpoint", fail)
        with pytest.raises(RuntimeError, match="injected"):
            RawJournalRepository(path, AGENT)
    assert snapshot(path) == before
    r = RawJournalRepository(path, AGENT)
    assert r.db.execute(
        "SELECT value FROM metadata WHERE key=?", (StorageMigration.KEY,)
    ).fetchone() == ("1",)
    r.close()


@pytest.mark.parametrize("constructor", [RawJournalRepository, SpoolRepository])
@pytest.mark.parametrize(
    "problem,code", [("version", "UNSUPPORTED_STORAGE_VERSION"), ("owner", "OWNER_MISMATCH")]
)
def test_m04_m07_refuse_without_mutation(tmp_path, constructor, problem, code):
    path = tmp_path / "legacy.db"
    legacy(path)
    with sqlite3.connect(path) as db:
        if problem == "version":
            db.execute("INSERT INTO metadata VALUES (?, '999')", (StorageMigration.KEY,))
        else:
            db.execute("UPDATE metadata SET value=? WHERE key='agent_id'", (NEW,))
    before = snapshot(path)
    with pytest.raises(ValueError, match=code):
        constructor(path, AGENT)
    assert snapshot(path) == before


@pytest.mark.parametrize("commit", [False, True])
def test_r02_r03_outer_transaction(tmp_path, commit):
    path = tmp_path / "spool.db"
    r = RawJournalRepository(path, AGENT)
    before = snapshot(path)
    try:
        r.db.execute("BEGIN IMMEDIATE")
        assert r.append_in_transaction(r.db, binding(), record(header()), path=tmp_path / "hint")
        assert snapshot(path) == before
        if commit:
            r.db.commit()
            after = snapshot(path)
            assert (
                len(after["low_raw_records"])
                == len(after["low_raw_generations"])
                == len(after["low_raw_locations"])
                == 1
            )
            assert after["low_storage_usage"] == [(1, len(line(header())))]
        else:
            r.db.rollback()
            assert snapshot(path) == before
    finally:
        r.close()


def test_r01_r04_r05_transaction_contract(tmp_path, monkeypatch):
    path = tmp_path / "raw.db"
    r = RawJournalRepository(path, AGENT)
    try:
        with pytest.raises(RawJournalError, match="INVALID_TRANSACTION"):
            r.append_in_transaction(r.db, binding(), record(header()))
        with sqlite3.connect(path) as foreign:
            foreign.execute("BEGIN IMMEDIATE")
            with pytest.raises(RawJournalError, match="INVALID_TRANSACTION"):
                r.append_in_transaction(foreign, binding(), record(header()))
        before = snapshot(path)
        r.db.execute("BEGIN IMMEDIATE")
        r.db.execute("UPDATE metadata SET value=? WHERE key='agent_id'", (NEW,))
        with pytest.raises(RawJournalError, match="OWNER_MISMATCH"):
            r.append_in_transaction(r.db, binding(), record(header()))
        r.db.rollback()

        def fail(stage):
            if stage == "raw_inserted":
                raise RuntimeError("injected")

        with monkeypatch.context() as patch:
            patch.setattr(r, "_checkpoint", fail)
            r.db.execute("BEGIN IMMEDIATE")
            with pytest.raises(RuntimeError):
                r.append_in_transaction(r.db, binding(), record(header()))
            r.db.rollback()
        assert snapshot(path) == before
        assert r.append(binding(), record(header()))
        assert not r.append(binding(), record(header()))
        for invalid in (
            record(header(opened_at="2026-10-12T00:00:00Z")),
            replace(record(header()), offset=1),
            record(header(instance_id=NEW), journal_id=NEW),
        ):
            with pytest.raises(RawJournalError):
                r.append(binding(), invalid)
    finally:
        r.close()


def test_b01_b03_b04_q05_replay(scenario):
    s = scenario
    assert s.repo.accept_prepared_transition(s.p) == "accepted"
    expected = len(s.old.raw) + len(s.p.proof) + len(TAIL) + len(s.p.header.raw)
    state = snapshot(s.path)
    assert state["low_storage_usage"] == [(1, expected)]
    assert s.raw.checkpoint(s.binding, NEW) == len(s.p.header.raw)
    assert state["low_boundary_tails"][0][2:] == (
        len(s.old.raw),
        len(TAIL),
        sha256(TAIL).digest(),
        TAIL,
    )
    assert [(r[1], r[3]) for r in state["low_boundary_writers"]] == [
        ("A", "sealed"),
        ("B", "active"),
    ]
    assert s.repo.accept_prepared_transition(s.p) == "repeated"
    assert snapshot(s.path) == state
    s.raw.close()
    s.raw = RawJournalRepository(s.path, AGENT)
    s.repo = BoundaryRepository(s.raw)
    assert s.repo.accept_prepared_transition(s.p) == "repeated"
    assert snapshot(s.path) == state
    assert s.repo.accept_prepared_transition(s.second()) == "accepted"
    state = snapshot(s.path)
    s.raw.max_raw_bytes = 1
    s.repo.limits = BoundaryLimits(1, 1, 1)
    assert s.repo.accept_prepared_transition(s.p) == "repeated"
    assert snapshot(s.path) == state
    assert state["low_boundary_bindings"][0][2:5] == (2, "C", "container-C")


@pytest.mark.parametrize(
    "change",
    [
        {"proof": b"different"},
        {"boundary_id": "other"},
        {"seq": 3},
        {"seq": 0},
        {"previous_id": "other"},
        {"expected_version": 99},
        {"old_writer": "X"},
        {"old_container": "X"},
        {"new_writer": "A"},
        {"new_container": "container-A"},
        {"agent_id": NEW},
        {"binding": JournalBinding(binding().deployment_id, binding().instance_id, 2)},
        {"binding": JournalBinding(binding().deployment_id, NEW, 1)},
    ],
)
def test_b05_b06_b07_conflicts(scenario, change):
    s = scenario
    if change in ({"proof": b"different"}, {"boundary_id": "other"}):
        s.repo.accept_prepared_transition(s.p)
    reject_unchanged(s, replace(s.p, **change))


@pytest.mark.parametrize(
    "damage", ["missing", "digest", "progress", "gap", "footer", "foreign-key", "taken-journal"]
)
def test_b07_b08_raw_evidence(scenario, damage):
    s = scenario
    j = s.p.journals[0]
    if damage == "missing":
        j = replace(j, lf_end=j.lf_end + 1, snapshot_size=j.snapshot_size + 1)
    elif damage == "digest":
        j = replace(j, prefix_digest=b"x" * 32)
    elif damage == "progress":
        s.raw.append(s.binding, record(event(), len(s.old.raw)))
    elif damage == "gap":
        with s.raw.db:
            s.raw.db.execute("DELETE FROM low_raw_records")
    elif damage == "footer":
        j = replace(j, footer_present=True)
    elif damage == "foreign-key":
        j = replace(j, source_key="foreign")
    else:
        s.raw.append(s.binding, s.p.header)
    reject_unchanged(s, replace(s.p, journals=(j,)))


@pytest.mark.parametrize(
    "damage",
    ["record", "identity", "seq", "offset", "newline", "duplicate", "size", "mutable", "tail-type"],
)
def test_b09_structural_disagreement(scenario, damage):
    s = scenario
    p = s.p
    if damage == "record":
        p = replace(p, header=record(event(), journal_id=NEW))
    elif damage == "identity":
        p = replace(p, header=replace(p.header, journal_id=THIRD))
    elif damage == "seq":
        p = replace(p, new_journal_seq=4)
    elif damage == "offset":
        p = replace(p, header=replace(p.header, offset=1))
    elif damage == "newline":
        p = replace(p, header=replace(p.header, raw=p.header.raw[:-1]))
    elif damage == "duplicate":
        p = replace(p, journals=p.journals * 2)
    elif damage == "size":
        p = replace(p, journals=(replace(p.journals[0], snapshot_size=1),))
    elif damage == "mutable":
        p = replace(p, journals=list(p.journals))
    else:
        p = replace(p, journals=(replace(p.journals[0], tail=bytearray(TAIL)),))
    reject_unchanged(s, p, "INVALID_PREPARED")


@pytest.mark.parametrize(
    "dimension,amount,allowed",
    [
        ("proof", 1024 * 1024, True),
        ("proof", 1024 * 1024 + 1, False),
        ("tail", 4 * 1024 * 1024, True),
        ("tail", 4 * 1024 * 1024 + 1, False),
        ("journals", 0, False),
        ("journals", 1, True),
        ("journals", 64, True),
        ("journals", 65, False),
    ],
)
def test_q01_q02_q03_limits(scenario, dimension, amount, allowed):
    s = scenario
    p = s.p
    if dimension == "proof":
        p = replace(p, proof=b"p" * amount)
    elif dimension == "tail":
        j = replace(
            p.journals[0],
            tail=b"\xff" * amount,
            snapshot_size=len(s.old.raw) + amount,
            snapshot_digest=sha256(s.old.raw + b"\xff" * amount).digest(),
        )
        p = replace(p, journals=(j,))
    else:
        journals = []
        for n in range(amount):
            jid = f"00000000-0000-4000-8000-{1000 + n:012d}"
            old = record(header(journal_id=jid, journal_seq=n + 3))
            s.raw.append(s.binding, old)
            journals.append(s.pin(old, n + 3, b"" if n == 0 else TAIL))
        p = replace(
            p,
            journals=tuple(journals),
            header=record(header(journal_id=NEW, journal_seq=100)),
            new_journal_seq=100,
        )
    if allowed:
        expected = (
            snapshot(s.path)["low_storage_usage"][0][1]
            + len(p.proof)
            + sum(len(j.tail) for j in p.journals)
            + len(p.header.raw)
        )
        assert s.repo.accept_prepared_transition(p) == "accepted"
        after = snapshot(s.path)
        assert after["low_storage_usage"] == [(1, expected)]
        assert sorted(r[-1] for r in after["low_boundary_tails"]) == sorted(
            j.tail for j in p.journals
        )
    else:
        reject_unchanged(s, p, "STORAGE_LIMIT")


@pytest.mark.parametrize("short", [0, 1])
def test_q04_q06_total_budget(scenario, short):
    s = scenario
    expected = len(s.old.raw) + len(s.p.proof) + len(TAIL) + len(s.p.header.raw)
    s.raw.max_raw_bytes = expected - short
    if short:
        reject_unchanged(s, s.p, "STORAGE_LIMIT")
    else:
        assert s.repo.accept_prepared_transition(s.p) == "accepted"
        before = snapshot(s.path)
        with pytest.raises(RawJournalError, match="STORAGE_LIMIT"):
            s.raw.append(s.binding, record(event(), len(s.p.header.raw), NEW))
        assert snapshot(s.path) == before


@pytest.mark.parametrize("case", ["same", "same-busy", "fork", "raw"])
def test_q07_q08_q09_connections_compete(scenario, case):
    s = scenario
    barrier = Barrier(2)
    locked = Event()
    release = Event()
    if case == "raw":
        s.raw.max_raw_bytes = len(s.old.raw) + len(s.p.proof) + len(TAIL) + len(s.p.header.raw)

    def run(index, synchronize=True):
        r = RawJournalRepository(s.path, AGENT, max_raw_bytes=s.raw.max_raw_bytes)
        try:
            if synchronize:
                barrier.wait(timeout=10)
            if case == "same-busy" and index == 1 and synchronize:
                assert locked.wait(timeout=10), "Первый writer не достиг proof INSERT"
            if index == 1 and case == "raw":
                try:
                    r.append(s.binding, record(header(journal_id=THIRD, journal_seq=3)))
                    return "raw"
                except RawJournalError as error:
                    return error.code
            p = s.p
            if index == 1 and case == "fork":
                p = replace(
                    p,
                    boundary_id="fork",
                    proof=b"fork",
                    new_writer="C",
                    new_container="container-C",
                )
            try:
                repo = BoundaryRepository(r)
                if case == "same-busy" and index == 0 and synchronize:

                    def hold(stage):
                        if stage == "proof":
                            locked.set()
                            assert release.wait(timeout=10), "Второй writer не завершил попытку"

                    repo._checkpoint = hold
                return repo.accept_prepared_transition(p)
            except BoundaryError as error:
                return error.code
        finally:
            if case == "same-busy" and index == 1 and synchronize:
                release.set()
            r.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, [0, 1]))
    if case == "same-busy":
        assert results == ["accepted", "WRITER_BUSY"]
    # После завершения обоих потоков write lock снят. Один явный retry
    # разрешает временный busy, не ослабляя итоговый семантический оракул.
    # Старый публичный raw append сохраняет общий безопасный код DB_ERROR.
    for index, result in enumerate(results):
        if result in {"WRITER_BUSY", "DB_ERROR"}:
            results[index] = run(index, synchronize=False)
    if case in {"same", "same-busy"}:
        assert sorted(results) == ["accepted", "repeated"]
    elif case == "fork":
        assert sorted(results) == ["CONFLICT", "accepted"]
    else:
        assert "STORAGE_LIMIT" in results
        assert len(set(results) & {"raw", "accepted"}) == 1
        assert snapshot(s.path)["low_storage_usage"][0][1] <= s.raw.max_raw_bytes


FAULTS = (
    "proof",
    "pins",
    "tails",
    "sealed",
    "writer",
    "raw_inserted",
    "raw_checkpoint",
    "header",
    "binding",
    "usage",
    "before_commit",
    "commit",
)


@pytest.mark.parametrize("point", FAULTS)
def test_e_transaction_faults_and_commit(scenario, monkeypatch, point):
    s = scenario
    before = snapshot(s.path)

    def fail(stage):
        if stage == point:
            raise sqlite3.OperationalError("injected private SQL /path raw secret")

    with monkeypatch.context() as patch:
        patch.setattr(s.repo, "_checkpoint", fail)
        patch.setattr(s.raw, "_checkpoint", fail)
        if point == "commit":
            patch.setattr(s.repo, "_commit", lambda: fail("commit"))
        with pytest.raises(BoundaryError, match="DB_ERROR") as caught:
            s.repo.accept_prepared_transition(s.p)
        assert "secret" not in str(caught.value)
    assert snapshot(s.path) == before
    s.close()
    s.raw = RawJournalRepository(s.path, AGENT)
    s.repo = BoundaryRepository(s.raw)
    assert s.repo.accept_prepared_transition(s.p) == "accepted"
    assert s.repo.accept_prepared_transition(s.p) == "repeated"


def crash_worker(path, point):
    """Печать с flush — IPC подтверждение, os._exit не вызывает cleanup SQLite."""
    s = Scenario(path, seed=False)

    def kill(stage):
        if stage == point:
            print(f"reached:{point}", flush=True)
            os._exit(73)

    s.repo._checkpoint = kill
    s.repo.accept_prepared_transition(s.p)
    raise AssertionError("crash point not reached")


@pytest.mark.parametrize("point", ["tails", "after_commit"])
def test_e_real_process_crash_ipc(scenario, point):
    s = scenario
    before = snapshot(s.path)
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0,'tests/telemetry'); "
            "from test_boundary_storage import crash_worker; crash_worker(sys.argv[1],sys.argv[2])",
            str(s.path),
            point,
        ],
        capture_output=True,
        timeout=20,
    )
    assert process.returncode == 73, process.stderr.decode(errors="replace")
    assert process.stdout.strip() == f"reached:{point}".encode()
    s.close()
    s.raw = RawJournalRepository(s.path, AGENT)
    s.repo = BoundaryRepository(s.raw)
    assert s.raw.db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    if point == "tails":
        assert snapshot(s.path) == before
        assert s.repo.accept_prepared_transition(s.p) == "accepted"
    else:
        assert s.repo.accept_prepared_transition(s.p) == "repeated"
    after = snapshot(s.path)
    assert len(after["low_boundary_proofs"]) == len(after["low_boundary_tails"]) == 1
    assert after["low_storage_usage"] == [
        (1, len(s.old.raw) + len(s.p.header.raw) + len(s.p.proof) + len(TAIL))
    ]


def test_e_busy_retry_and_injected_full(scenario, monkeypatch):
    s = scenario
    with sqlite3.connect(s.path) as lock:
        lock.execute("BEGIN IMMEDIATE")
        reject_unchanged(s, s.p, "WRITER_BUSY")
        lock.rollback()
    before = snapshot(s.path)

    def full(stage):
        if stage == "writer":
            error = sqlite3.OperationalError("injected SQLITE_FULL")
            error.sqlite_errorcode = sqlite3.SQLITE_FULL
            raise error

    with monkeypatch.context() as patch:
        patch.setattr(s.repo, "_checkpoint", full)
        reject_unchanged(s, s.p, "DB_ERROR")
    assert snapshot(s.path) == before
    assert s.repo.accept_prepared_transition(s.p) == "accepted"


def test_b02_f_sample_885_tail_and_unconnected_consumer(tmp_path):
    """Поздний null ждёт при наличии boundary-таблиц; старый raw конвертируется."""
    directory = Path(__file__).resolve().parent / "fixtures/boundary"
    old_path = directory / "old-crash.jsonl"
    new_path = directory / "new-process.jsonl"
    old_doc = json.loads(old_path.read_bytes().splitlines()[0])
    bound = JournalBinding(old_doc["deployment_id"], old_doc["instance_id"], 1)
    path = tmp_path / "sample.db"
    raw = RawJournalRepository(path, AGENT)
    spool = SpoolRepository(path, AGENT)
    try:
        repo = BoundaryRepository(raw)
        collector = LowJournalCollector(raw, bound)
        assert collector.step([old_path]).saved_records == 3
        assert raw.checkpoint(bound, old_doc["journal_id"]) == 885
        assert collector.step([old_path, new_path]).code == "MISSING_PREDECESSOR"
        assert not snapshot(path)["low_boundary_bindings"]
        mapper = LowProductionMapper(AGENT)
        converter = LowRawConverter(spool, mapper, mapper.version)
        assert converter.step(bound).records == 3
        assert spool.count() == 2
        before = snapshot(path)
        with raw.db:
            scope = repo.scope(bound)
            raw.db.execute(
                "INSERT INTO low_boundary_bindings VALUES (?,?,?,?,?,?,?)",
                (scope, 1, 0, "A", "container-A", None, 0),
            )
            raw.db.execute(
                "INSERT INTO low_boundary_writers VALUES (?,?,?,'active',NULL,NULL)",
                (scope, "A", "container-A"),
            )
        opaque = b"\xff\x80" + b"x" * 23
        assert len(opaque) == 25
        prefix = old_path.read_bytes()
        j = PreparedJournal(
            raw.source_key(bound),
            old_doc["journal_id"],
            1,
            "sample",
            sha256(prefix + opaque).digest(),
            910,
            885,
            sha256(prefix).digest(),
            False,
            opaque,
        )
        new_header = read(new_path, bound)[0][0]
        p = PreparedTransition(
            AGENT,
            bound,
            0,
            "sample-P1",
            1,
            None,
            b"sample-proof",
            "A",
            "container-A",
            "B",
            "container-B",
            (j,),
            new_header,
            2,
        )
        assert repo.accept_prepared_transition(p) == "accepted"
        after = snapshot(path)
        assert after["low_boundary_tails"][0][2:] == (885, 25, sha256(opaque).digest(), opaque)
        assert raw.checkpoint(bound, old_doc["journal_id"]) == 885
        assert after["pending"] == before["pending"]
        assert after["low_conversion_ledger"] == before["low_conversion_ledger"]
        assert collector.step([old_path, new_path]).code == "MISSING_PREDECESSOR"
    finally:
        spool.close()
        raw.close()


def test_b10_empty_and_multiple_tails_footer(scenario):
    s = scenario
    end = len(s.old.raw)
    closing = record(footer(), end)
    s.raw.append(s.binding, closing)
    prefix = s.old.raw + closing.raw
    j1 = replace(
        s.p.journals[0],
        snapshot_size=len(prefix),
        lf_end=len(prefix),
        prefix_digest=sha256(prefix).digest(),
        snapshot_digest=sha256(prefix).digest(),
        footer_present=True,
        tail=b"",
    )
    other = record(header(journal_id=THIRD, journal_seq=3))
    s.raw.append(s.binding, other)
    j2 = s.pin(other, 3, b"\x00\xff\x80")
    expected = len(prefix) + len(other.raw) + len(s.p.header.raw) + len(s.p.proof) + 3
    assert s.repo.accept_prepared_transition(replace(s.p, journals=(j1, j2))) == "accepted"
    after = snapshot(s.path)
    assert after["low_storage_usage"] == [(1, expected)]
    assert sorted(row[-1] for row in after["low_boundary_tails"]) == [b"", b"\x00\xff\x80"]


def test_b11_ack_preserves_boundary_and_ledger(scenario):
    s = scenario
    old_event = record(event(), len(s.old.raw))
    s.raw.append(s.binding, old_event)
    prefix = s.old.raw + old_event.raw
    j = replace(
        s.p.journals[0],
        lf_end=len(prefix),
        snapshot_size=len(prefix) + len(TAIL),
        prefix_digest=sha256(prefix).digest(),
        snapshot_digest=sha256(prefix + TAIL).digest(),
    )
    spool = SpoolRepository(s.path, AGENT)
    try:
        mapper = LowProductionMapper(AGENT)
        converter = LowRawConverter(spool, mapper, mapper.version)
        assert converter.step(s.binding).records == 2
        assert s.repo.accept_prepared_transition(replace(s.p, journals=(j,))) == "accepted"
        new_event = record(event(), len(s.p.header.raw), NEW)
        s.raw.append(s.binding, new_event)
        assert converter.step(s.binding).records == 2
        batch = spool.next_batch()
        assert len(batch.events) == 2
        before = snapshot(s.path)
        ack = Ack(
            batch_id=batch.batch_id,
            accepted_ids=[batch.events[0].event_id],
            rejected=[
                RejectedEvent(
                    index=1,
                    event_id=batch.events[1].event_id,
                    code="INVALID_EVENT",
                    retryable=False,
                )
            ],
        )
        assert spool.acknowledge(batch, ack) == 1
        after = snapshot(s.path)
        for table in before:
            if table.startswith("low_"):
                assert after[table] == before[table]
        assert after["batch_snapshots"] == before["batch_snapshots"]
        assert not after["pending"]
        assert len(after["quarantine"]) == 1
        assert converter.convert(s.binding, old_event) == "repeated"
        assert spool.acknowledge(batch, ack) == 0
    finally:
        spool.close()


def test_pins_immutable_and_no_history_reuse(scenario):
    s = scenario
    reject_unchanged(
        s,
        replace(s.p, journals=(replace(s.p.journals[0], snapshot_digest=b"x" * 32),)),
        "INVALID_PREPARED",
    )
    assert s.repo.accept_prepared_transition(s.p) == "accepted"
    before = snapshot(s.path)
    with pytest.raises(RawJournalError, match="GENERATION_SEALED"):
        s.raw.append(s.binding, record(event(), len(s.old.raw)))
    assert not s.raw.append(s.binding, s.old)
    assert snapshot(s.path) == before
    reject_unchanged(
        s, replace(s.second(), new_writer="A", new_container="container-A"), "CONFLICT"
    )
    other = record(header(journal_id="00000000-0000-4000-8000-000000000066", journal_seq=4))
    s.raw.append(s.binding, other)
    reject_unchanged(s, replace(s.second(), journals=(s.pin(other, 4),)), "CONFLICT")


@pytest.mark.parametrize("value", [None, b"not-a-journal"])
def test_prepared_nested_types_safe(scenario, value):
    reject_unchanged(scenario, replace(scenario.p, journals=(value,)), "INVALID_PREPARED")
    reject_unchanged(scenario, replace(scenario.p, header=value), "INVALID_PREPARED")


def test_invalid_outer_transaction_and_mutable_replay(scenario):
    s = scenario
    s.raw.db.execute("BEGIN IMMEDIATE")
    with pytest.raises(BoundaryError, match="INVALID_TRANSACTION"):
        s.repo.accept_prepared_transition(s.p)
    assert s.raw.db.in_transaction
    s.raw.db.rollback()
    s.repo.accept_prepared_transition(s.p)
    reject_unchanged(s, replace(s.p, proof=bytearray(s.p.proof)), "INVALID_PREPARED")


def test_e_real_sqlite_full_without_filling_disk(scenario):
    """SQLite сама отказывает из-за лимита страниц только этой временной БД."""
    s = scenario
    pages = s.raw.db.execute("PRAGMA page_count").fetchone()[0]
    s.raw.db.execute(f"PRAGMA max_page_count={pages}")
    p = replace(s.p, proof=b"p" * (1024 * 1024))
    reject_unchanged(s, p, "DB_ERROR")
    s.raw.db.execute("PRAGMA max_page_count=1073741823")
    assert s.repo.accept_prepared_transition(p) == "accepted"


def test_r_internal_sql_failure_safe_and_no_transaction_control(scenario):
    s = scenario
    before = snapshot(s.path)
    statements = []
    s.raw.db.execute(
        "CREATE TEMP TRIGGER fail_raw BEFORE INSERT ON low_raw_records "
        "BEGIN SELECT RAISE(ABORT,'private message /path'); END"
    )
    s.raw.db.execute("BEGIN IMMEDIATE")
    s.raw.db.set_trace_callback(statements.append)
    try:
        with pytest.raises(RawJournalError, match="DB_ERROR") as caught:
            s.raw.append_in_transaction(s.raw.db, s.binding, s.p.header)
        assert "private" not in str(caught.value)
        assert s.raw.db.in_transaction
        assert not any(
            sql.upper().startswith(("BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT"))
            for sql in statements
        )
    finally:
        s.raw.db.set_trace_callback(None)
        s.raw.db.rollback()
    assert snapshot(s.path) == before
