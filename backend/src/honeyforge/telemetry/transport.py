"""Явно внедряемый router; рабочая фабрика FastAPI его не регистрирует."""

import asyncio
from typing import Any

import anyio
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from honeyforge.api.dependencies import Reader
from honeyforge.auth.service import AuthService
from honeyforge.common import DomainError
from honeyforge.config import Settings
from honeyforge.telemetry.history import CatchUpService, HistoryService
from honeyforge.telemetry.stream import LiveStreamService


class SessionAccess:
    """Каждая проверка использует новую короткую Session, включая простой WS."""

    def __init__(self, sessions: sessionmaker[Session], settings: Settings) -> None:
        self.sessions, self.settings = sessions, settings

    def check(self, token: str | None, origin: str | None) -> None:
        with self.sessions() as session:
            auth = AuthService(session, self.settings)
            auth.verify_origin(origin)
            auth.authenticate(token)


def create_router(
    history: HistoryService, catchup: CatchUpService, live: LiveStreamService, access: SessionAccess
) -> APIRouter:
    """Используется только явно собранным приложением после внедрения зависимостей."""
    router = APIRouter(prefix="/api/v1", tags=["telemetry"])

    @router.get("/events")
    def events(
        user: Reader,
        deployment_id: str | None = None,
        source_ip: str | None = None,
        type: str | None = None,
        start: str | None = Query(None, alias="from"),
        end: str | None = Query(None, alias="to"),
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        filters = history.filters(deployment_id, source_ip, type, start, end)
        return history.page(filters, cursor, limit)

    @router.get("/stream-events")
    def stream_events(
        user: Reader, after: str = "0", until: str | None = None, limit: int = 100
    ) -> dict[str, Any]:
        return catchup.page(after, until, limit)

    @router.websocket("/stream")
    async def stream(socket: WebSocket) -> None:
        await socket.accept()

        def authenticate() -> None:
            access.check(socket.cookies.get("hf_session"), socket.headers.get("origin"))

        async def receive_disconnect() -> None:
            while True:
                message = await socket.receive()
                if message["type"] == "websocket.disconnect":
                    return

        sender = asyncio.create_task(live.run(socket.send_json, authenticate))
        receiver = asyncio.create_task(receive_disconnect())
        try:
            done, _ = await asyncio.wait({sender, receiver}, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except DomainError as error:
            await socket.close(
                code=4401 if error.status == 401 else 4403 if error.status == 403 else 1013
            )
        except (TimeoutError, SQLAlchemyError):
            await socket.close(code=1013)
        except (WebSocketDisconnect, OSError):
            pass
        finally:
            for task in (sender, receiver):
                task.cancel()
            # ASGI cancel scope не должен прерывать возврат SQL-соединения.
            with anyio.CancelScope(shield=True):
                cleanup = asyncio.gather(sender, receiver, return_exceptions=True)
                cancelled = False
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        cancelled = True
                cleanup.result()
                if cancelled:
                    raise asyncio.CancelledError

    return router
