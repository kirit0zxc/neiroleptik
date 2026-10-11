"""Auth интеграция изолированного router с настоящей PostgreSQL, без storage claims."""

import os
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from dotenv import dotenv_values
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from starlette.websockets import WebSocketDisconnect

from honeyforge.auth.security import Security
from honeyforge.common import utcnow
from honeyforge.config import Settings
from honeyforge.main import create_app
from honeyforge.models import User, UserSession
from honeyforge.telemetry.stream import LiveStreamService
from honeyforge.telemetry.transport import SessionAccess, create_router

from .test_read_services import record, services


@pytest.fixture
def pg_client() -> Iterator[tuple[TestClient, sessionmaker[Session], str]]:
    root = Path(__file__).resolve().parents[3]
    url = os.environ.get("HF_TEST_DATABASE_URL") or dotenv_values(root / ".env").get(
        "HF_TEST_DATABASE_URL"
    )
    if not url:
        pytest.skip("Нет HF_TEST_DATABASE_URL: PostgreSQL auth интеграция не проверена")
    if make_url(url).database != "honeyforge_test":
        pytest.fail("Разрешена только honeyforge_test")
    schema = "stream_auth_" + uuid4().hex
    engine = create_engine(url, connect_args={"connect_timeout": 5})
    scoped = create_engine(
        url, connect_args={"connect_timeout": 5, "options": f"-csearch_path={schema}"}
    )
    settings = Settings(
        database_url=url,
        session_secret="x" * 32,
        credential_key=Fernet.generate_key().decode(),
        environment="test",
        cookie_secure=False,
    )
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    try:
        User.metadata.create_all(scoped, tables=[User.__table__, UserSession.__table__])
        sessions = sessionmaker(scoped)
        token = "hf_session_test-only-stream"
        with sessions() as session:
            user = User(username="viewer", role="viewer", password_hash="unused")
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
        app = create_app(settings, telemetry=False)
        app.state.engine.dispose()
        app.state.engine = scoped
        reader, history, catchup = services([record(1)])
        app.include_router(
            create_router(
                history,
                catchup,
                LiveStreamService(reader, history.presenter, poll_seconds=0.01),
                SessionAccess(sessions, settings),
            )
        )
        with TestClient(app, headers={"Origin": "http://localhost:5173"}) as client:
            client.cookies.set("hf_session", token)
            yield client, sessions, token
    finally:
        scoped.dispose()
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()


def test_reader_real_session_and_revocation(pg_client) -> None:
    client, sessions, token = pg_client
    assert client.get("/api/v1/events").status_code == 200
    assert client.get("/api/v1/stream-events").status_code == 200
    with client.websocket_connect("/api/v1/stream") as ws:
        assert ws.receive_json() == {"type": "hello", "watermark": "1"}
        with sessions() as session:
            session.execute(delete(UserSession))
            session.commit()
        with pytest.raises(WebSocketDisconnect) as error:
            ws.receive_json()
        assert error.value.code == 4401
    assert client.get("/api/v1/events").status_code == 401


def test_expired_session(pg_client) -> None:
    client, sessions, token = pg_client
    with sessions() as session:
        saved = session.query(UserSession).one()
        assert saved is not None
        saved.expires_at = utcnow() - timedelta(seconds=1)
        session.commit()
    assert client.get("/api/v1/events").status_code == 401
    with client.websocket_connect("/api/v1/stream") as ws:
        with pytest.raises(WebSocketDisconnect) as error:
            ws.receive_json()
        assert error.value.code == 4401
