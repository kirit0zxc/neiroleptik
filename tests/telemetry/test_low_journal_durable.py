"""Матрица долговечного потребления: реальные bytes, SQLite и аварийный процесс."""

import base64
import json
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from test_low_journal_reader_contract import (
    JOURNAL,
    OTHER,
    binding,
    event,
    footer,
    header,
    line,
    read,
    source,
)
from test_sender import AGENT, seed

from agent.telemetry.low_journal.collector import CollectionLimits, LowJournalCollector
from agent.telemetry.low_journal.durable import RawJournalError, RawJournalRepository
from agent.telemetry.schemas import Ack
from agent.telemetry.spool import OffsetConflict, SpoolRepository
from infra.runtime.sample_low_journal import run_sample


@pytest.fixture
def repo(tmp_path):
    repository = RawJournalRepository(tmp_path / "spool.db", AGENT)
    yield repository
    repository.close()


def stored(repo):
    return repo.db.execute(
        "SELECT journal_id,offset,next_offset,kind,raw FROM low_raw_records ORDER BY rowid"
    ).fetchall()


def test_service_lines_and_idempotency(repo, tmp_path):
    path = source(tmp_path, header(), event(), footer())
    collector = LowJournalCollector(repo, binding())
    first = collector.step([path])
    assert (first.state, first.saved_records, first.saved_bytes) == (
        "progress",
        3,
        path.stat().st_size,
    )
    rows = stored(repo)
    assert [row[3] for row in rows] == ["header", "event", "footer"]
    assert b"".join(row[4] for row in rows) == path.read_bytes()
    assert repo.checkpoint(binding(), JOURNAL) == path.stat().st_size
    assert collector.step([path]).saved_records == 0
    renamed = path.with_name("renamed.jsonl")
    path.rename(renamed)
    assert collector.step([renamed]).saved_records == 0
    assert stored(repo) == rows
    assert repo.db.execute("SELECT bytes FROM low_raw_usage").fetchone()[0] == first.saved_bytes
    assert not repo.db.in_transaction


@pytest.mark.parametrize(
    "table,operation",
    [
        ("low_raw_records", "INSERT"),
        ("low_raw_generations", "INSERT"),
        ("low_raw_generations", "UPDATE"),
        ("low_raw_usage", "UPDATE"),
    ],
)
def test_failure_after_insert_or_checkpoint_rolls_back(repo, tmp_path, table, operation):
    records, _ = read(source(tmp_path, header(), event(), footer()))
    if operation == "UPDATE":
        repo.append(binding(), records[0])
    before = stored(repo)
    position = repo.checkpoint(binding(), JOURNAL)
    repo.db.execute(
        f"CREATE TRIGGER fail AFTER {operation} ON {table} "
        "BEGIN SELECT RAISE(ABORT, 'secret-db-payload'); END"
    )
    with pytest.raises(RawJournalError, match="^DB_ERROR$"):
        repo.append(binding(), records[1] if operation == "UPDATE" else records[0])
    assert stored(repo) == before
    assert repo.checkpoint(binding(), JOURNAL) == position
    assert not repo.db.in_transaction


def test_position_conflict_and_skipped_record(repo, tmp_path):
    records, _ = read(source(tmp_path, header(), event(), footer()))
    repo.append(binding(), records[0])
    assert repo.append(binding(), records[0]) is False
    changed = replace(records[0], raw=records[0].raw.replace(b"opened_at", b"opened_ax"))
    with pytest.raises(RawJournalError, match="POSITION_CONFLICT"):
        repo.append(binding(), changed)
    with pytest.raises(RawJournalError, match="POSITION_CONFLICT"):
        repo.append(binding(), records[2])
    assert len(stored(repo)) == 1
    assert repo.checkpoint(binding(), JOURNAL) == records[0].next_offset


def test_two_consumers_same_position(tmp_path):
    records, _ = read(source(tmp_path, header(), event(), footer()))
    db = tmp_path / "race.db"
    RawJournalRepository(db, AGENT).close()

    def consume():
        local = RawJournalRepository(db, AGENT)
        try:
            return local.append(binding(), records[0])
        finally:
            local.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: consume(), range(2))) == [False, True]
    local = RawJournalRepository(db, AGENT)
    try:
        assert stored(local)[0][4] == records[0].raw
        assert len(stored(local)) == 1
        assert local.checkpoint(binding(), JOURNAL) == records[0].next_offset
    finally:
        local.close()


def test_existing_spool_and_ack_do_not_requeue(tmp_path):
    db = tmp_path / "existing.db"
    spool = SpoolRepository(db, AGENT)
    seed(spool, 2)
    batch = spool.next_batch()
    before = {
        table: spool.db.execute(f"SELECT * FROM {table}").fetchall()
        for table in ("pending", "checkpoints", "quarantine", "batch_snapshots", "metadata")
    }
    raw = RawJournalRepository(db, AGENT)
    try:
        for table, rows in before.items():
            assert raw.db.execute(f"SELECT * FROM {table}").fetchall() == rows
        spool.acknowledge(
            batch,
            Ack(
                batch_id=batch.batch_id,
                accepted_ids=[e.event_id for e in batch.events],
                rejected=[],
            ),
        )
        from agent.telemetry.adapters.low import SourceRecord

        with pytest.raises(OffsetConflict):
            spool.append(SourceRecord("test-source", 0, 1, batch.events[0]))
        assert spool.count() == 0
        path = source(tmp_path, header(), event(), footer())
        collector = LowJournalCollector(raw, binding())
        assert collector.step([path]).saved_records == 3
        assert collector.step([path]).saved_records == 0
        assert spool.count() == 0
        assert len(stored(raw)) == 3
    finally:
        raw.close()
        spool.close()


def test_incomplete_tail_then_append(repo, tmp_path):
    tail = line(event())
    path = source(tmp_path, header(), tail[: len(tail) // 2])
    collector = LowJournalCollector(repo, binding())
    assert collector.step([path]).saved_records == 1
    assert repo.checkpoint(binding(), JOURNAL) == len(line(header()))
    with path.open("ab") as file:
        file.write(tail[len(tail) // 2 :])
    assert collector.step([path]).saved_records == 1
    assert b"".join(row[4] for row in stored(repo)) == path.read_bytes()
    assert collector.step([path]).saved_records == 0


def generations(tmp_path, *, close=True, seq=2, previous=JOURNAL):
    first = tmp_path / "z.jsonl"
    first.write_bytes(
        line(header()) + line(event()) + (line(footer(reason="rotation")) if close else b"")
    )
    second = tmp_path / "a.jsonl"
    second.write_bytes(
        line(header(journal_id=OTHER, journal_seq=seq, previous_journal_id=previous, first_seq=2))
        + line(event(seq=2, event="request"))
        + line(footer(journal_id=OTHER, last_seq=2))
    )
    return first, second


def test_chain_order_connection_and_missing_predecessor(repo, tmp_path):
    first, second = generations(tmp_path)
    collector = LowJournalCollector(repo, binding())
    assert collector.step([second]).code == "MISSING_PREDECESSOR"
    assert stored(repo) == []
    assert collector.step([second, first]).saved_records == 6
    rows = stored(repo)
    assert [row[0] for row in rows] == [JOURNAL] * 3 + [OTHER] * 3
    import json

    assert json.loads(rows[1][4])["connection_id"] == json.loads(rows[4][4])["connection_id"]
    assert collector.step([first, second]).saved_records == 0


@pytest.mark.parametrize("closed", [False, True])
def test_late_null_process_boundary_keeps_previous_checkpoint(repo, tmp_path, closed):
    """Поздний null не разрешает новую цепь даже после завершённого predecessor."""
    first = tmp_path / "first.jsonl"
    first.write_bytes(line(header()) + line(event()) + (line(footer()) if closed else b""))
    collector = LowJournalCollector(repo, binding())
    assert collector.step([first]).saved_records == (3 if closed else 2)
    before = stored(repo)
    checkpoint = repo.checkpoint(binding(), JOURNAL)
    second = tmp_path / "new-process.jsonl"
    second.write_bytes(
        line(header(journal_id=OTHER, journal_seq=2, previous_journal_id=None))
        + line(event())
        + line(footer(journal_id=OTHER))
    )
    assert len(read(second)[0]) == 3
    result = collector.step([first, second])
    assert (result.state, result.code, result.saved_records) == (
        "waiting",
        "MISSING_PREDECESSOR",
        0,
    )
    assert stored(repo) == before
    assert repo.checkpoint(binding(), JOURNAL) == checkpoint
    assert repo.checkpoint(binding(), OTHER) == 0
    assert not repo.db.in_transaction
    assert collector.step([first]).saved_records == 0


def test_open_predecessor_waits(repo, tmp_path):
    first, second = generations(tmp_path, close=False)
    collector = LowJournalCollector(repo, binding())
    assert collector.step([second, first]).code == "OPEN_PREDECESSOR"
    assert repo.checkpoint(binding(), OTHER) == 0
    with first.open("ab") as file:
        file.write(line(footer(reason="rotation")))
    assert collector.step([second, first]).saved_records == 4


@pytest.mark.parametrize("damage", ["branch", "cycle", "seq", "cross_seq"])
def test_bad_chain_stops(repo, tmp_path, damage):
    first, second = generations(tmp_path)
    if damage == "branch":
        extra = tmp_path / "b.jsonl"
        extra.write_bytes(
            line(
                header(
                    journal_id="00000000-0000-4000-8000-000000000009",
                    journal_seq=3,
                    previous_journal_id=JOURNAL,
                    first_seq=3,
                )
            )
        )
        paths = [first, second, extra]
    elif damage == "cycle":
        first.write_bytes(line(header(journal_seq=3, previous_journal_id=OTHER)))
        paths = [first, second]
    elif damage == "seq":
        second.write_bytes(second.read_bytes().replace(b'"seq":2', b'"seq":1'))
        paths = [first, second]
    else:
        second.write_bytes(second.read_bytes().replace(b'"first_seq":2', b'"first_seq":1'))
        paths = [first, second]
    result = LowJournalCollector(repo, binding()).step(paths)
    assert result.state == "error"
    if damage == "seq":
        # Корректный header сохранён; ошибочное событие не продвинуло его позицию.
        assert repo.checkpoint(binding(), OTHER) == len(second.read_bytes().splitlines(True)[0])
        assert [row[3] for row in stored(repo) if row[0] == OTHER] == ["header"]
    else:
        assert repo.checkpoint(binding(), OTHER) == 0
    assert not repo.db.in_transaction


@pytest.mark.parametrize("damage", ["truncate", "empty", "prefix", "id"])
def test_source_changes_no_silent_recovery(repo, tmp_path, damage):
    path = source(tmp_path, header(), event(), footer())
    collector = LowJournalCollector(repo, binding())
    collector.step([path])
    before = stored(repo)
    original = path.read_bytes()
    changed = {
        "truncate": line(header()),
        "empty": b"",
        "prefix": original.replace(b"8080", b"8081"),
        "id": original.replace(JOURNAL.encode(), OTHER.encode()),
    }[damage]
    path.write_bytes(changed)
    assert collector.step([path]).state == "error"
    assert stored(repo) == before
    assert repo.checkpoint(binding(), JOURNAL) == len(original)


def test_new_generation_at_previous_path(repo, tmp_path):
    first, second = generations(tmp_path)
    collector = LowJournalCollector(repo, binding())
    assert collector.step([first]).saved_records == 3
    archive = first.with_name("archive.jsonl")
    first.rename(archive)
    second.rename(first)
    assert collector.step([first, archive]).saved_records == 3
    assert len(stored(repo)) == 6


def test_quota_lock_and_step_limits(repo, tmp_path):
    path = source(tmp_path, header(), event(), footer())
    collector = LowJournalCollector(repo, binding())
    assert collector.step([path], CollectionLimits(max_records=1)).code == "STEP_LIMIT"
    assert len(stored(repo)) == 1
    repo.max_raw_bytes = len(line(header()))
    assert collector.step([path]).code == "STORAGE_LIMIT"
    assert len(stored(repo)) == 1
    repo.max_raw_bytes = 10000
    lock = sqlite3.connect(tmp_path / "spool.db")
    lock.execute("BEGIN IMMEDIATE")
    try:
        assert collector.step([path]).code == "DB_ERROR"
        assert len(stored(repo)) == 1
        assert not repo.db.in_transaction
    finally:
        lock.rollback()
        lock.close()
    assert collector.step([path], CollectionLimits(max_scan_bytes=1)).code == "REPLAY_LIMIT"
    assert collector.step([path, path], CollectionLimits(max_files=1)).code == "FILE_LIMIT"
    assert collector.step([path], CollectionLimits(max_bytes=1)).code == "STEP_LIMIT"
    assert collector.step([path]).saved_records == 2


def test_secret_payload_safe_error(repo, tmp_path):
    secret = b"Authorization: hf_agent_PRIVATE_SECRET"
    path = source(
        tmp_path,
        header(),
        event(event="request", payload_b64=base64.b64encode(secret).decode()),
        b'{"secret":"hf_agent_PRIVATE_SECRET"}\n',
    )
    result = LowJournalCollector(repo, binding()).step([path])
    assert result.code == "INVALID_RECORD"
    assert "PRIVATE_SECRET" not in repr(result)
    assert len(stored(repo)) == 2
    assert secret not in stored(repo)[1][4]
    assert not repo.db.in_transaction


@pytest.mark.parametrize("point", ["after_insert", "after_checkpoint", "after_commit"])
@pytest.mark.parametrize("seed_header", [False, True])
def test_process_crash_recovery(tmp_path, point, seed_header):
    path = source(tmp_path, header(), event(), footer())
    db = tmp_path / "crash.db"
    if seed_header:
        initial = RawJournalRepository(db, AGENT)
        try:
            initial.append(binding(), read(path)[0][0])
        finally:
            initial.close()
    script = """
import os, sys
from pathlib import Path
from agent.telemetry.low_journal import JournalBinding, LowJournalReader
from agent.telemetry.low_journal.durable import RawJournalRepository
repo = RawJournalRepository(sys.argv[1], sys.argv[3])
binding = JournalBinding(sys.argv[4],sys.argv[5],1)
repo.db.create_function("crash",0,lambda: os._exit(73))
if sys.argv[6] != "after_commit":
    table = "low_raw_records" if sys.argv[6] == "after_insert" else "low_raw_generations"
    operation = "UPDATE" if sys.argv[7] == "1" and table == "low_raw_generations" else "INSERT"
    repo.db.execute(f"CREATE TRIGGER kill AFTER {operation} ON {table} BEGIN SELECT crash(); END")
with LowJournalReader().open(Path(sys.argv[2]),binding) as stream:
    record = next(stream)
    if sys.argv[7] == "1":
        record = next(stream)
    repo.append(binding,record,path=Path(sys.argv[2]))
os._exit(73)
"""
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(db),
            str(path),
            AGENT,
            binding().deployment_id,
            binding().instance_id,
            point,
            str(int(seed_header)),
        ],
        capture_output=True,
        timeout=20,
    )
    assert process.returncode == 73, process.stderr.decode()
    reopened = RawJournalRepository(db, AGENT)
    try:
        committed = point == "after_commit"
        count = int(seed_header) + int(committed)
        expected_raw = [line(header()), line(event())][:count]
        assert len(stored(reopened)) == count
        assert reopened.checkpoint(binding(), JOURNAL) == sum(map(len, expected_raw))
        reopened.db.execute("DROP TRIGGER IF EXISTS kill")
        result = LowJournalCollector(reopened, binding()).step([path])
        assert result.saved_records == 3 - count
        assert b"".join(row[4] for row in stored(reopened)) == path.read_bytes()
        assert reopened.checkpoint(binding(), JOURNAL) == path.stat().st_size
    finally:
        reopened.close()


def test_real_low_rotation_restart(tmp_path):
    sample = run_sample(tmp_path / "low", rotate=True)
    assert sample.return_code == 0
    expected = binding().__class__(sample.deployment_id, sample.instance_id, 1)
    db = tmp_path / "real.db"
    repository = RawJournalRepository(db, AGENT)
    try:
        collector = LowJournalCollector(repository, expected)
        result = collector.step(list(reversed(sample.files)), CollectionLimits(max_records=30))
        assert result.code == "STEP_LIMIT"
        assert len(stored(repository)) == 30
    finally:
        repository.close()
    repository = RawJournalRepository(db, AGENT)
    try:
        collector = LowJournalCollector(repository, expected)
        result = collector.step(list(reversed(sample.files)))
        assert result.state == "progress"
        assert collector.step(sample.files).saved_records == 0
        for path in sample.files:
            records, _ = read(path, expected)
            actual = repository.db.execute(
                "SELECT raw FROM low_raw_records WHERE journal_id=? ORDER BY offset",
                (records[0].journal_id,),
            ).fetchall()
            assert b"".join(row[0] for row in actual) == path.read_bytes()
            assert repository.checkpoint(expected, records[0].journal_id) == path.stat().st_size
    finally:
        repository.close()


def test_compatibility_preserves_nonempty_quarantine_and_snapshots(tmp_path):
    from agent.telemetry.schemas import RejectedEvent

    db = tmp_path / "compatibility.db"
    spool = SpoolRepository(db, AGENT)
    try:
        events = seed(spool, 3)
        batch = spool.next_batch()
        spool.acknowledge(
            batch,
            Ack(
                batch_id=batch.batch_id,
                accepted_ids=[events[0].event_id],
                rejected=[
                    RejectedEvent(
                        index=1, event_id=events[1].event_id, code="INVALID_EVENT", retryable=False
                    ),
                    RejectedEvent(
                        index=2,
                        event_id=events[2].event_id,
                        code="TEMPORARILY_UNAVAILABLE",
                        retryable=True,
                    ),
                ],
            ),
        )
        tables = ("metadata", "pending", "checkpoints", "quarantine", "batch_snapshots")
        before = {table: spool.db.execute(f"SELECT * FROM {table}").fetchall() for table in tables}
        assert all(before[table] for table in tables)
        raw = RawJournalRepository(db, AGENT)
        try:
            path = source(tmp_path, header(), event(), footer())
            assert LowJournalCollector(raw, binding()).step([path]).saved_records == 3
            assert {
                table: raw.db.execute(f"SELECT * FROM {table}").fetchall() for table in tables
            } == before
        finally:
            raw.close()
        assert spool.count() == 1
        assert spool.quarantine_count() == 1
        assert spool.list_quarantine()[0].event == events[1]
    finally:
        spool.close()


@pytest.mark.parametrize("interrupt", [False, True])
def test_resources_closed_on_failure_and_cancellation(repo, tmp_path, monkeypatch, interrupt):
    from agent.telemetry.low_journal.source import JournalSource

    path = source(tmp_path, header(), event(), footer())
    descriptors = []
    original_close = JournalSource.close

    def close(source):
        original_close(source)
        descriptors.append(source._fd)

    monkeypatch.setattr(JournalSource, "close", close)
    original_append = repo.append

    def fail(*args, **kwargs):
        if interrupt:
            raise KeyboardInterrupt
        raise RawJournalError("DB_ERROR")

    monkeypatch.setattr(repo, "append", fail)
    collector = LowJournalCollector(repo, binding())
    if interrupt:
        with pytest.raises(KeyboardInterrupt):
            collector.step([path])
    else:
        assert collector.step([path]).code == "DB_ERROR"
    assert descriptors == [None, None]
    assert not repo.db.in_transaction
    assert repo.checkpoint(binding(), JOURNAL) == 0
    monkeypatch.setattr(repo, "append", original_append)
    assert collector.step([path]).saved_records == 3
    assert descriptors == [None] * 4


def test_append_after_snapshot_seen_next_step(repo, tmp_path, monkeypatch):
    from agent.telemetry.low_journal.reader import JournalStream

    path = source(tmp_path, header())
    original_enter = JournalStream.__enter__
    calls = 0

    def enter(stream):
        nonlocal calls
        result = original_enter(stream)
        calls += 1
        if calls == 2:
            with path.open("ab") as file:
                file.write(line(event()))
        return result

    monkeypatch.setattr(JournalStream, "__enter__", enter)
    collector = LowJournalCollector(repo, binding())
    assert collector.step([path]).saved_records == 1
    assert stored(repo)[0][4] == line(header())
    assert collector.step([path]).saved_records == 1
    assert b"".join(row[4] for row in stored(repo)) == path.read_bytes()


def test_independent_source_continues_after_missing_file(repo, tmp_path):
    from agent.telemetry.low_journal import JournalBinding

    failed = LowJournalCollector(repo, binding()).step([tmp_path / "not-yet-created.jsonl"])
    assert failed.state == "waiting" and failed.code == "IO_ERROR"
    other = JournalBinding(binding().deployment_id, OTHER, 1)
    path = source(tmp_path, header(instance_id=OTHER), event(), footer())
    assert LowJournalCollector(repo, other).step([path]).saved_records == 3
    assert repo.checkpoint(other, JOURNAL) == path.stat().st_size
    assert repo.checkpoint(binding(), JOURNAL) == 0


def test_wrong_journal_sequence_is_corruption(repo, tmp_path):
    first, second = generations(tmp_path, seq=3)
    result = LowJournalCollector(repo, binding()).step([first, second])
    assert result.code == "CHAIN_SEQUENCE"
    assert stored(repo) == []


def test_actual_sink_connection_request_cross_files(repo, tmp_path):
    from test_low_journal_reader_contract import DEPLOYMENT, INSTANCE

    from honeypots.low.journal import JournalSettings, RotatingJournalSink

    directory = tmp_path / "sink"
    directory.mkdir()
    sink = RotatingJournalSink(
        JournalSettings(directory, INSTANCE, DEPLOYMENT, 1, max_file_bytes=262144)
    )
    sink.open()
    try:
        payload = base64.b64encode(b"x" * 90000).decode()
        for seq in (1, 2):
            row = event(seq=seq, event="request", payload_b64=payload)
            del row["record"]
            sink.emit(row)
        row = event(seq=3)
        del row["record"]
        sink.emit(row)
        row = event(seq=5, event="request", payload_b64=payload)
        del row["record"]
        sink.emit(row)
    finally:
        sink.close()
    paths = list(directory.glob("*.jsonl"))
    assert len(paths) == 2
    collector = LowJournalCollector(repo, binding())
    assert (
        collector.step(list(reversed(paths)), CollectionLimits(max_line_bytes=131072)).saved_records
        == 8
    )
    documents = [json.loads(row[4]) for row in stored(repo) if row[3] == "event"]
    assert [row["seq"] for row in documents] == [1, 2, 3, 5]
    assert documents[2]["connection_id"] == documents[3]["connection_id"]
    assert collector.step(paths, CollectionLimits(max_line_bytes=131072)).saved_records == 0


def test_budget_counts_header_read_ahead(repo, tmp_path, monkeypatch):
    from agent.telemetry.low_journal.source import JournalSource

    path = source(tmp_path, header(), event(), footer())
    original_read = JournalSource.read
    actual = 0

    def read(source, count):
        nonlocal actual
        chunk = original_read(source, count)
        actual += len(chunk)
        return chunk

    monkeypatch.setattr(JournalSource, "read", read)
    limits = CollectionLimits(max_line_bytes=512, max_scan_bytes=1500)
    result = LowJournalCollector(repo, binding()).step([path], limits)
    assert result.code == "REPLAY_LIMIT"
    assert actual <= limits.max_scan_bytes
    assert result.saved_records < 3


def test_reader_large_sequence_saved_without_integer_overflow(repo, tmp_path):
    large = 2**63
    path = source(
        tmp_path,
        header(journal_seq=large, first_seq=large),
        event(seq=large),
        footer(last_seq=large),
    )
    records, _ = read(path)
    for record in records:
        assert repo.append(binding(), record)
    assert b"".join(row[4] for row in stored(repo)) == path.read_bytes()
    assert repo.checkpoint(binding(), JOURNAL) == path.stat().st_size
    assert repo.db.execute(
        "SELECT journal_seq,typeof(journal_seq) FROM low_raw_generations"
    ).fetchone() == (str(large), "text")
    assert all(repo.append(binding(), record) is False for record in records)
