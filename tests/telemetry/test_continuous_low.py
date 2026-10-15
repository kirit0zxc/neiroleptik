"""Постоянный worker: настоящая SQLite, дописывание журнала и потерянный ACK."""

import json
from contextlib import closing

from test_low_journal_reader_contract import OTHER, binding, event, footer, header, line
from test_low_production_mapper import AGENT

from agent.runtime.executor.telemetry import LowTelemetryWorker
from agent.telemetry.sender import HttpResponse, TransportError


class Center:
    """Центр запоминает ID, но умеет потерять первый ответ после commit."""

    def __init__(self, *, lose_ack=False, status=200):
        self.ids = set()
        self.batches = []
        self.lose_ack = lose_ack
        self.status = status

    def post(self, payload):
        batch = json.loads(payload)
        self.batches.append(batch)
        ids = [row["event_id"] for row in batch["events"]]
        self.ids.update(ids)
        if self.lose_ack:
            self.lose_ack = False
            raise TransportError()
        return HttpResponse(
            self.status,
            json.dumps(
                {
                    "batch_id": batch["batch_id"],
                    "accepted_ids": ids,
                    "rejected": [],
                }
            ).encode(),
        )


def journal(tmp_path):
    bound = binding()
    directory = tmp_path / bound.deployment_id
    directory.mkdir()
    path = directory / f"low-{bound.instance_id}-test.jsonl"
    path.write_bytes(line(header()) + line(event()))
    return path


def test_append_stop_restart_and_no_implicit_binding(tmp_path):
    path = journal(tmp_path)
    center = Center()
    db = tmp_path / "spool.db"
    with closing(LowTelemetryWorker(db, tmp_path, AGENT, center)) as worker:
        assert worker.step(()) == ("idle", "idle")
        assert not center.ids
        assert worker.step((binding(),))[1] == "delivered:1"
        with path.open("ab") as stream:
            stream.write(line(event(seq=2)) + line(footer(last_seq=2)))
        assert worker.step((binding(),))[1] == "delivered:1"
    with closing(LowTelemetryWorker(db, tmp_path, AGENT, center)) as worker:
        assert worker.step((binding(),))[1] == "idle"
    assert len(center.ids) == 2
    assert len(center.batches) == 2
    assert path.exists()


def test_lost_ack_survives_worker_restart(tmp_path):
    journal(tmp_path)
    center = Center(lose_ack=True)
    db = tmp_path / "spool.db"
    with closing(LowTelemetryWorker(db, tmp_path, AGENT, center)) as worker:
        assert worker.step((binding(),))[1] == "TRANSPORT_ERROR"
        assert worker.step((binding(),))[1] == "waiting"
        assert len(center.batches) == 1
    with closing(LowTelemetryWorker(db, tmp_path, AGENT, center)) as worker:
        assert worker.step((binding(),))[1] == "delivered:1"
    # После рестарта sender вправе собрать новый batch; Event ID и содержимое неизменны.
    assert center.batches[0]["events"] == center.batches[1]["events"]
    assert len(center.ids) == 1


def test_auth_pause_keeps_collecting_and_does_not_retry(tmp_path):
    path = journal(tmp_path)
    center = Center(status=401)
    with closing(LowTelemetryWorker(tmp_path / "spool.db", tmp_path, AGENT, center)) as worker:
        assert worker.step((binding(),))[1] == "AUTH_REQUIRED"
        with path.open("ab") as stream:
            stream.write(line(event(seq=2)))
        assert worker.step((binding(),))[1] == "AUTH_REQUIRED"
        assert len(center.batches) == 1
        assert worker._spool.db.execute("SELECT count(*) FROM pending").fetchone()[0] == 2


def test_untrusted_new_root_does_not_block_saved_delivery(tmp_path):
    path = journal(tmp_path)
    center = Center(lose_ack=True)
    db = tmp_path / "spool.db"
    with closing(LowTelemetryWorker(db, tmp_path, AGENT, center)) as worker:
        worker.step((binding(),))
    path.with_name(f"low-{binding().instance_id}-late.jsonl").write_bytes(
        line(header(journal_id=OTHER, journal_seq=2))
    )
    with closing(LowTelemetryWorker(db, tmp_path, AGENT, center)) as worker:
        collected, sent = worker.step((binding(),))
        assert collected in {"MISSING_PREDECESSOR", "OPEN_PREDECESSOR"}
        assert sent == "delivered:1"
    assert len(center.ids) == 1
