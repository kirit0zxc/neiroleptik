"""Настоящий sender и HTTP с долговечной тестовой дедупликацией до потери ACK."""

import json
import socket
import sqlite3
from contextlib import closing, contextmanager

from pydantic import SecretStr
from test_sender import AGENT, TOKEN, Clock, http_server, seed, sender_for

from agent.telemetry.schemas import Ack, Batch, RejectedEvent
from agent.telemetry.sender import HttpBatchTransport
from agent.telemetry.spool import SpoolRepository


class DurableReceiver:
    """Явно тестовый контракт; SQLite здесь не заменяет backend/PostgreSQL."""

    def __init__(self, path):
        self.path = path
        self.mode = "drop"
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE events (id TEXT PRIMARY KEY, payload BLOB NOT NULL)")
            db.execute("CREATE TABLE requests (payload BLOB NOT NULL)")

    def reply(self, handler, payload, attempt):
        batch = Batch.model_validate_json(payload)
        accepted = []
        rejected = []
        # Commit действительно завершается до разрыва сокета; соединение БД закрывается.
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO requests VALUES (?)", (payload,))
            for index, event in enumerate(batch.events):
                raw = event.canonical_json().encode()
                existing = db.execute(
                    "SELECT payload FROM events WHERE id=?", (str(event.event_id),)
                ).fetchone()
                assert existing is None or existing[0] == raw
                if self.mode == "mixed" and index == 1:
                    rejected.append(
                        RejectedEvent(
                            index=index,
                            event_id=event.event_id,
                            code="INVALID_EVENT",
                            retryable=False,
                        )
                    )
                elif self.mode == "mixed" and index == 2:
                    rejected.append(
                        RejectedEvent(
                            index=index,
                            event_id=event.event_id,
                            code="TEMPORARILY_UNAVAILABLE",
                            retryable=True,
                        )
                    )
                else:
                    db.execute(
                        "INSERT OR IGNORE INTO events VALUES (?,?)", (str(event.event_id), raw)
                    )
                    accepted.append(event.event_id)
        if self.mode == "drop":
            handler.connection.shutdown(socket.SHUT_RDWR)
            handler.connection.close()
            return
        ack = Ack(batch_id=batch.batch_id, accepted_ids=accepted, rejected=rejected)
        raw = (
            ack.model_dump_json().encode()
            if self.mode != "invalid"
            else b'{"batch_id":"secret-invalid"}'
        )
        handler.send_response(200)
        handler.send_header("Content-Length", str(len(raw)))
        handler.end_headers()
        handler.wfile.write(raw)

    def rows(self, table):
        with closing(sqlite3.connect(self.path)) as db, db:
            return db.execute(f"SELECT * FROM {table}").fetchall()


@contextmanager
def queue(path):
    spool = SpoolRepository(path, AGENT)
    try:
        yield spool
    finally:
        spool.close()


def test_durable_receiver_lost_ack_restart_invalid_and_new_events(tmp_path):
    receiver = DurableReceiver(tmp_path / "receiver.db")
    path = tmp_path / "spool.db"
    with http_server(receiver.reply) as (endpoint, requests):
        transport = HttpBatchTransport(endpoint, SecretStr(TOKEN), timeout=2)
        with queue(path) as spool:
            original = seed(spool, 2)
            clock = Clock()
            sender = sender_for(spool, transport, clock)
            assert sender.step().error_code == "TRANSPORT_ERROR"
            assert spool.count() == 2
            assert len(receiver.rows("events")) == 2
            assert len(receiver.rows("requests")) == 1
            assert sender.step().state == "waiting"
            assert len(receiver.rows("requests")) == 1
            clock.value = 1
            assert sender.step().state == "retry"
            assert receiver.rows("requests")[0] == receiver.rows("requests")[1]
            assert len(receiver.rows("events")) == 2
        # Рестарт sender/spool: новый batch_id допустим, canonical Event обязан совпасть.
        with queue(path) as spool:
            assert spool.count() == 2
            receiver.mode = "invalid"
            clock = Clock()
            sender = sender_for(spool, transport, clock)
            assert sender.step().error_code == "INVALID_ACK"
            assert spool.count() == 2
            old = Batch.model_validate_json(receiver.rows("requests")[0][0])
            new = Batch.model_validate_json(receiver.rows("requests")[-1][0])
            assert old.events == new.events == original
            receiver.mode = "accepted"
            clock.value = 1
            assert sender.step().accepted == 2
            assert spool.count() == 0
            assert len(receiver.rows("events")) == 2
            newer = seed(spool)
            assert sender.step().accepted == 1
            assert len(receiver.rows("events")) == 3
            expected = {
                str(event.event_id): event.canonical_json().encode() for event in original + newer
            }
            assert dict(receiver.rows("events")) == expected
            assert spool.quarantine_count() == 0
            assert spool.checkpoint("test-source") == 3
        assert all(request[0] == "/agent/v1/events" for request in requests)


def test_http_unavailable_backoff_then_mixed_quarantine(tmp_path):
    receiver = DurableReceiver(tmp_path / "receiver.db")
    receiver.mode = "mixed"

    def unavailable_then_receive(handler, payload, attempt):
        if attempt <= 2:
            handler.send_response(503)
            handler.send_header("Content-Length", "0")
            handler.end_headers()
        else:
            receiver.reply(handler, payload, attempt)

    with (
        http_server(unavailable_then_receive) as (endpoint, requests),
        queue(tmp_path / "spool.db") as spool,
    ):
        events = seed(spool, 3)
        clock = Clock()
        sender = sender_for(spool, HttpBatchTransport(endpoint, SecretStr(TOKEN), 2), clock)
        assert sender.step().retry_in == 1
        assert spool.count() == 3
        assert sender.step().state == "waiting"
        clock.value = 1
        assert sender.step().retry_in == 2
        assert spool.count() == 3
        clock.value = 3
        result = sender.step()
        assert (result.accepted, result.quarantined) == (1, 1)
        assert spool.count() == 1
        assert spool.list_quarantine()[0].event == events[1]
        assert len(receiver.rows("events")) == 1
        assert [
            json.loads(row["payload"])["event_id"]
            for row in [
                {"payload": value[0]} for value in spool.db.execute("SELECT payload FROM pending")
            ]
        ] == [str(events[2].event_id)]
        receiver.mode = "accepted"
        clock.value += result.retry_in
        assert sender.step().accepted == 1
        assert spool.count() == 0
        assert spool.quarantine_count() == 1
        assert len(receiver.rows("events")) == 2
        assert requests[0][2] == requests[1][2] == requests[2][2]
        assert Batch.model_validate_json(requests[3][2]).events == [events[2]]


def test_connection_refused_retains_pending(tmp_path):
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        endpoint = f"http://127.0.0.1:{reserved.getsockname()[1]}/test-events"
        with queue(tmp_path / "spool.db") as spool:
            seed(spool)
            sender = sender_for(spool, HttpBatchTransport(endpoint, SecretStr(TOKEN), 0.2), Clock())
            assert sender.step().error_code == "TRANSPORT_ERROR"
            assert sender.step().state == "waiting"
            assert spool.count() == 1
            assert spool.checkpoint("test-source") == 1
