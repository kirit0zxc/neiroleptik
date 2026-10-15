"""C05/C12/C13: реальные дочерние процессы и HTTP без production-стенда."""

import asyncio
import base64
import json
import queue
import subprocess
import sys
import threading
from contextlib import closing

import pytest
from continuous_low_support import (
    Receiver,
    child_env,
    expected_events,
    journal,
    run_worker_child,
    snapshot,
    worker_for,
)
from test_low_journal_reader_contract import binding, event, footer, header


@pytest.mark.parametrize("stage", ["lost_ack", "pending_crash"])
def test_c05_c13_new_process_recovers_exact_saved_events(tmp_path, stage):
    source = journal(tmp_path, rows=[header(), event(), event(seq=2), footer(last_seq=2)])
    expected = expected_events(source)
    db = tmp_path / "spool.db"
    receiver = Receiver(tmp_path / "receiver.db")
    receiver.mode = "drop" if stage == "lost_ack" else "accepted"
    with receiver.serve() as endpoint:
        first = run_worker_child(
            db, tmp_path, endpoint, "normal" if stage == "lost_ack" else "crash"
        )
        if stage == "lost_ack":
            assert "TRANSPORT_ERROR" in first.stdout
            assert receiver.events() == expected
        else:
            assert receiver.rows("requests") == []
        before = snapshot(db)
        assert {row[1]: row[2].encode() for row in before["pending"]} == expected
        assert [row[-1] for row in before["low_raw_records"]] == source.read_bytes().splitlines(
            True
        )
        assert before["low_raw_generations"][0][2] == source.stat().st_size
        assert len(before["low_conversion_ledger"]) == 4
        receiver.mode = "accepted"
        assert "delivered:2" in run_worker_child(db, tmp_path, endpoint).stdout
        after = snapshot(db)
        assert not after["pending"] and not after["quarantine"]
        for table in ("low_raw_records", "low_raw_generations", "low_conversion_ledger"):
            assert after[table] == before[table]
        assert receiver.events() == expected
        requests = receiver.rows("requests")
        assert len(requests) == (2 if stage == "lost_ack" else 1)
        if stage == "lost_ack":
            original, repeated = [json.loads(row[0]) for row in requests]
            assert original["events"] == repeated["events"]
            assert original["batch_id"] != repeated["batch_id"]
        assert "idle" in run_worker_child(db, tmp_path, endpoint).stdout
        assert snapshot(db) == after


LOW_PROCESS = """
import asyncio, json, sys
from pathlib import Path
from honeypots.low import LowService, LowConfig
from honeypots.low.journal import JournalSettings, RotatingJournalSink
from test_low_journal_reader_contract import binding
async def main():
    bound = binding()
    sink = RotatingJournalSink(JournalSettings(
        directory=Path(sys.argv[1]), instance_id=bound.instance_id,
        deployment_id=bound.deployment_id, profile_revision=bound.profile_revision))
    sink.open()
    service = LowService(LowConfig(tcp_port=0, http_port=0), sink)
    try:
        await service.start()
        print(json.dumps([service.tcp_port, service.http_port]), flush=True)
        assert await asyncio.to_thread(sys.stdin.readline) == 'stop\\n'
    finally:
        await service.stop()
        sink.close()
asyncio.run(main())
"""


def test_c12_real_low_tcp_http_child_production_mapping_and_transport(tmp_path):
    directory = tmp_path / binding().deployment_id
    directory.mkdir()
    child = subprocess.Popen(
        [sys.executable, "-c", LOW_PROCESS, str(directory)],
        env=child_env(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    tcp_payload = b"\x00\xff\xfe" + "Привет".encode()
    http_payload = (
        b"GET /private?secret=LOW_SECRET HTTP/1.1\r\nHost: test\r\nX-Test: "
        + "мир".encode()
        + b"\r\n\r\n"
    )
    ready = queue.Queue()
    reader_thread = threading.Thread(target=lambda: ready.put(child.stdout.readline()), daemon=True)
    reader_thread.start()

    async def exercise(tcp_port, http_port):
        reader, writer = await asyncio.open_connection("127.0.0.1", tcp_port)
        try:
            assert (await asyncio.wait_for(reader.readline(), 3)).startswith(b"SSH-")
            writer.write(tcp_payload)
            await writer.drain()
            writer.write_eof()
            await asyncio.wait_for(reader.read(), 3)
        finally:
            writer.close()
            await writer.wait_closed()
        reader, writer = await asyncio.open_connection("127.0.0.1", http_port)
        try:
            writer.write(http_payload)
            await writer.drain()
            assert (await asyncio.wait_for(reader.read(), 3)).startswith(b"HTTP/1.1 200")
        finally:
            writer.close()
            await writer.wait_closed()

    try:
        # Readiness по IPC после bind; сетевой probe не добавляет скрытый connection.
        # Отдельный daemon-reader позволяет finally убить только нашего ребёнка и при EOF timeout.
        tcp_port, http_port = json.loads(ready.get(timeout=10))
        asyncio.run(exercise(tcp_port, http_port))
        child.stdin.write("stop\n")
        child.stdin.flush()
        _, error = child.communicate(timeout=10)
        assert child.returncode == 0, error
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=5)
        reader_thread.join(5)
        assert not reader_thread.is_alive()
    [source] = directory.glob("low-*.jsonl")
    rows = [json.loads(raw) for raw in source.read_bytes().splitlines()]
    assert [row["record"] for row in rows] == [
        "header",
        "event",
        "event",
        "event",
        "event",
        "footer",
    ]
    assert [row["event"] for row in rows[1:-1]] == [
        "connection",
        "request",
        "connection",
        "request",
    ]
    assert base64.b64decode(rows[2]["payload_b64"]) == tcp_payload
    assert base64.b64decode(rows[4]["payload_b64"]) == http_payload
    expected = expected_events(source)
    receiver = Receiver(tmp_path / "receiver.db")
    db = tmp_path / "spool.db"
    with receiver.serve() as endpoint, closing(worker_for(db, tmp_path, endpoint)) as worker:
        assert worker.step((binding(),))[1] == "delivered:4"
        assert worker.step((binding(),))[1] == "idle"
    assert receiver.events() == expected
    state = snapshot(db)
    assert [row[-1] for row in state["low_raw_records"]] == source.read_bytes().splitlines(True)
    assert state["low_raw_generations"][0][2] == source.stat().st_size
    assert len(state["low_conversion_ledger"]) == 6
    assert not state["pending"] and not state["quarantine"]
    actual = [json.loads(raw) for raw in receiver.events().values()]
    assert {row["type"] for row in actual} == {"connection.opened", "tcp.request", "http.request"}
    for opened, request_row in ((rows[1], rows[2]), (rows[3], rows[4])):
        assert opened["connection_id"] == request_row["connection_id"]
    assert not state["low_boundary_bindings"]
