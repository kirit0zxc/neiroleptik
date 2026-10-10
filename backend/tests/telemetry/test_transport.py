"""Изолированный router: отказ настоящей auth не требует SQL-запроса без cookie."""

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker
from starlette.websockets import WebSocketDisconnect

from honeyforge.config import Settings
from honeyforge.main import create_app
from honeyforge.telemetry.stream import LiveStreamService
from honeyforge.telemetry.transport import SessionAccess, create_router

from .test_read_services import services


def application():
    settings = Settings(
        database_url="postgresql+psycopg://test:test@localhost/honeyforge_test",
        session_secret="x" * 32,
        credential_key=Fernet.generate_key().decode(),
        environment="test",
        cookie_secure=False,
    )
    app = create_app(settings)
    reader, history, catchup = services([])
    router = create_router(
        history,
        catchup,
        LiveStreamService(reader, history.presenter),
        SessionAccess(sessionmaker(app.state.engine), settings),
    )
    app.include_router(router)
    return app


def test_auth_denials_and_isolated_openapi() -> None:
    app = application()
    paths = app.openapi()["paths"]
    assert "/api/v1/events" in paths
    assert "/api/v1/stream-events" in paths
    with TestClient(app) as client:
        for path in ["events", "stream-events"]:
            response = client.get("/api/v1/" + path)
            assert response.status_code == 401
            assert set(response.json()) == {"detail"}
        for origin, code in [("http://localhost:5173", 4401), ("https://untrusted.test", 4403)]:
            with client.websocket_connect("/api/v1/stream", headers={"origin": origin}) as ws:
                try:
                    ws.receive_json()
                except WebSocketDisconnect as error:
                    assert error.code == code
                else:
                    raise AssertionError("Отказ авторизации не должен отправлять данные")
