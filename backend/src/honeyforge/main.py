"""Сборка HTTP-приложения; миграции запускаются отдельно перед запуском."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from honeyforge.api import agent, auth, control
from honeyforge.api.schemas import ErrorResponse
from honeyforge.common import DomainError
from honeyforge.config import Settings, get_settings


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Секретные ответы не кэшируются; request body не копируется в ошибки."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response


def create_app(settings: Settings | None = None) -> FastAPI:
    """Factory позволяет тестировать приложение с явной конфигурацией."""
    configuration = settings or get_settings()
    engine = create_engine(
        configuration.database_url.get_secret_value(),
        pool_pre_ping=True,
        connect_args={"connect_timeout": 5},
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        yield
        engine.dispose()

    app = FastAPI(
        title="HoneyForge Control API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if configuration.environment != "production" else None,
        redoc_url=None,
    )
    app.state.engine = engine
    if settings is not None:
        app.dependency_overrides[get_settings] = lambda: configuration
    app.add_middleware(
        CORSMiddleware,
        allow_origins=configuration.allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Content-Type", "X-CSRF-Token", "Authorization"],
    )
    app.add_middleware(SecurityHeadersMiddleware)

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, error: DomainError) -> JSONResponse:
        return JSONResponse(status_code=error.status, content={"detail": error.detail})

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
        # Pydantic input может содержать пароль или весь payload; наружу идут только loc/type/msg.
        errors: list[dict[str, Any]] = [
            {"loc": list(item["loc"]), "type": item["type"], "msg": item["msg"]}
            for item in error.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": errors})

    @app.get("/healthz", tags=["health"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", tags=["health"])
    def ready() -> JSONResponse:
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1 FROM users LIMIT 1"))
            return JSONResponse({"status": "ready"})
        except SQLAlchemyError:
            return JSONResponse({"status": "unavailable"}, status_code=503)

    # Одинаковый detail применяется и к бизнес-422, и к списку ошибок полей.
    errors: dict[int | str, dict[str, Any]] = {
        status: {"model": ErrorResponse} for status in (401, 403, 404, 409, 422, 429)
    }
    app.include_router(auth.router, responses=errors)
    app.include_router(control.router, responses=errors)
    app.include_router(agent.router, responses=errors)
    return app
