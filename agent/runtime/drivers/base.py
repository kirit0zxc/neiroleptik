"""Общий контракт драйверов ловушек: операции жизненного цикла, статус и ошибки.

Драйвер управляет одним слотом (одним назначением центра). Он не получает токен центра,
не обращается к центру и не исполняет произвольные команды из профиля: все операции
принимают только типизированные объекты.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar
from uuid import UUID

from agent.runtime.contract import DeploymentSpec, Level


class DriverState(StrEnum):
    """Состояние слота: остановлен, работает или отказал (вход закрыт)."""

    STOPPED = "stopped"
    RUNNING = "running"
    FAILED = "failed"


class DriverErrorCode(StrEnum):
    """Коды ожидаемых отказов драйвера для локальной обработки Reconciler."""

    UNSUPPORTED_PROFILE = "UNSUPPORTED_PROFILE"
    INVALID_STATE = "INVALID_STATE"
    BACKEND_UNAVAILABLE = "BACKEND_UNAVAILABLE"
    START_FAILED = "START_FAILED"
    HEALTHCHECK_FAILED = "HEALTHCHECK_FAILED"
    CLEANUP_FAILED = "CLEANUP_FAILED"


class DriverError(Exception):
    """Ожидаемый отказ драйвера. Сообщение безопасно: без секретов и сырого вывода команд."""

    def __init__(self, code: DriverErrorCode, message: str = "") -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code.value}: {message}" if message else code.value)


@dataclass(frozen=True)
class DriverStatus:
    """Наблюдаемое состояние слота.

    healthy=True допустимо только при state=RUNNING и означает, что проверка здоровья
    этого экземпляра пройдена самим драйвером. instance_id — последний успешно
    созданный экземпляр (None, пока их не было); revision — применённая ревизия профиля.
    """

    state: DriverState
    healthy: bool = False
    instance_id: str | None = None
    revision: int | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if self.healthy and self.state is not DriverState.RUNNING:
            raise ValueError("healthy возможно только в состоянии RUNNING")
        if self.instance_id is not None:
            UUID(self.instance_id)


class Driver(ABC):
    """Шаблонный метод: публичные операции сериализуются, подклассы реализуют _операции.

    Параллельный вызов ждёт завершения предыдущего, а не выполняется одновременно: так
    не возникает гонки между, например, reset и inspect одного слота.
    """

    level: ClassVar[Level]

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    async def start(self, deployment: DeploymentSpec) -> DriverStatus:
        """Запускает ловушку по профилю; допустимо только из STOPPED. Новый instance_id."""
        async with self._lock:
            return await self._start(deployment)

    async def stop(self) -> DriverStatus:
        """Останавливает и удаляет ресурсы слота; повторный вызов безопасен."""
        async with self._lock:
            return await self._stop()

    async def apply_config(self, deployment: DeploymentSpec) -> DriverStatus:
        """Применяет другую ревизию, пересоздавая экземпляр. Новый instance_id."""
        async with self._lock:
            return await self._apply_config(deployment)

    async def inspect(self) -> DriverStatus:
        """Только наблюдение: ресурсы не меняются, сетевые пробы ловушки не выполняются."""
        async with self._lock:
            return await self._inspect()

    async def reset(self) -> DriverStatus:
        """Возвращает чистое состояние прежним профилем, пересоздавая экземпляр."""
        async with self._lock:
            return await self._reset()

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
