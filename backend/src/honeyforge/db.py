"""Создание подключений и транзакций без неявного изменения схемы БД."""

from collections.abc import Iterator
from functools import lru_cache
from typing import cast

from fastapi import Request
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session

from honeyforge.config import get_settings


class Base(DeclarativeBase):
    """Общая метаинформация моделей для Alembic."""


@lru_cache
def get_engine() -> Engine:
    """Пул проверяет соединение перед выдачей после рестарта PostgreSQL."""
    return create_engine(
        get_settings().database_url.get_secret_value(),
        pool_pre_ping=True,
        connect_args={"connect_timeout": 5},
    )


def get_session(request: Request) -> Iterator[Session]:
    """Незавершённая транзакция откатывается при выходе, commit делает сервис."""
    with Session(cast(Engine, request.app.state.engine), expire_on_commit=False) as session:
        yield session
