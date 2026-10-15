"""Изолированный стенд worker: SQL-оракул, долговечный HTTP receiver и IPC-процессы."""

import base64
import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
from contextlib import closing, contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

from pydantic import SecretStr
from test_low_journal_reader_contract import binding, event, header, line
from test_low_production_mapper import AGENT
from test_sender import TOKEN, Clock

from agent.runtime.executor.telemetry import LowTelemetryWorker
from agent.telemetry.schemas import Batch
from agent.telemetry.sender import HttpBatchTransport

TABLES = (
    "low_raw_records",
    "low_raw_generations",
    "low_conversion_ledger",
    "pending",
    "quarantine",
    "batch_snapshots",
    "low_boundary_bindings",
    "low_boundary_writers",
    "low_boundary_proofs",
    "low_boundary_journals",
    "low_boundary_pins",
    "low_boundary_tails",
    "low_storage_usage",
    "low_raw_usage",
    "low_raw_locations",
    "checkpoints",
)


def snapshot(path):
    """Только read-only connection: constructor repository мог бы мигрировать БД."""
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        return {
            table: db.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in TABLES
        }


def journal(root, bound=None, rows=None):
    bound = bound or binding()
    directory = root / bound.deployment_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"low-{bound.instance_id}-test.jsonl"
    path.write_bytes(
        b"".join(
            line(row) if isinstance(row, dict) else row for row in (rows or [header(), event()])
        )
    )
    return path


def append(path, *rows):
    with path.open("ab") as stream:
        for row in rows:
            stream.write(line(row) if isinstance(row, dict) else row)


def command(store, *, action="start", generation=1, instance=None):
    """Настоящий store получает метаданные команды; файл не создаёт доверие."""
    metadata = {
        "command_id": str(uuid4()),
        "deployment_id": binding().deployment_id,
        "action": action,
        "generation": generation,
        "expected_generation": generation - 1,
        "revision": 1,
        "expected_instance_id": instance,
        "reconcile": False,
    }
    return store.accept(metadata, "a" * 64, "lease-test")


def complete(store, record, *, failed=False):
    return store.complete(
        record.command_id,
        {
            "deployment_id": record.deployment_id,
            "generation": record.generation,
            "instance_id": None
            if failed
            else (record.attempt_instance_id or record.expected_instance_id),
            "applied_revision": None if failed else 1,
            "outcome": "failed" if failed else "succeeded",
            "runtime_state": "failed"
            if failed
            else ("running" if record.action == "start" else "stopped"),
            "error": {"code": "LAUNCH_FAILED", "message": "Запуск не подтверждён"}
            if failed
            else None,
        },
    )


def expected_events(path, bound=None):
    """Независимый UUID/DTO-оракул по сохранённым входным bytes, без вызова mapper."""
    bound = bound or binding()
    rows = path.read_bytes().splitlines(keepends=True)
    journal_id = json.loads(rows[0])["journal_id"]
    result = {}
    offset = 0

    def encode(value):
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )

    for raw in rows:
        row = json.loads(raw)
        if row["record"] == "event":
            scope = [AGENT, bound.deployment_id, bound.instance_id]
            event_id = str(uuid5(NAMESPACE_URL, encode([*scope, journal_id, offset])))
            peer = row["peer"]
            data = {"low_seq": str(row["seq"])}
            kind = "connection.opened"
            if row["event"] == "request":
                payload = base64.b64decode(row["payload_b64"])
                kind = row["protocol"] + ".request"
                data.update(
                    payload_b64=row["payload_b64"],
                    captured_bytes=len(payload),
                    mapper_truncated=False,
                    capture_completeness="unknown",
                )
                if row["protocol"] == "http":
                    # Фикстуры этого стенда имеют одну заранее известную корректную строку.
                    data.update(
                        method="GET", http_version="1.1", path="[скрыто]", http_parse_state="parsed"
                    )
            result[event_id] = encode(
                {
                    "schema_version": 1,
                    "event_id": event_id,
                    "agent_id": AGENT,
                    "deployment_id": bound.deployment_id,
                    "instance_id": bound.instance_id,
                    "profile_revision": bound.profile_revision,
                    "source_seq": offset,
                    "occurred_at": row["timestamp"],
                    "type": kind,
                    "source": None if peer is None else {"ip": peer["host"], "port": peer["port"]},
                    "service": row["service"],
                    "data": data,
                    "artifact_ids": [],
                    "session_id": str(
                        uuid5(NAMESPACE_URL, encode([*scope, "low", row["connection_id"]]))
                    ),
                }
            ).encode()
        offset += len(raw)
    return result


def worker_for(path, root, endpoint, clock=None, timeout=2):
    return LowTelemetryWorker(
        path,
        root,
        AGENT,
        HttpBatchTransport(endpoint, SecretStr(TOKEN), timeout),
        clock=clock or Clock(),
        jitter=lambda low, high: high,
    )


class Receiver:
    """Тестовый commit до ACK, pair-dedup и явная передача ошибок server-thread в pytest."""

    def __init__(self, path):
        self.path = path
        self.mode = "accepted"
        self.entered = threading.Event()
        self.release = threading.Event()
        self.errors = []
        with closing(sqlite3.connect(path)) as db, db:
            db.execute(
                "CREATE TABLE events (agent TEXT, id TEXT, payload BLOB, PRIMARY KEY(agent,id))"
            )
            db.execute("CREATE TABLE requests (payload BLOB)")

    def rows(self, table):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()

    def events(self):
        rows = self.rows("events")
        assert {agent for agent, _, _ in rows} <= {AGENT}
        return {event_id: raw for _, event_id, raw in rows}

    def reply(self, handler):
        try:
            payload = handler.rfile.read(int(handler.headers["Content-Length"]))
            batch = Batch.model_validate_json(payload)
            accepted, rejected = [], []
            mode = self.mode
            with closing(sqlite3.connect(self.path)) as db, db:
                db.execute("INSERT INTO requests VALUES (?)", (payload,))
                if mode not in {"503", "401", "403", "hold"}:
                    for index, item in enumerate(batch.events):
                        if mode == "mixed" and index in {1, 2}:
                            rejected.append(
                                {
                                    "index": index,
                                    "event_id": str(item.event_id),
                                    "code": "INVALID_EVENT"
                                    if index == 1
                                    else "TEMPORARILY_UNAVAILABLE",
                                    "retryable": index == 2,
                                }
                            )
                            continue
                        key = (str(item.agent_id), str(item.event_id))
                        raw = item.canonical_json().encode()
                        old = db.execute(
                            "SELECT payload FROM events WHERE agent=? AND id=?", key
                        ).fetchone()
                        assert old is None or old == (raw,), "Изменены bytes прежнего Event"
                        db.execute("INSERT OR IGNORE INTO events VALUES (?,?,?)", (*key, raw))
                        accepted.append(str(item.event_id))
            self.entered.set()
            if mode == "drop":
                handler.connection.shutdown(socket.SHUT_RDWR)
                return
            if mode == "hold":
                assert self.release.wait(5), "Не освобождён HTTP-барьер"
                return
            ack = {"batch_id": str(batch.batch_id), "accepted_ids": accepted, "rejected": rejected}
            if mode == "foreign":
                ack["batch_id"] = "ffffffff-ffff-4fff-8fff-ffffffffffff"
            if mode == "incomplete":
                ack["accepted_ids"] = accepted[:-1]
            body = b"ACK_SECRET_INVALID_JSON" if mode == "invalid" else json.dumps(ack).encode()
            handler.send_response(int(mode) if mode.isdigit() else 200)
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)
        except BaseException as error:
            self.errors.append(error)

    @contextmanager
    def serve(self, reserved=None):
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                receiver.reply(self)

            def log_message(self, *_args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler, bind_and_activate=reserved is None)
        if reserved is not None:
            server.socket.close()
            server.socket = reserved
            server.server_address = reserved.getsockname()
            server.server_activate()
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01))
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_address[1]}/agent/v1/events"
        finally:
            self.release.set()
            server.shutdown()
            server.server_close()
            thread.join(5)
            assert not thread.is_alive()
            assert not self.errors, self.errors


def child_env():
    root = Path(__file__).resolve().parents[2]
    return {**os.environ, "PYTHONPATH": os.pathsep.join([str(root), str(Path(__file__).parent)])}


def run_worker_child(db, root, endpoint, mode="normal"):
    """IPC строка выдаётся transport только после долговечного pending/ledger commit."""
    script = """
import os, sys
from contextlib import closing
from pathlib import Path
from test_low_journal_reader_contract import binding
from continuous_low_support import worker_for
mode = sys.argv[4]
with closing(worker_for(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3])) as worker:
    if mode == 'crash':
        class CrashTransport:
            def post(self, payload):
                print('PENDING_COMMITTED', flush=True)
                os._exit(79)
        worker._sender._transport = CrashTransport()
    print(worker.step((binding(),)), flush=True)
"""
    child = subprocess.run(
        [sys.executable, "-c", script, str(db), str(root), endpoint, mode],
        env=child_env(),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert child.returncode == (79 if mode == "crash" else 0), child.stderr
    if mode == "crash":
        assert child.stdout.strip() == "PENDING_COMMITTED"
    return child
