"""P01–P12: настоящий HTTP sender, PostgreSQL и сетевые REST/WS в тестовой сборке."""

import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from threading import Thread
from uuid import UUID

import httpx
import pytest
import uvicorn
from agent.telemetry.adapters.low import SourceRecord
from agent.telemetry.schemas import Ack, Batch, Event
from agent.telemetry.sender import HttpBatchTransport, TelemetrySender
from agent.telemetry.spool import SpoolRepository
from sqlalchemy import delete, func, select, update
from sqlalchemy import event as sql_event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from websockets.exceptions import ConnectionClosedError
from websockets.sync.client import connect

from honeyforge.auth.security import Security
from honeyforge.common import utcnow
from honeyforge.models import User, UserSession
from honeyforge.telemetry.publisher import OutboxPublisher
from honeyforge.telemetry.storage import EventStorage

from .integration_app import TEST_ORIGIN, TEST_TOKEN, build_test_app

AGENT = str(UUID(int=1))
COOKIE = "hf_session_test-integration-only"


def synthetic(number=1, agent=1, password="test-private-password"):
    """Event v1 с детерминированной парой ID; не production Low mapping."""
    return Event.model_validate(
        {
            "schema_version": 1,
            "event_id": str(UUID(int=number)),
            "agent_id": str(UUID(int=agent)),
            "deployment_id": str(UUID(int=10 + agent)),
            "instance_id": str(UUID(int=20 + agent)),
            "profile_revision": 1,
            "source_seq": number,
            "occurred_at": "2026-10-11T00:00:00Z",
            "type": "auth.attempt",
            "source": {"ip": "192.0.2.1", "port": 3456},
            "service": {"protocol": "ssh", "port": 2222},
            "data": {"username": "test", "password": password, "success": False},
            "artifact_ids": [],
        }
    )


def identity(event):
    return event.agent_id, event.deployment_id, event.instance_id, event.profile_revision


@contextmanager
def loopback_server(app):
    """Предварительно связанный порт исключает гонку выбора адреса и общий стенд."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", ws="websockets"))
    thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            if not thread.is_alive() or time.monotonic() >= deadline:
                raise RuntimeError("Тестовый HTTP-сервер не запущен")
            time.sleep(0.01)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(5)
        listener.close()
        if thread.is_alive():
            raise RuntimeError("Тестовый HTTP-сервер не остановился")


@pytest.fixture
def lab(engine, tables, settings):
    scoped = engine.execution_options(schema_translate_map={None: tables.metadata.schema})
    User.metadata.create_all(scoped, tables=[User.__table__, UserSession.__table__])
    with Session(scoped) as session:
        viewer = User(username="test-viewer", role="viewer", password_hash="unused")
        session.add(viewer)
        session.flush()
        session.add(
            UserSession(
                token_hash=Security(settings).digest(COOKIE),
                user_id=viewer.id,
                expires_at=utcnow() + timedelta(hours=1),
            )
        )
        session.commit()
    services = build_test_app(
        settings,
        scoped,
        tables,
        {
            "Bearer " + TEST_TOKEN: identity(synthetic()),
            "Bearer hf_agent_test-second": identity(synthetic(agent=2)),
        },
    )
    with loopback_server(services.app) as url:
        with httpx.Client(
            base_url=url, headers={"Origin": TEST_ORIGIN}, cookies={"hf_session": COOKIE}, timeout=3
        ) as client:
            yield services, client, scoped, url


def counts(engine, tables):
    with engine.connect() as connection:
        return tuple(
            connection.scalar(select(func.count()).select_from(table))
            for table in (tables.events, tables.outbox, tables.publications)
        )


def originals(engine, tables):
    with engine.connect() as connection:
        return {
            (str(agent), str(eid)): bytes(payload)
            for agent, eid, payload in connection.execute(
                select(
                    tables.events.c.agent_id,
                    tables.events.c.event_id,
                    tables.events.c.canonical_bytes,
                )
            )
        }


def sender(spool, url, **options):
    from pydantic import SecretStr

    return TelemetrySender(
        spool,
        HttpBatchTransport(url + "/test/ingest", SecretStr(TEST_TOKEN), timeout=0.5),
        **options,
    )


def append(spool, *events):
    for event in events:
        offset = spool.checkpoint("synthetic-test")
        spool.append(SourceRecord("synthetic-test", offset, offset + 1, event))


def websocket(url, cookie=COOKIE, origin=TEST_ORIGIN):
    return connect(
        url.replace("http://", "ws://") + "/api/v1/stream",
        origin=origin,
        additional_headers={"Cookie": "hf_session=" + cookie},
        open_timeout=3,
        close_timeout=3,
        proxy=None,
    )


def receive(ws):
    return json.loads(ws.recv(timeout=3))


def post(client, *events):
    batch = Batch(events=list(events))
    response = client.post(
        "/test/ingest", content=batch.to_bytes(), headers={"Authorization": "Bearer " + TEST_TOKEN}
    )
    return batch, response


def test_lost_commit_response_restart_ack_before_publisher_and_live(lab, tables, tmp_path):
    services, client, scoped, url = lab
    path = tmp_path / "sender.sqlite3"
    spool = SpoolRepository(path, AGENT)
    event = synthetic()
    try:
        append(spool, event)
        services.control.mode = "lost_response"
        result = sender(spool, url).step()
        assert result.state == "retry" and result.error_code == "TRANSPORT_ERROR"
        assert spool.count() == 1
        assert counts(scoped, tables) == (1, 1, 0)
        assert client.get("/api/v1/events").json()["items"] == []
    finally:
        spool.close()
    spool = SpoolRepository(path, AGENT)
    try:
        assert sender(spool, url).step().accepted == 1
        assert spool.count() == 0
        assert counts(scoped, tables) == (1, 1, 0)
        sent = [
            Batch.model_validate_json(p).events[0].canonical_json()
            for p in services.control.payloads
        ]
        assert sent == [event.canonical_json()] * 2
        with websocket(url) as ws:
            assert receive(ws) == {"type": "hello", "watermark": "0"}
            assert services.publisher.publish_once() == 1
            live = receive(ws)
            assert live["type"] == "event" and live["stream_seq"] == "1"
            public = client.get("/api/v1/events").json()["items"][0]
            assert live["event"] == public
            assert "test-private-password" not in json.dumps(public)
            assert public["data"]["password"] == "[test-masked]"
        assert OutboxPublisher(scoped, tables).publish_once() == 0
        assert counts(scoped, tables) == (1, 1, 1)
        assert originals(scoped, tables) == {
            (AGENT, str(event.event_id)): event.canonical_json().encode()
        }
    finally:
        spool.close()


@pytest.mark.parametrize("stage", ["event_inserted", "before_commit", "commit"])
def test_storage_failure_never_acknowledges(lab, tables, tmp_path, stage):
    services, client, scoped, url = lab

    class FaultStorage(EventStorage):
        def _checkpoint(self, point, session):
            if point == stage:
                raise OperationalError("test fault", {}, Exception("injected"))

    def fail_commit(connection):
        raise OperationalError("test commit fault", {}, Exception("injected"))

    original = services.storage.store
    services.storage.store = FaultStorage(scoped, tables).store
    spool = SpoolRepository(tmp_path / "failure.sqlite3", AGENT)
    try:
        append(spool, synthetic())
        if stage == "commit":
            sql_event.listen(scoped, "commit", fail_commit)
        try:
            result = sender(spool, url).step()
            assert result.state == "retry" and result.http_status == 503
        finally:
            if stage == "commit":
                sql_event.remove(scoped, "commit", fail_commit)
        assert spool.count() == 1 and counts(scoped, tables) == (0, 0, 0)
        services.storage.store = original
        assert sender(spool, url).step().accepted == 1
        assert counts(scoped, tables) == (1, 1, 0)
    finally:
        spool.close()


def test_mixed_conflicting_hidden_original_and_invalid_neighbors(lab, tables):
    services, client, scoped, _ = lab
    original = synthetic()
    assert post(client, original)[1].status_code == 200
    batch, response = post(
        client, synthetic(2), synthetic(password="changed-private"), synthetic(3)
    )
    ack = Ack.model_validate_json(response.content)
    assert ack.batch_id == batch.batch_id
    assert set(ack.accepted_ids) == {UUID(int=2), UUID(int=3)}
    assert [(r.index, r.code, r.retryable) for r in ack.rejected] == [
        (1, "EVENT_ID_CONFLICT", False)
    ]
    assert (
        originals(scoped, tables)[(AGENT, str(original.event_id))]
        == original.canonical_json().encode()
    )
    bad = json.loads(Batch(events=[synthetic(4), synthetic(5), synthetic(6)]).to_bytes())
    # Явный null теперь валиден; пустой объект по-прежнему нарушает endpoint контракт.
    bad["events"][1]["source"] = {}
    response = client.post(
        "/test/ingest", json=bad, headers={"Authorization": "Bearer " + TEST_TOKEN}
    )
    ack = Ack.model_validate_json(response.content)
    assert set(ack.accepted_ids) == {UUID(int=4), UUID(int=6)}
    assert [(r.index, r.event_id, r.code) for r in ack.rejected] == [(1, None, "INVALID_EVENT")]
    assert counts(scoped, tables) == (5, 5, 0)


def test_concurrent_http_duplicates_and_conflicts(lab, tables):
    services, _, scoped, url = lab

    def send(password):
        batch = Batch(events=[synthetic(password=password)])
        with httpx.Client(timeout=10) as client:
            return Ack.model_validate_json(
                client.post(
                    url + "/test/ingest",
                    content=batch.to_bytes(),
                    headers={"Authorization": "Bearer " + TEST_TOKEN},
                ).content
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = list(pool.map(send, ["same", "same"]))
    assert all(ack.accepted_ids == [UUID(int=1)] for ack in first)
    with ThreadPoolExecutor(max_workers=2) as pool:
        second = list(pool.map(send, ["same", "changed"]))
    assert second[0].accepted_ids == [UUID(int=1)]
    assert second[1].rejected[0].code == "EVENT_ID_CONFLICT"
    assert counts(scoped, tables) == (1, 1, 0)


def test_publisher_rollback_restart_snapshot_reconnect_and_bigint(lab, tables):
    services, client, scoped, url = lab
    post(client, synthetic(1), synthetic(2))
    with scoped.begin() as connection:
        connection.execute(update(tables.state).values(last_seq=2**53))

    class FailedPublisher(OutboxPublisher):
        def _checkpoint(self, stage, session):
            if stage == "before_commit":
                raise RuntimeError("Управляемый rollback издателя")

    with pytest.raises(RuntimeError):
        FailedPublisher(scoped, tables).publish_once()
    assert client.get("/api/v1/events").json()["items"] == []
    assert counts(scoped, tables) == (2, 2, 0)
    assert OutboxPublisher(scoped, tables).publish_once() == 2
    first = client.get("/api/v1/events", params={"limit": 1}).json()
    old_ids = {first["items"][0]["event_id"]}
    with websocket(url) as ws:
        assert receive(ws)["watermark"] == str(2**53 + 2)
    # Публикация во время разрыва восстанавливается REST до hello нового WS.
    post(client, synthetic(3))
    services.publisher.publish_once()
    second = client.get(
        "/api/v1/events", params={"limit": 1, "cursor": first["next_cursor"]}
    ).json()
    assert old_ids | {item["event_id"] for item in second["items"]} == {
        str(UUID(int=1)),
        str(UUID(int=2)),
    }
    assert second["next_cursor"] is None
    with websocket(url) as ws:
        hello = receive(ws)
        page = client.get(
            "/api/v1/stream-events", params={"after": str(2**53 + 2), "until": hello["watermark"]}
        ).json()
        recovered = page["items"]
        post(client, synthetic(4))
        services.publisher.publish_once()
        recovered.append(receive(ws))
        assert {row["event"]["event_id"] for row in recovered} == {
            str(UUID(int=3)),
            str(UUID(int=4)),
        }
        assert all(
            isinstance(row["stream_seq"], str) and int(row["stream_seq"]) > 2**53
            for row in recovered
        )
    # Одинаковый event_id разных агентов сохраняет обе пары.
    response = client.post(
        "/test/ingest",
        content=Batch(events=[synthetic(1, agent=2)]).to_bytes(),
        headers={"Authorization": "Bearer hf_agent_test-second"},
    )
    assert Ack.model_validate_json(response.content).accepted_ids == [UUID(int=1)]
    services.publisher.publish_once()
    items = client.get("/api/v1/events").json()["items"]
    assert len({(item["agent_id"], item["event_id"]) for item in items}) == 5
    assert counts(scoped, tables) == (5, 5, 5)


def test_access_cookie_origin_expiry_and_idle_revoke(lab):
    _, client, scoped, url = lab
    for cookie, origin, code in [
        ("invalid", TEST_ORIGIN, 4401),
        (COOKIE, "https://untrusted.test", 4403),
    ]:
        with websocket(url, cookie, origin) as ws:
            with pytest.raises(ConnectionClosedError) as closed:
                receive(ws)
            assert closed.value.code == code
    assert client.get("/api/v1/events", headers={"Cookie": "hf_session=invalid"}).status_code == 401
    with Session(scoped) as session:
        session.execute(update(UserSession).values(expires_at=utcnow() - timedelta(seconds=1)))
        session.commit()
    assert client.get("/api/v1/events").status_code == 401
    with websocket(url) as ws:
        with pytest.raises(ConnectionClosedError) as closed:
            receive(ws)
        assert closed.value.code == 4401
    with Session(scoped) as session:
        session.execute(update(UserSession).values(expires_at=utcnow() + timedelta(hours=1)))
        session.commit()
    with websocket(url) as ws:
        assert receive(ws)["type"] == "hello"
        with Session(scoped) as session:
            session.execute(delete(UserSession))
            session.commit()
        with pytest.raises(ConnectionClosedError) as closed:
            receive(ws)
        assert closed.value.code == 4401


@pytest.mark.parametrize("mode", ["outage", "invalid_ack", "timeout_after_commit"])
def test_backoff_bad_ack_and_recovery(lab, tables, tmp_path, mode):
    services, _, scoped, url = lab
    clock = [0.0]
    spool = SpoolRepository(tmp_path / "retry.sqlite3", AGENT)
    try:
        append(spool, synthetic())
        services.control.mode = mode
        delivery = sender(spool, url, clock=lambda: clock[0], jitter=lambda low, high: high)
        result = delivery.step()
        assert result.state == "retry" and spool.count() == 1
        assert delivery.step().state == "waiting"
        assert len(services.control.payloads) == 1
        services.control.mode = "normal"
        clock[0] += result.retry_in
        assert delivery.step().accepted == 1
        assert spool.count() == 0 and counts(scoped, tables) == (1, 1, 0)
        assert services.control.payloads[0] == services.control.payloads[1]
    finally:
        spool.close()


def test_invalid_batch_test_auth_and_identity(lab, tables):
    _, client, scoped, _ = lab
    assert client.post("/test/ingest", json={}).status_code == 401
    headers = {"Authorization": "Bearer " + TEST_TOKEN}
    malformed_id = json.loads(Batch(events=[synthetic()]).to_bytes())
    malformed_id["batch_id"] = 123
    for body in [
        b"{}",
        b'{"schema_version":1,"schema_version":1}',
        json.dumps(malformed_id).encode(),
        b"x" * (2 * 1024 * 1024 + 1),
    ]:
        assert client.post("/test/ingest", content=body, headers=headers).status_code in {413, 422}
    response = client.post(
        "/test/ingest", content=Batch(events=[synthetic(agent=2)]).to_bytes(), headers=headers
    )
    assert Ack.model_validate_json(response.content).rejected[0].code == "DEPLOYMENT_MISMATCH"
    assert counts(scoped, tables) == (0, 0, 0)


@pytest.mark.parametrize("stage", ["before_commit", "after_commit"])
def test_publisher_process_crash_and_restart(lab, tables, stage):
    """P06: настоящий отдельный процесс, не только исключение/новый объект."""
    services, client, scoped, _ = lab
    post(client, synthetic())
    script = """
import os, sys
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from honeyforge.telemetry.tables import TelemetryTables
from honeyforge.telemetry.publisher import OutboxPublisher
url = os.environ['HF_TEST_DATABASE_URL']
assert make_url(url).database == 'honeyforge_test'
engine = create_engine(url, connect_args={'connect_timeout': 5})
tables = TelemetryTables(sys.argv[1])
class Crash(OutboxPublisher):
    def _checkpoint(self, point, session):
        if point == sys.argv[2]: os._exit(77)
Crash(engine, tables).publish_once()
os._exit(78)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, tables.metadata.schema, stage],
        capture_output=True,
        timeout=15,
    )
    committed = stage == "after_commit"
    assert result.returncode == (78 if committed else 77)
    assert counts(scoped, tables) == (1, 1, int(committed))
    assert len(client.get("/api/v1/events").json()["items"]) == int(committed)
    assert OutboxPublisher(scoped, tables).publish_once() == (0 if committed else 1)
    assert services.publisher.publish_once() == 0
    assert counts(scoped, tables) == (1, 1, 1)


def test_automatic_acceptance(lab, tables, tmp_path):
    """E01–E02: exact identity/payload при outage, рестартах и WS-разрыве."""
    services, client, scoped, url = lab
    start = time.monotonic()
    durations = {}
    events = [synthetic(number) for number in range(1, 24)]
    expected = {(str(e.agent_id), str(e.event_id)): e.canonical_json().encode() for e in events}
    spool_path = tmp_path / "acceptance.sqlite3"
    spool = SpoolRepository(spool_path, AGENT)
    services.control.mode = "outage"
    try:
        append(spool, *events[:10])
        assert sender(spool, url).step().state == "retry"
        append(spool, *events[10:])
        assert spool.count() == 23 and counts(scoped, tables) == (0, 0, 0)
    finally:
        spool.close()
    durations["offline_accumulation"] = time.monotonic() - start
    spool = SpoolRepository(spool_path, AGENT)
    try:
        services.control.mode = "lost_response"
        assert sender(spool, url).step().error_code == "TRANSPORT_ERROR"
        assert spool.count() == 23 and originals(scoped, tables) == expected
        assert sender(spool, url).step().accepted == 23
        assert spool.count() == 0
    finally:
        spool.close()
    durations["delivery_after_restart"] = (
        time.monotonic() - start - durations["offline_accumulation"]
    )
    with websocket(url) as ws:
        assert receive(ws)["watermark"] == "0"
        for count in [10, 10, 3]:
            assert services.publisher.publish_once(limit=10) == count
        live = [receive(ws) for _ in events]
        assert {(row["event"]["agent_id"], row["event"]["event_id"]) for row in live} == set(
            expected
        )
        assert all(row["event"]["data"]["password"] == "[test-masked]" for row in live)
    history = client.get("/api/v1/events", params={"limit": 5}).json()
    snapshot = list(history["items"])
    late = synthetic(24)
    post(client, late)
    services.publisher.publish_once()
    while history["next_cursor"] is not None:
        history = client.get(
            "/api/v1/events", params={"limit": 5, "cursor": history["next_cursor"]}
        ).json()
        snapshot.extend(history["items"])
    assert {(row["agent_id"], row["event_id"]) for row in snapshot} == set(expected)
    with websocket(url) as ws:
        hello = receive(ws)
        recovered = client.get(
            "/api/v1/stream-events", params={"after": "23", "until": hello["watermark"]}
        ).json()["items"]
        final = synthetic(25)
        post(client, final)
        services.publisher.publish_once()
        recovered.append(receive(ws))
        assert {row["event"]["event_id"] for row in recovered} == {
            str(UUID(int=24)),
            str(UUID(int=25)),
        }
    for item in [late, final]:
        expected[(str(item.agent_id), str(item.event_id))] = item.canonical_json().encode()
    # Намеренный повтор всего набора серверу после первого ACK.
    replay = SpoolRepository(tmp_path / "replay.sqlite3", AGENT)
    try:
        append(replay, *events, late, final)
        assert sender(replay, url).step().accepted == 25 and replay.count() == 0
    finally:
        replay.close()
    assert OutboxPublisher(scoped, tables).publish_once() == 0
    assert originals(scoped, tables) == expected
    assert counts(scoped, tables) == (25, 25, 25)
    public = client.get("/api/v1/events").json()["items"]
    assert len(public) == 25
    assert {(row["agent_id"], row["event_id"]) for row in public} == set(expected)
    report = {
        "status": "passed",
        "synthetic_event": True,
        "browser_verified": False,
        "production_low_verified": False,
        "unique_pairs": len(expected),
        "counts_event_outbox_publication": list(counts(scoped, tables)),
        "pending": 0,
        "canonical_payloads_verified": True,
        "masked_rest_ws_verified": True,
        "duplicate_publications": 0,
        "durations_seconds": {**durations, "total_scenario": time.monotonic() - start},
        "expected": [
            {
                "agent_id": agent,
                "event_id": eid,
                "canonical_sha256": hashlib.sha256(payload).hexdigest(),
            }
            for (agent, eid), payload in sorted(expected.items())
        ],
    }
    target = os.environ.get("HF_ACCEPTANCE_REPORT")
    if target:
        Path(target).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
