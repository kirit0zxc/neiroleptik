"""Регрессии отмены активного SQL и переполнения UTC в истории."""

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from threading import Event
from time import monotonic
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from honeyforge.auth.security import Security
from honeyforge.common import DomainError, utcnow
from honeyforge.main import create_app
from honeyforge.models import User, UserSession
from honeyforge.telemetry.history import HistoryService
from honeyforge.telemetry.read_adapter import CommittedReadAdapter
from honeyforge.telemetry.reader import PublicationReader
from honeyforge.telemetry.stream import LiveStreamService
from honeyforge.telemetry.transport import SessionAccess, create_router

from .test_auth_postgres import pg_client as pg_client
from .test_stream_postgres import sql_services


class LockedDatabase:
    """Один reader connection; блокировка снимается только страховочной очисткой."""

    def __init__(self, engine, tables, settings):
        self.owner, self.tables = engine, tables
        self.name = "stream_cancel_" + uuid4().hex
        self.reads = create_engine(
            engine.url,
            pool_size=1,
            max_overflow=0,
            pool_timeout=1,
            connect_args={
                "connect_timeout": 1,
                "options": f"-csearch_path={tables.metadata.schema} -cstatement_timeout=10000",
                "application_name": self.name,
            },
        )
        with engine.begin() as connection:
            scoped = connection.execution_options(
                schema_translate_map={None: tables.metadata.schema}
            )
            User.metadata.create_all(scoped, tables=[User.__table__, UserSession.__table__])
        self.sessions = sessionmaker(self.reads)
        self.token = "hf_session_cancel-regression"
        with self.sessions() as session:
            user = User(username="cancel-viewer", role="viewer", password_hash="unused")
            session.add(user)
            session.flush()
            session.add(
                UserSession(
                    token_hash=Security(settings).digest(self.token),
                    user_id=user.id,
                    expires_at=utcnow() + timedelta(hours=1),
                )
            )
            session.commit()
        self.finished = Event()
        finished = self.finished

        class ObservedReader(CommittedReadAdapter):
            def watermark(self):
                try:
                    return super().watermark()
                finally:
                    finished.set()

        self.reader = ObservedReader(PublicationReader(self.reads, tables))
        _, self.history, self.catchup = sql_services(self.reads, tables)
        self.auth_finished = Event()
        auth_finished = self.auth_finished

        class ObservedAccess(SessionAccess):
            def check(self, token, origin):
                try:
                    return super().check(token, origin)
                finally:
                    auth_finished.set()

        self.access = ObservedAccess(self.sessions, settings)
        self.live = LiveStreamService(self.reader, self.history.presenter)
        self.blocker = engine.connect()
        self.transaction = self.blocker.begin()

    def block(self, table):
        if table == "user_sessions":
            self.finished = self.auth_finished
        self.blocker.execute(
            text(f'LOCK TABLE "{self.tables.metadata.schema}"."{table}" IN ACCESS EXCLUSIVE MODE')
        )

    def authenticate(self):
        try:
            self.access.check(self.token, "http://localhost:5173")
        finally:
            self.finished.set()

    def wait_for_lock(self):
        """pg_stat_activity доказывает ожидание SQL, а не только запуск worker."""
        deadline = monotonic() + 5
        with self.owner.connect() as connection:
            while monotonic() < deadline:
                waiting = connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE application_name=:name AND wait_event_type='Lock'"
                    ),
                    {"name": self.name},
                )
                connection.rollback()
                if waiting:
                    return True
                Event().wait(0.01)
        return False

    def assert_released(self):
        assert self.transaction.is_active, "Тест не должен разблокировать SQL до проверки"
        assert self.finished.is_set(), "Worker ещё выполняет DB-операцию"
        assert self.reads.pool.checkedout() == 0, "WS удерживает соединение"
        with self.owner.connect() as connection:
            assert (
                connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE application_name=:name AND state='active'"
                    ),
                    {"name": self.name},
                )
                == 0
            )

    def close(self):
        self.transaction.rollback()
        self.blocker.close()
        self.reads.dispose()


@pytest.fixture
def locked_db(engine, tables, settings) -> Iterator[LockedDatabase]:
    database = LockedDatabase(engine, tables, settings)
    try:
        yield database
    finally:
        database.close()


@pytest.mark.parametrize("operation", ["read", "auth"])
@pytest.mark.parametrize("repeated_cancel", [False, True])
def test_cancel_waits_for_sql_and_connection_before_unlock(locked_db, operation, repeated_cancel):
    async def scenario():
        locked_db.block("publications" if operation == "read" else "user_sessions")

        async def send(payload):
            pytest.fail("Заблокированный первоначальный SQL не должен отправлять hello")

        auth = (lambda: None) if operation == "read" else locked_db.authenticate
        task = asyncio.create_task(locked_db.live.run(send, auth))
        timer = None
        try:
            assert await asyncio.to_thread(locked_db.wait_for_lock)
            started = monotonic()
            task.cancel()
            if repeated_cancel:
                timer = asyncio.get_running_loop().call_later(0.05, task.cancel)
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
            assert monotonic() - started < 2
            locked_db.assert_released()
            assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        finally:
            if timer:
                timer.cancel()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_transport_disconnect_waits_for_blocked_read(locked_db, settings):
    app = create_app(settings)
    app.state.engine.dispose()
    app.state.engine = locked_db.reads
    app.include_router(
        create_router(locked_db.history, locked_db.catchup, locked_db.live, locked_db.access)
    )
    locked_db.block("publications")
    with TestClient(app, headers={"Origin": "http://localhost:5173"}) as client:
        client.cookies.set("hf_session", locked_db.token)
        with client.websocket_connect("/api/v1/stream"):
            assert locked_db.wait_for_lock()
            started = monotonic()
        assert monotonic() - started < 2
        locked_db.assert_released()


@pytest.mark.parametrize("field", ["start", "end"])
@pytest.mark.parametrize(
    "value",
    [
        "0001-01-01T00:00:00+01:00",
        "9999-12-31T23:59:59-01:00",
    ],
)
def test_out_of_range_utc_filters_are_safe_422(field, value):
    with pytest.raises(DomainError) as error:
        HistoryService.filters(**{field: value})
    assert error.value.status == 422
    assert error.value.detail == "Некорректные фильтры истории"
    assert error.value.__cause__ is None


@pytest.mark.parametrize("field", ["from", "to"])
@pytest.mark.parametrize(
    "value",
    [
        "0001-01-01T00:00:00+01:00",
        "9999-12-31T23:59:59-01:00",
    ],
)
def test_out_of_range_dates_http_422_without_internal_data(pg_client, field, value):
    client, _, _ = pg_client
    response = client.get("/api/v1/events", params={field: value})
    assert response.status_code == 422
    assert response.json() == {"detail": "Некорректные фильтры истории"}


def test_valid_dates_still_normalize_to_utc():
    filters = HistoryService.filters(
        start="0001-01-01T01:00:00+01:00", end="9999-12-31T22:59:59-01:00"
    )
    assert filters.start == datetime(1, 1, 1, tzinfo=UTC)
    assert filters.end == datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC)


@pytest.mark.parametrize("operation", ["read", "auth"])
def test_transport_shutdown_and_repeated_cancel_wait_for_sql(locked_db, settings, operation):
    async def scenario():
        app = create_app(settings)
        app.state.engine.dispose()
        app.state.engine = locked_db.reads
        app.include_router(
            create_router(locked_db.history, locked_db.catchup, locked_db.live, locked_db.access)
        )
        locked_db.block("publications" if operation == "read" else "user_sessions")
        incoming = asyncio.Queue()
        incoming.put_nowait({"type": "websocket.connect"})

        async def send(message):
            assert message["type"] == "websocket.accept"

        scope = {
            "type": "websocket",
            "asgi": {"version": "3.0"},
            "scheme": "ws",
            "path": "/api/v1/stream",
            "raw_path": b"/api/v1/stream",
            "query_string": b"",
            "root_path": "",
            "http_version": "1.1",
            "headers": [
                (b"origin", b"http://localhost:5173"),
                (b"cookie", f"hf_session={locked_db.token}".encode()),
            ],
            "client": ("testclient", 123),
            "server": ("testserver", 80),
            "subprotocols": [],
        }
        task = asyncio.create_task(app(scope, incoming.get, send))
        timer = None
        try:
            assert await asyncio.to_thread(locked_db.wait_for_lock)
            started = monotonic()
            # Shutdown отменяет и endpoint, и его дочерние asyncio-задачи.
            for pending in asyncio.all_tasks():
                if pending is not asyncio.current_task():
                    pending.cancel()
            timer = asyncio.get_running_loop().call_later(0.05, task.cancel)
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
            assert monotonic() - started < 2
            locked_db.assert_released()
            assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        finally:
            if timer:
                timer.cancel()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["read", "auth"])
def test_server_deadline_stops_sql_without_disconnect(locked_db, operation):
    from sqlalchemy.exc import SQLAlchemyError

    async def scenario():
        locked_db.block("publications" if operation == "read" else "user_sessions")

        async def send(payload):
            pytest.fail("Заблокированный SQL не завершился успешно")

        auth = (lambda: None) if operation == "read" else locked_db.authenticate
        task = asyncio.create_task(locked_db.live.run(send, auth))
        try:
            assert await asyncio.to_thread(locked_db.wait_for_lock)
            with pytest.raises(SQLAlchemyError):
                await asyncio.wait_for(task, 2)
            locked_db.assert_released()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_sql_deadline_does_not_leak_to_other_operations(locked_db):
    from honeyforge.telemetry.db_operations import DBOperations

    def timeout():
        with locked_db.reads.connect() as connection:
            return connection.scalar(text("SHOW statement_timeout"))

    assert timeout() == "10s"
    assert asyncio.run(DBOperations().call(timeout)) == "1s"
    assert timeout() == "10s"
    assert locked_db.reads.pool.checkedout() == 0
