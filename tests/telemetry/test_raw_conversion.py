"""C01–C11: синтетический mapper, реальные SQLite-транзакции и crash процесса."""

import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier
from uuid import UUID, uuid5

import pytest
from test_low_journal_reader_contract import (
    JOURNAL,
    OTHER,
    binding,
    event,
    footer,
    header,
    read,
    source,
)
from test_sender import AGENT, seed

from agent.telemetry.low_journal.conversion import (
    ConversionError,
    ConversionLimits,
    LowRawConverter,
)
from agent.telemetry.low_journal.durable import RawJournalRepository
from agent.telemetry.schemas import Ack, Event, RejectedEvent
from agent.telemetry.spool import SpoolRepository


class SyntheticMapper:
    """Только тестовый Event: не утверждает Low identity, session или peer mapping."""

    def convert(self, bound, record):
        if record.kind != "event":
            return None
        return Event.model_validate(
            {
                "schema_version": 1,
                "event_id": str(
                    uuid5(
                        UUID(AGENT),
                        ":".join(
                            (
                                bound.deployment_id,
                                bound.instance_id,
                                record.journal_id,
                                str(record.offset),
                            )
                        ),
                    )
                ),
                "agent_id": AGENT,
                "deployment_id": bound.deployment_id,
                "instance_id": bound.instance_id,
                "profile_revision": bound.profile_revision,
                "source_seq": record.offset,
                "occurred_at": "2026-10-11T00:00:00Z",
                "type": "runtime.state",
                "source": {"ip": "127.0.0.1", "port": 0},
                "service": {"protocol": "http", "port": 8080},
                "data": {"synthetic": True},
                "artifact_ids": [],
            }
        )


@pytest.fixture
def conversion(tmp_path):
    path = tmp_path / "spool.db"
    records, _ = read(source(tmp_path, header(), event(), footer()))
    raw = RawJournalRepository(path, AGENT)
    for record in records:
        raw.append(binding(), record)
    spool = SpoolRepository(path, AGENT)
    try:
        yield path, raw, spool, records, LowRawConverter(spool, SyntheticMapper(), "synthetic/1")
    finally:
        raw.close()
        spool.close()


def ledger(spool):
    return spool.db.execute("SELECT * FROM low_conversion_ledger ORDER BY offset").fetchall()


def test_atomic_service_event_and_unchanged_raw(conversion):
    _, raw, spool, records, converter = conversion
    before = raw.db.execute("SELECT * FROM low_raw_records").fetchall()
    position = raw.checkpoint(binding(), JOURNAL)
    result = converter.step(binding())
    assert (result.state, result.records, result.raw_bytes) == (
        "progress",
        3,
        sum(len(r.raw) for r in records),
    )
    assert spool.count() == 1
    assert [row[7] for row in ledger(spool)] == ["service", "event", "service"]
    assert converter.step(binding()).state == "idle"
    assert converter.convert(binding(), records[0]) == "repeated"
    assert raw.db.execute("SELECT * FROM low_raw_records").fetchall() == before
    assert raw.checkpoint(binding(), JOURNAL) == position
    assert spool.checkpoint("test-source") == 0


@pytest.mark.parametrize("mode", ["accepted", "permanent", "retryable"])
def test_repeat_after_ack_and_restart(conversion, mode):
    path, _, spool, records, converter = conversion
    converter.convert(binding(), records[1])
    batch = spool.next_batch()
    event_id = batch.events[0].event_id
    ack = Ack(
        batch_id=batch.batch_id,
        accepted_ids=[event_id] if mode == "accepted" else [],
        rejected=[]
        if mode == "accepted"
        else [
            RejectedEvent(
                index=0,
                event_id=event_id,
                code="TEMPORARILY_UNAVAILABLE" if mode == "retryable" else "EVENT_ID_CONFLICT",
                retryable=mode == "retryable",
            )
        ],
    )
    before = ledger(spool)
    assert converter.convert(binding(), records[1]) == "repeated"
    spool.acknowledge(batch, ack)
    reopened = SpoolRepository(path, AGENT)
    try:
        assert (
            LowRawConverter(reopened, SyntheticMapper(), "synthetic/1").convert(
                binding(), records[1]
            )
            == "repeated"
        )
        assert reopened.count() == (mode == "retryable")
        assert reopened.quarantine_count() == (mode == "permanent")
        assert ledger(reopened) == before
    finally:
        reopened.close()


@pytest.mark.parametrize("mode", ["raises", "none", "invalid", "foreign", "service_event"])
def test_mapper_failure_has_no_evidence(conversion, mode):
    _, _, spool, records, _ = conversion

    class BadMapper(SyntheticMapper):
        def convert(self, bound, record):
            mapped = super().convert(bound, records[1])
            if mode == "raises":
                raise ValueError("private-password")
            if mode == "none":
                return None
            if mode == "invalid":
                mapped.data["bad"] = float("nan")
            if mode == "foreign":
                return mapped.model_copy(update={"instance_id": UUID(int=999)})
            return mapped

    chosen = records[0] if mode == "service_event" else records[1]
    with pytest.raises(ConversionError) as error:
        LowRawConverter(spool, BadMapper(), "test").convert(binding(), chosen)
    assert "private" not in str(error.value)
    assert spool.count() == 0 and ledger(spool) == []
    assert not spool.db.in_transaction


@pytest.mark.parametrize("table", ["pending", "low_conversion_ledger"])
def test_real_insert_failure_rolls_back_pair(conversion, table):
    _, _, spool, records, converter = conversion
    converter.convert(binding(), records[0])
    before = ledger(spool)
    spool.db.execute(
        f"CREATE TRIGGER fail AFTER INSERT ON {table} BEGIN SELECT RAISE(ABORT,'secret-sql'); END"
    )
    with pytest.raises(ConversionError, match="^DB_ERROR$"):
        converter.convert(binding(), records[1])
    assert spool.count() == 0 and ledger(spool) == before
    assert not spool.db.in_transaction
    spool.db.execute("DROP TRIGGER fail")
    assert converter.convert(binding(), records[1]) == "created"


def test_real_commit_denied_rolls_back(conversion):
    _, _, spool, records, converter = conversion

    def deny_commit(action, arg1, arg2, database, trigger):
        if action == sqlite3.SQLITE_TRANSACTION and arg1 == "COMMIT":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    spool.db.set_authorizer(deny_commit)
    with pytest.raises(ConversionError, match="^DB_ERROR$"):
        converter.convert(binding(), records[1])
    spool.db.set_authorizer(None)
    assert spool.count() == 0 and ledger(spool) == []
    assert not spool.db.in_transaction
    assert converter.convert(binding(), records[1]) == "created"


@pytest.mark.parametrize("stage", ["pending_inserted", "ledger_inserted", "before_commit"])
def test_fault_boundaries_roll_back(conversion, stage):
    _, _, spool, records, _ = conversion

    class Fault(LowRawConverter):
        def _checkpoint(self, point):
            if point == stage:
                raise sqlite3.OperationalError("private-sql")

    with pytest.raises(ConversionError, match="^DB_ERROR$"):
        Fault(spool, SyntheticMapper(), "synthetic/1").convert(binding(), records[1])
    assert spool.count() == 0 and ledger(spool) == []


@pytest.mark.parametrize(
    "stage", ["pending_inserted", "ledger_inserted", "before_commit", "after_commit"]
)
def test_process_crash_before_and_after_commit(conversion, stage):
    path, _, spool, records, converter = conversion
    script = """
import os, sys
from test_raw_conversion import SyntheticMapper, binding, AGENT
from agent.telemetry.spool import SpoolRepository
from agent.telemetry.low_journal.conversion import LowRawConverter
from agent.telemetry.low_journal.models import JournalRecord
spool = SpoolRepository(sys.argv[1], AGENT)
row = spool.db.execute("SELECT kind,journal_id,offset,next_offset,raw FROM low_raw_records "
                       "WHERE kind='event'").fetchone()
class Crash(LowRawConverter):
    def _checkpoint(self, point):
        if point == sys.argv[2]: os._exit(77)
Crash(spool, SyntheticMapper(), 'synthetic/1').convert(binding(), JournalRecord(*row))
"""
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(Path.cwd()), str(Path(__file__).parent)]),
    }
    result = subprocess.run(
        [sys.executable, "-c", script, str(path), stage], env=env, capture_output=True, timeout=15
    )
    assert result.returncode == 77, result.stderr.decode()
    committed = stage == "after_commit"
    assert (spool.count(), len(ledger(spool))) == (int(committed), int(committed))
    assert converter.convert(binding(), records[1]) == ("repeated" if committed else "created")
    assert spool.count() == len(ledger(spool)) == 1


def test_raw_result_version_and_id_conflicts(conversion):
    _, _, spool, records, converter = conversion
    converter.convert(binding(), records[1])
    before = ledger(spool)
    with pytest.raises(ConversionError, match="RAW_CONFLICT"):
        converter.convert(binding(), replace(records[1], raw=records[1].raw + b" "))
    with pytest.raises(ConversionError, match="RAW_NOT_SAVED"):
        converter.convert(binding(), replace(records[1], offset=9999))
    with pytest.raises(ConversionError, match="RESULT_CONFLICT"):
        LowRawConverter(spool, SyntheticMapper(), "synthetic/2").convert(binding(), records[1])

    class Changed(SyntheticMapper):
        def convert(self, bound, record):
            value = super().convert(bound, record)
            value.data["changed"] = True
            return value

    with pytest.raises(ConversionError, match="RESULT_CONFLICT"):
        LowRawConverter(spool, Changed(), "synthetic/1").convert(binding(), records[1])
    assert ledger(spool) == before and spool.count() == 1


def test_two_connections_same_position(conversion):
    path, _, spool, records, _ = conversion
    barrier = Barrier(2)

    def run(_):
        local = SpoolRepository(path, AGENT)
        try:
            barrier.wait(5)
            return LowRawConverter(local, SyntheticMapper(), "synthetic/1").convert(
                binding(), records[1]
            )
        finally:
            local.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(run, range(2))) == ["created", "repeated"]
    assert spool.count() == len(ledger(spool)) == 1


def test_step_limits_lock_and_progress(conversion):
    path, raw, spool, records, converter = conversion
    assert converter.step(binding(), ConversionLimits(max_bytes=1)).state == "limited"
    assert ledger(spool) == []
    first = converter.step(binding(), ConversionLimits(max_records=1))
    assert first.state == "limited" and first.records == 1
    spool.db.execute("PRAGMA busy_timeout=1")
    with raw.db:
        raw.db.execute("BEGIN IMMEDIATE")
        result = converter.step(binding())
    assert result.state == "retry" and result.code == "WRITER_BUSY"
    assert len(ledger(spool)) == 1
    spool.db.execute(
        "CREATE TRIGGER fail AFTER INSERT ON pending BEGIN SELECT RAISE(ABORT,'private'); END"
    )
    assert converter.step(binding()).state == "error"
    assert len(ledger(spool)) == 1
    spool.db.execute("DROP TRIGGER fail")
    assert converter.step(binding()).records == 2


def test_sqlite_full_no_false_ledger(conversion):
    _, raw, spool, records, _ = conversion

    class Large(SyntheticMapper):
        def convert(self, bound, record):
            value = super().convert(bound, record)
            value.data["blob"] = "x" * 7900
            return value

    pages = spool.db.execute("PRAGMA page_count").fetchone()[0]
    spool.db.execute(f"PRAGMA max_page_count={pages}")
    before = raw.checkpoint(binding(), JOURNAL)
    with pytest.raises(ConversionError, match="^DB_ERROR$"):
        LowRawConverter(spool, Large(), "large").convert(binding(), records[1])
    assert spool.count() == 0 and ledger(spool) == []
    assert raw.checkpoint(binding(), JOURNAL) == before
    spool.db.execute("PRAGMA max_page_count=10000")
    assert LowRawConverter(spool, Large(), "large").convert(binding(), records[1]) == "created"


@pytest.mark.parametrize("acknowledged", [False, True])
def test_existing_spool_snapshots_and_event_id_conflict(conversion, acknowledged):
    _, _, spool, records, converter = conversion
    original = seed(spool)[0]
    batch = spool.next_batch()
    if acknowledged:
        spool.acknowledge(
            batch, Ack(batch_id=batch.batch_id, accepted_ids=[original.event_id], rejected=[])
        )
    before = {
        name: spool.db.execute(f"SELECT * FROM {name}").fetchall()
        for name in ("pending", "quarantine", "batch_snapshots", "checkpoints")
    }

    class Reused(SyntheticMapper):
        def convert(self, bound, record):
            return super().convert(bound, record).model_copy(update={"event_id": original.event_id})

    with pytest.raises(ConversionError, match="EVENT_ID_CONFLICT"):
        LowRawConverter(spool, Reused(), "test").convert(binding(), records[1])
    assert ledger(spool) == []
    for name, rows in before.items():
        assert spool.db.execute(f"SELECT * FROM {name}").fetchall() == rows
    assert converter.convert(binding(), records[1]) == "created"


@pytest.mark.parametrize("limits", [{"max_records": True}, {"max_bytes": 0}])
def test_invalid_limits(limits):
    with pytest.raises(ValueError):
        ConversionLimits(**limits)


def test_new_process_same_instance_repeated_seq_separate_saved_positions(conversion, tmp_path):
    """Конверсия raw независима от ещё не согласованного перехода collector через разрыв."""
    _, raw, spool, first, converter = conversion
    converter.step(binding())
    prior_checkpoint = raw.checkpoint(binding(), JOURNAL)
    second, _ = read(
        source(tmp_path, header(journal_id=OTHER, journal_seq=2), event(), footer(journal_id=OTHER))
    )
    # Raw repository принимает каждый journal отдельно; collector не ослабляем.
    for record in second:
        raw.append(binding(), record)
    assert second[1].offset == first[1].offset
    assert second[1].document()["seq"] == first[1].document()["seq"] == 1
    assert converter.step(binding()).records == 3
    batch = spool.next_batch()
    assert len({e.event_id for e in batch.events}) == 2
    assert raw.checkpoint(binding(), JOURNAL) == prior_checkpoint
    assert raw.checkpoint(binding(), OTHER) == second[-1].next_offset
    assert converter.convert(binding(), first[1]) == "repeated"


def test_id_reuse_from_another_position_after_ack_preserves_ledger(conversion, tmp_path):
    _, raw, spool, records, converter = conversion
    converter.convert(binding(), records[1])
    batch = spool.next_batch()
    old_event_id = batch.events[0].event_id
    spool.acknowledge(batch, Ack(batch_id=batch.batch_id, accepted_ids=[old_event_id], rejected=[]))
    second, _ = read(
        source(tmp_path, header(journal_id=OTHER, journal_seq=2), event(), footer(journal_id=OTHER))
    )
    for record in second:
        raw.append(binding(), record)
    before = ledger(spool)

    class Reused(SyntheticMapper):
        def convert(self, bound, record):
            return super().convert(bound, record).model_copy(update={"event_id": old_event_id})

    with pytest.raises(ConversionError, match="EVENT_ID_CONFLICT"):
        LowRawConverter(spool, Reused(), "synthetic/1").convert(binding(), second[1])
    assert spool.count() == 0 and ledger(spool) == before


def test_upgrade_nonempty_legacy_spool_keeps_snapshots_quarantine_and_checkpoints(conversion):
    """C11: открывается прежняя схема без ledger с уже применённым частичным ACK."""
    path, _, spool, records, _ = conversion
    seed(spool, 2)
    batch = spool.next_batch()
    spool.acknowledge(
        batch,
        Ack(
            batch_id=batch.batch_id,
            accepted_ids=[batch.events[0].event_id],
            rejected=[
                RejectedEvent(
                    index=1,
                    event_id=batch.events[1].event_id,
                    code="EVENT_ID_CONFLICT",
                    retryable=False,
                )
            ],
        ),
    )
    seed(spool)
    spool.next_batch()
    spool.db.execute("DROP TABLE low_conversion_ledger")
    names = (
        "pending",
        "quarantine",
        "batch_snapshots",
        "checkpoints",
        "metadata",
        "low_raw_records",
        "low_raw_generations",
    )
    before = {name: spool.db.execute(f"SELECT * FROM {name}").fetchall() for name in names}
    spool.close()
    upgraded = SpoolRepository(path, AGENT)
    try:
        for name, rows in before.items():
            assert upgraded.db.execute(f"SELECT * FROM {name}").fetchall() == rows
        assert ledger(upgraded) == []
        converter = LowRawConverter(upgraded, SyntheticMapper(), "synthetic/1")
        assert converter.convert(binding(), records[1]) == "created"
        assert upgraded.count() == 2 and upgraded.quarantine_count() == 1
        assert upgraded.checkpoint("test-source") == 3
    finally:
        upgraded.close()
