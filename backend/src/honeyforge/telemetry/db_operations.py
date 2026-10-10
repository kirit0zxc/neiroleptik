"""Учтённые WS worker и серверный deadline SQL, включая проверку сессии."""

import asyncio
from collections.abc import Callable
from contextvars import ContextVar, copy_context
from threading import Event
from time import monotonic
from typing import Any, TypeVar

from sqlalchemy import Engine, event

T = TypeVar("T")


class OperationStopped(TimeoutError):
    """Остановленный вызов не должен начинать следующий SQL."""


class SQLDeadline:
    """Общий бюджет вызова; текущий SQL ограничен сервером одной секундой."""

    def __init__(self, seconds: float) -> None:
        self.until = monotonic() + seconds
        self.stopped = Event()

    def milliseconds(self) -> int:
        remaining = self.until - monotonic()
        if self.stopped.is_set() or remaining <= 0:
            raise OperationStopped("DB-операция остановлена")
        return max(1, min(1000, int(remaining * 1000)))


_current: ContextVar[SQLDeadline | None] = ContextVar("telemetry_sql_deadline", default=None)


def _before_sql(
    connection: Any,
    cursor: Any,
    statement: str,
    parameters: Any,
    context: Any,
    executemany: bool,
) -> None:
    """Без контекста WS hook не изменяет SQL других компонентов."""
    deadline = _current.get()
    if deadline is not None:
        if connection.dialect.name != "postgresql":
            raise ValueError("WS DB deadline требует PostgreSQL")
        cursor.execute(f"SET LOCAL statement_timeout = {deadline.milliseconds()}")


event.listen(Engine, "before_cursor_execute", _before_sql)


class DBOperations:
    """Отмена asyncio не отменяет владение синхронным worker до его завершения."""

    async def call(self, operation: Callable[..., T], *args: Any) -> T:
        deadline = SQLDeadline(3)

        def execute() -> T:
            token = _current.set(deadline)
            try:
                deadline.milliseconds()
                return operation(*args)
            finally:
                _current.reset(token)

        # Future сохраняет владение worker даже при отмене всех asyncio-задач shutdown.
        worker = asyncio.get_running_loop().run_in_executor(None, copy_context().run, execute)
        try:
            return await asyncio.wait_for(asyncio.shield(worker), 3)
        except (asyncio.CancelledError, TimeoutError):
            deadline.stopped.set()
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not worker.cancelled():
                worker.exception()
            raise
