"""CLI продолжает конверсию проверенного raw, даже если новая цепочка ждёт границу."""

import json
from contextlib import closing

from test_low_journal_reader_contract import OTHER, binding, event, footer, header, line
from test_low_production_mapper import AGENT

from agent.telemetry.collect_low import main
from agent.telemetry.low_journal.durable import RawJournalRepository
from agent.telemetry.low_journal.reader import LowJournalReader
from agent.telemetry.spool import SpoolRepository


def test_waiting_chain_still_converts_saved_records(tmp_path, capsys):
    path = tmp_path / "old.jsonl"
    path.write_bytes(line(header()) + line(event()) + line(footer()))
    db = tmp_path / "spool.db"
    with closing(RawJournalRepository(db, AGENT)) as raw:
        with LowJournalReader().open(path, binding()) as stream:
            for record in stream:
                raw.append(binding(), record)
    late = tmp_path / "late.jsonl"
    late.write_bytes(line(header(journal_id=OTHER, journal_seq=2)))
    bound = binding()
    result = main(
        [
            "--spool",
            str(db),
            "--agent-id",
            AGENT,
            "--deployment-id",
            bound.deployment_id,
            "--instance-id",
            bound.instance_id,
            "--profile-revision",
            "1",
            str(path),
            str(late),
        ]
    )
    assert result == 1
    diagnostic = json.loads(capsys.readouterr().out)
    assert diagnostic["collection"]["state"] == "waiting"
    assert diagnostic["collection"]["code"] in {"MISSING_PREDECESSOR", "OPEN_PREDECESSOR"}
    assert diagnostic["conversion"]["records"] == 3
    with closing(SpoolRepository(db, AGENT)) as spool:
        assert spool.db.execute("SELECT count(*) FROM pending").fetchone()[0] == 1
    assert path.exists() and late.exists()
