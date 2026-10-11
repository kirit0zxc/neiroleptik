"""Совместные PostgreSQL transaction/publisher → history/catch-up/live проверки."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from uuid import UUID

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from honeyforge.telemetry.cursors import CursorCodec
from honeyforge.telemetry.history import CatchUpService, HistoryService
from honeyforge.telemetry.presentation import PublicEventPresenter
from honeyforge.telemetry.publisher import OutboxPublisher
from honeyforge.telemetry.read_adapter import CommittedReadAdapter
from honeyforge.telemetry.read_contract import HistoryFilters
from honeyforge.telemetry.reader import PublicationReader
from honeyforge.telemetry.storage import EventStorage
from honeyforge.telemetry.stream import LiveStreamService
from honeyforge.telemetry.types import PreparedEvent

from .test_read_services import NOW, mask, record


def prepared(number, agent=1, kind="session.command", occurred=NOW):
    event = record(number, agent=agent, kind=kind).event
    event["occurred_at"] = occurred.isoformat()
    if kind == "auth.attempt":
        event["data"] = {"username": "test", "password": "secret", "success": False}
    return PreparedEvent(
        number * 100 + agent,
        UUID(event["agent_id"]),
        UUID(event["event_id"]),
        UUID(event["deployment_id"]),
        UUID(event["instance_id"]),
        1,
        json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(),
    )


def sql_services(engine, tables):
    adapter = CommittedReadAdapter(PublicationReader(engine, tables))
    presenter = PublicEventPresenter(mask)
    return (
        adapter,
        HistoryService(adapter, CursorCodec(b"k" * 32), presenter),
        CatchUpService(adapter, presenter),
    )


def test_pending_publication_snapshot_late_received_and_ties(engine, tables, monkeypatch):
    adapter, history, catchup = sql_services(engine, tables)
    monkeypatch.setattr("honeyforge.telemetry.storage.utcnow", lambda: NOW)
    storage = EventStorage(engine, tables)
    storage.store([prepared(1), prepared(1, agent=2), prepared(3)])
    assert history.page(HistoryFilters())["items"] == []
    assert catchup.page()["watermark"] == "0"
    publisher = OutboxPublisher(engine, tables)
    assert publisher.publish_once() == 3
    first = history.page(HistoryFilters(), limit=1)
    assert first["items"][0]["event_id"] == str(UUID(int=3))
    monkeypatch.setattr("honeyforge.telemetry.storage.utcnow", lambda: NOW - timedelta(seconds=1))
    storage.store([prepared(4)])
    publisher.publish_once()
    second = history.page(HistoryFilters(), first["next_cursor"], 1)
    third = history.page(HistoryFilters(), second["next_cursor"], 1)
    assert second["items"][0]["agent_id"] == str(UUID(int=2))
    assert third["items"][0]["agent_id"] == str(UUID(int=1))
    assert third["next_cursor"] is None
    assert len(history.page(HistoryFilters())["items"]) == 4
    assert adapter.watermark() == 4


def test_sql_filters_and_bigint_stream_boundaries(engine, tables):
    storage = EventStorage(engine, tables)
    storage.store(
        [
            prepared(1),
            prepared(2, kind="http.request"),
            prepared(3, occurred=NOW + timedelta(seconds=1)),
        ]
    )
    with engine.begin() as connection:
        connection.execute(update(tables.state).values(last_seq=2**53))
    OutboxPublisher(engine, tables).publish_once()
    adapter, history, catchup = sql_services(engine, tables)
    page = catchup.page("0", limit=1)
    assert page["items"][0]["stream_seq"] == str(2**53 + 1)
    assert catchup.page(page["next_cursor"], page["watermark"], 2)["next_cursor"] is None
    filters = history.filters(
        str(UUID(int=10)),
        "192.0.2.1",
        "session.command",
        NOW.isoformat(),
        (NOW + timedelta(seconds=1)).isoformat(),
    )
    assert [e["event_id"] for e in history.page(filters)["items"]] == [str(UUID(int=1))]
    assert adapter.retention_floor() is None


@pytest.mark.parametrize("rollback", [False, True])
def test_uncommitted_publisher_invisible_and_rollback(engine, tables, rollback):
    EventStorage(engine, tables).store([prepared(1)])
    entered, release = Event(), Event()

    class PausedPublisher(OutboxPublisher):
        def _checkpoint(self, stage: str, session: Session) -> None:
            if stage == "before_commit":
                entered.set()
                assert release.wait(10)
                if rollback:
                    raise RuntimeError("Управляемый rollback")

    adapter, history, catchup = sql_services(engine, tables)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(PausedPublisher(engine, tables).publish_once)
        try:
            assert entered.wait(10)
            assert adapter.watermark() == 0
            assert history.page(HistoryFilters())["items"] == []
            assert catchup.page()["items"] == []
        finally:
            release.set()
        if rollback:
            with pytest.raises(RuntimeError):
                future.result(10)
        else:
            assert future.result(10) == 1
    assert adapter.watermark() == (0 if rollback else 1)


def test_live_commit_between_watermark_and_poll_and_two_instances(engine, tables):
    async def scenario():
        adapter, history, catchup = sql_services(engine, tables)
        captured, release = Event(), Event()

        class WatermarkBarrier(CommittedReadAdapter):
            first = True

            def watermark(self):
                watermark = super().watermark()
                if self.first:
                    self.first = False
                    captured.set()
                    assert release.wait(10)
                return watermark

        barrier = WatermarkBarrier(PublicationReader(engine, tables))
        outputs = [[], []]
        got = [asyncio.Event(), asyncio.Event()]
        tasks = []

        async def send0(value):
            outputs[0].append(value)
            if value["type"] == "event":
                got[0].set()

        tasks.append(
            asyncio.create_task(
                LiveStreamService(barrier, history.presenter, poll_seconds=0.01).run(
                    send0, lambda: None
                )
            )
        )
        try:
            assert await asyncio.to_thread(captured.wait, 10)
            await asyncio.to_thread(
                EventStorage(engine, tables).store, [prepared(1, kind="auth.attempt")]
            )
            await asyncio.to_thread(OutboxPublisher(engine, tables).publish_once)
            release.set()
            await asyncio.wait_for(got[0].wait(), 10)
            assert outputs[0][0] == {"type": "hello", "watermark": "0"}
            assert outputs[0][1]["stream_seq"] == "1"
            assert outputs[0][1]["event"]["data"]["password"] == "••••"

            async def send1(value):
                outputs[1].append(value)
                if value["type"] == "hello":
                    got[1].set()

            tasks.append(
                asyncio.create_task(
                    LiveStreamService(adapter, history.presenter, poll_seconds=0.01).run(
                        send1, lambda: None
                    )
                )
            )
            await asyncio.wait_for(got[1].wait(), 10)
            assert outputs[1][0] == {"type": "hello", "watermark": "1"}
            assert catchup.page("0", "1")["items"][0]["event"] == outputs[0][1]["event"]
        finally:
            release.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        assert adapter.watermark() == 1
        with engine.connect() as connection:
            original = bytes(connection.scalar(select(tables.events.c.canonical_bytes)))
        assert b"secret" in original

    asyncio.run(scenario())


@pytest.mark.parametrize("expire", [False, True])
def test_sql_router_real_auth_all_channels_and_session_end(engine, tables, settings, expire):
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import sessionmaker
    from starlette.websockets import WebSocketDisconnect

    from honeyforge.auth.security import Security
    from honeyforge.common import utcnow
    from honeyforge.main import create_app
    from honeyforge.models import User, UserSession
    from honeyforge.telemetry.transport import SessionAccess, create_router

    token = "hf_session_stream-sql-test"
    sessions = sessionmaker(engine)
    with sessions() as session:
        user = User(username="stream-viewer", role="viewer", password_hash="unused")
        session.add(user)
        session.flush()
        session.add(
            UserSession(
                token_hash=Security(settings).digest(token),
                user_id=user.id,
                expires_at=utcnow() + timedelta(hours=1),
            )
        )
        session.commit()
    adapter, history, catchup = sql_services(engine, tables)
    app = create_app(settings, telemetry=False)
    app.include_router(
        create_router(
            history,
            catchup,
            LiveStreamService(adapter, history.presenter, poll_seconds=0.01),
            SessionAccess(sessions, settings),
        )
    )
    storage, publisher = EventStorage(engine, tables), OutboxPublisher(engine, tables)
    first = prepared(1, kind="auth.attempt")
    storage.store([first])
    publisher.publish_once()
    with TestClient(app, headers={"Origin": "http://localhost:5173"}) as client:
        client.cookies.set("hf_session", token)
        history_json = client.get("/api/v1/events").json()
        stream_json = client.get("/api/v1/stream-events").json()
        assert history_json["items"][0]["data"]["password"] == "••••"
        assert stream_json["items"][0]["event"] == history_json["items"][0]
        with client.websocket_connect("/api/v1/stream") as ws:
            assert ws.receive_json() == {"type": "hello", "watermark": "1"}
            storage.store([prepared(2, kind="auth.attempt")])
            publisher.publish_once()
            live = ws.receive_json()
            assert live["stream_seq"] == "2"
            assert live["event"]["data"]["password"] == "••••"
            with sessions() as session:
                saved = session.get(UserSession, Security(settings).digest(token))
                assert saved is not None
                if expire:
                    saved.expires_at = utcnow() - timedelta(seconds=1)
                else:
                    session.delete(saved)
                session.commit()
            storage.store([prepared(3)])
            publisher.publish_once()
            with pytest.raises(WebSocketDisconnect) as error:
                ws.receive_json()
            assert error.value.code == 4401
        assert client.get("/api/v1/events").status_code == 401
    assert adapter.watermark() == 3
    with engine.connect() as connection:
        originals = [
            bytes(value) for value in connection.scalars(select(tables.events.c.canonical_bytes))
        ]
    assert first.canonical_bytes in originals


def test_sql_output_matches_exact_frontend_wire(engine, tables, monkeypatch):
    from .test_wire_fixture import fixture

    monkeypatch.setattr("honeyforge.telemetry.storage.utcnow", lambda: NOW)
    base = 2**53 + 1
    with engine.begin() as connection:
        connection.execute(update(tables.state).values(last_seq=base - 1))
    storage = EventStorage(engine, tables)
    for value in [prepared(1), prepared(1, agent=2), prepared(3), prepared(4), prepared(5)]:
        storage.store([value])
    OutboxPublisher(engine, tables).publish_once()
    _, history, catchup = sql_services(engine, tables)
    actual = {
        "after": str(base - 1),
        "hello": {"type": "hello", "watermark": str(base + 2)},
        "pages": [
            catchup.page(str(base - 1), str(base + 2), 1),
            catchup.page(str(base), str(base + 2), 2),
        ],
        "live": {"type": "event", **catchup.page(str(base + 2), str(base + 3))["items"][0]},
        "reconnect_hello": {"type": "hello", "watermark": str(base + 4)},
        "reconnect_page": catchup.page(str(base + 3), str(base + 4)),
        "history": history.page(history.filters()),
    }
    assert actual == fixture()


def test_slow_sender_does_not_hold_sql_publisher(engine, tables):
    async def scenario():
        adapter, history, catchup = sql_services(engine, tables)
        started = asyncio.Event()

        async def slow(value):
            started.set()
            await asyncio.sleep(10)

        stream = LiveStreamService(adapter, history.presenter, send_seconds=0.2)
        task = asyncio.create_task(stream.run(slow, lambda: None))
        try:
            await asyncio.wait_for(started.wait(), 2)
            await asyncio.to_thread(EventStorage(engine, tables).store, [prepared(1)])
            result = await asyncio.wait_for(
                asyncio.to_thread(OutboxPublisher(engine, tables).publish_once), 2
            )
            assert result == 1
            with pytest.raises(TimeoutError):
                await task
            assert catchup.page()["items"][0]["stream_seq"] == "1"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
