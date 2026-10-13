"""Авария применения ACK не стирает ledger и не мешает следующей позиции.

Mapper и ACK синтетические: проверяется локальная SQLite, а не Low→UI.
"""

import os
import subprocess
import sys
from contextlib import ExitStack, closing
from pathlib import Path

import pytest
from test_low_journal_reader_contract import binding, event, footer, header, read, source
from test_raw_conversion import SyntheticMapper, ledger
from test_sender import AGENT

from agent.telemetry.low_journal.conversion import ConversionError, LowRawConverter
from agent.telemetry.low_journal.durable import RawJournalRepository
from agent.telemetry.schemas import Ack
from agent.telemetry.spool import SpoolRepository


@pytest.mark.parametrize("stage", ["after_delete", "after_ack_commit"])
def test_ack_process_crash_replay_and_next_position(tmp_path, stage):
    path = tmp_path / "spool.db"
    records, _ = read(source(tmp_path, header(), event(), event(seq=2), footer(last_seq=2)))
    raw = RawJournalRepository(path, AGENT)
    try:
        for record in records:
            raw.append(binding(), record)
        raw_before = raw.db.execute("SELECT * FROM low_raw_records ORDER BY offset").fetchall()
        checkpoint = raw.checkpoint(binding(), records[0].journal_id)
    finally:
        raw.close()
    spool = SpoolRepository(path, AGENT)
    try:
        LowRawConverter(spool, SyntheticMapper(), "synthetic/1").convert(binding(), records[1])
        batch = spool.next_batch()
        assert batch is not None
        evidence = ledger(spool)
        snapshot = batch.to_bytes()
    finally:
        spool.close()

    script = """
import os, sys
from test_sender import AGENT
from agent.telemetry.schemas import Ack, Batch
from agent.telemetry.spool import SpoolRepository
spool = SpoolRepository(sys.argv[1], AGENT)
saved = spool.db.execute("SELECT payload FROM batch_snapshots WHERE batch_id=?",
                         (sys.argv[3],)).fetchone()[0]
batch = Batch.model_validate_json(saved)
if sys.argv[2] == "after_delete":
    spool.db.create_function("crash", 0, lambda: os._exit(78))
    spool.db.execute("CREATE TEMP TRIGGER kill AFTER DELETE ON pending "
                     "BEGIN SELECT crash(); END")
spool.acknowledge(batch, Ack(batch_id=batch.batch_id,
                           accepted_ids=[batch.events[0].event_id], rejected=[]))
assert not spool.db.in_transaction
os._exit(78)
"""
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [str(Path(__file__).resolve().parents[2]), str(Path(__file__).resolve().parent)]
        ),
    }
    child = subprocess.run(
        [sys.executable, "-c", script, str(path), stage, str(batch.batch_id)],
        env=env,
        capture_output=True,
        timeout=15,
    )
    assert child.returncode == 78, child.stderr.decode(errors="replace")

    with ExitStack() as resources:
        spool = resources.enter_context(closing(SpoolRepository(path, AGENT)))
        raw = resources.enter_context(closing(RawJournalRepository(path, AGENT)))
        assert spool.db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert spool.count() == int(stage == "after_delete")
        assert ledger(spool) == evidence
        assert spool.db.execute(
            "SELECT payload FROM batch_snapshots WHERE batch_id=?", (str(batch.batch_id),)
        ).fetchone() == (snapshot,)
        assert (
            raw.db.execute("SELECT * FROM low_raw_records ORDER BY offset").fetchall() == raw_before
        )
        assert raw.checkpoint(binding(), records[0].journal_id) == checkpoint

        converter = LowRawConverter(spool, SyntheticMapper(), "synthetic/1")
        assert converter.convert(binding(), records[1]) == "repeated"
        spool.acknowledge(
            batch,
            Ack(batch_id=batch.batch_id, accepted_ids=[batch.events[0].event_id], rejected=[]),
        )
        assert spool.count() == 0
        assert converter.convert(binding(), records[1]) == "repeated"
        with pytest.raises(ConversionError, match="^RESULT_CONFLICT$"):
            LowRawConverter(spool, SyntheticMapper(), "synthetic/2").convert(binding(), records[1])
        assert ledger(spool) == evidence
        assert spool.count() == 0
        assert converter.convert(binding(), records[2]) == "created"
        following = spool.next_batch()
        assert following is not None
        assert len(following.events) == 1
        assert following.events[0].event_id != batch.events[0].event_id
        assert following.events[0].source_seq == records[2].offset
        assert ledger(spool)[0] == evidence[0]
