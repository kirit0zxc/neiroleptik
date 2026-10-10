"""Общий lifecycle драйверов; отмена вызывающего не освобождает работающий слот."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Awaitable
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from agent.runtime.contract import DeploymentSpec


class DriverState(StrEnum):
    """Фактическое состояние ресурса, отдельно от состояния команды центра."""

    STOPPED = "stopped"
    RUNNING = "running"
    FAILED = "failed"


class DriverErrorCode(StrEnum):
    """Безопасные коды ошибок lifecycle."""

    UNSUPPORTED_PROFILE = "UNSUPPORTED_PROFILE"
    INVALID_STATE = "INVALID_STATE"
    BACKEND_UNAVAILABLE = "BACKEND_UNAVAILABLE"
    START_FAILED = "START_FAILED"
    HEALTHCHECK_FAILED = "HEALTHCHECK_FAILED"
    CLEANUP_FAILED = "CLEANUP_FAILED"


class DriverError(Exception):
    """Ошибка не содержит сырых секретов или вывода внешнего процесса."""

    def __init__(self, code: DriverErrorCode, message: str = "") -> None:
        self.code = code
        self.message = message
        super().__init__(message or code.value)


@dataclass(frozen=True)
class DriverStatus:
    """Снимок ресурса; положительное здоровье возможно только для RUNNING."""

    state: DriverState
    healthy: bool = False
    instance_id: str | None = None
    revision: int | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if self.healthy and self.state != DriverState.RUNNING:
            raise ValueError("healthy требует RUNNING")
        if self.instance_id is not None:
            object.__setattr__(self, "instance_id", str(UUID(self.instance_id)))


class Driver(ABC):
    """Сериализация сохраняется до фактического завершения операции backend."""

    def __init__(self) -> None:
        self._operation_lock = asyncio.Lock()

    async def _serialized(self, operation: Awaitable[DriverStatus]) -> DriverStatus:
        async def execute() -> DriverStatus:
            async with self._operation_lock:
                return await operation

        task = asyncio.create_task(execute())
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Внешняя отмена не прерывает Docker и не разрешает следующую мутацию слота.
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not task.cancelled():
                task.exception()
            raise

    async def start(self, deployment: DeploymentSpec) -> DriverStatus:
        """Запускает слот после завершения предыдущей операции."""
        return await self._serialized(self._start(deployment))

    async def stop(self) -> DriverStatus:
        """Останавливает слот, включая повтор остановки."""
        return await self._serialized(self._stop())

    async def apply_config(self, deployment: DeploymentSpec) -> DriverStatus:
        """Применяет проверенную ревизию под общим замком."""
        return await self._serialized(self._apply_config(deployment))

    async def inspect(self) -> DriverStatus:
        """Наблюдает ресурс после завершения мутаций."""
        return await self._serialized(self._inspect())

    async def reset(self) -> DriverStatus:
        """Пересоздаёт слот с новым экземпляром."""
        return await self._serialized(self._reset())

    @abstractmethod
    async def _start(self, deployment: DeploymentSpec) -> DriverStatus: ...
    @abstractmethod
    async def _stop(self) -> DriverStatus: ...
    @abstractmethod
    async def _apply_config(self, deployment: DeploymentSpec) -> DriverStatus: ...
    @abstractmethod
    async def _inspect(self) -> DriverStatus: ...
    @abstractmethod
    async def _reset(self) -> DriverStatus: ...
