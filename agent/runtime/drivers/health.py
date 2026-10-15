"""Сетевые пробы драйверов: ожидание ответа ловушки и подтверждение освобождения портов.

Пробы идут с хоста на опубликованные порты и не отправляют ничего сверх минимума,
нужного протоколу: каждая проба порождает события в журнале ловушки. Общая часть
(повторы в пределах одного срока, параллельная проверка освобождения, безопасное
описание причины) одинакова для всех уровней; протокольная проба задаётся подклассом.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass


def check_probe_timings(source: object, names: tuple[str, ...]) -> None:
    """Таймеры драйвера должны быть конечными положительными числами.

    `NaN` и бесконечность отклоняются до запуска, иначе операция могла бы навсегда
    удерживать замок слота. Значение должно представляться конечным `float`, который
    используют таймеры asyncio: огромное целое отклоняется из-за переполнения.
    """
    for name in names:
        value = getattr(source, name)
        message = f"{name} должен быть конечным положительным числом"
        try:
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(message)
        except OverflowError as error:
            raise ValueError(message) from error


def probe_host(bind_address: str) -> str:
    """Пробы идут с хоста на bind-адрес; wildcard-адрес заменяется loopback."""
    address = ipaddress.ip_address(bind_address)
    if address.is_unspecified:
        return "127.0.0.1" if address.version == 4 else "::1"
    return bind_address


class HealthCheckFailed(Exception):
    """Ловушка не ответила ожидаемо за отведённое время; причина безопасна для показа."""


@dataclass(frozen=True)
class ReleaseTargets:
    """Порты хоста, освобождение которых подтверждает остановку слота.

    Wildcard-адрес публикации приводится к соответствующему loopback: проба идёт
    с самого хоста, и подключаться к `0.0.0.0` или `::` нельзя.
    """

    host: str
    ports: frozenset[int]

    def __post_init__(self) -> None:
        address = ipaddress.ip_address(self.host)
        if address.is_unspecified:
            object.__setattr__(self, "host", "127.0.0.1" if address.version == 4 else "::1")
        for port in self.ports:
            if type(port) is not int or not 1 <= port <= 65535:
                raise ValueError("порт должен быть целым от 1 до 65535")


class PortProbe[TargetsT](ABC):
    """Повторяет протокольную пробу до успеха или до общего срока.

    Подклассы реализуют `_probe_once` для своего протокола. Проверка здоровья
    выполняется при запуске, а не при периодическом наблюдении, чтобы опрос не
    засорял журнал ловушки (см. открытые вопросы в specs/runtime-drivers.md).
    """

    def __init__(self, timeout: float, interval: float, probe_timeout: float) -> None:
        self._timeout = timeout
        self._interval = interval
        self._probe_timeout = probe_timeout

    async def wait_until_healthy(self, targets: TargetsT) -> None:
        """Повторяет пробу до успеха или до срока; последняя причина попадает в ошибку.

        `ValueError` перехватывается наравне с сетевыми ошибками: `StreamReader.readline`
        поднимает его, если ответ превысил внутренний предел строки (64 КиБ) без
        перевода строки. Иначе ответ порта мог бы вывести исключение из драйвера — без
        аварийной очистки только что запущенного контейнера и без ACK центру.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._timeout
        last = "проверка не выполнялась"
        while True:
            try:
                await self._probe_once(targets, deadline)
                return
            except (OSError, EOFError, TimeoutError, ValueError, HealthCheckFailed) as error:
                last = self._describe(error)
            if loop.time() >= deadline:
                raise HealthCheckFailed(last)
            await asyncio.sleep(min(self._interval, max(deadline - loop.time(), 0.0)))

    @abstractmethod
    async def _probe_once(self, targets: TargetsT, deadline: float) -> None:
        """Одна проба протокола; расхождение с профилем — HealthCheckFailed."""

    async def wait_for_release(self, targets: ReleaseTargets) -> bool:
        """Ждёт явного отказа TCP-подключения ко всем портам за один общий срок.

        Таймаут и другие ошибки сети не доказывают освобождение. Порты проверяются
        параллельно; probe_timeout не прерывает медленный отказ Windows loopback.
        Никакие данные или команды остановки чужому процессу не отправляются.
        """
        if not targets.ports:
            return True
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._timeout
        while loop.time() < deadline:
            try:
                async with asyncio.timeout_at(deadline):
                    released = await asyncio.gather(
                        *(self._connection_refused(targets.host, port) for port in targets.ports)
                    )
            except TimeoutError:
                return False
            if all(released):
                return loop.time() <= deadline
            await asyncio.sleep(min(self._interval, max(deadline - loop.time(), 0.0)))
        return False

    async def _connection_refused(self, host: str, port: int) -> bool:
        """Отказ connect подтверждает отсутствие слушателя; успешный connect — нет."""
        try:
            _, writer = await asyncio.open_connection(host, port)
        except ConnectionRefusedError:
            return True
        except OSError:
            return False
        await self._close(writer)
        return False

    def _budget(self, deadline: float) -> float:
        """Время на одну пробу: не больше probe_timeout и не дальше общего срока."""
        remaining = deadline - asyncio.get_running_loop().time()
        return min(self._probe_timeout, max(remaining, 0.05))

    @staticmethod
    def _describe(error: Exception) -> str:
        """Краткая безопасная причина; тексты системных ошибок зависят от ОС и языка."""
        if isinstance(error, HealthCheckFailed):
            return str(error)
        if isinstance(error, TimeoutError):
            return "нет ответа в срок"
        if isinstance(error, ConnectionRefusedError):
            return "порт не принимает соединения"
        if isinstance(error, EOFError):
            return "соединение закрыто до ответа"
        if isinstance(error, ValueError):
            # Текст ValueError из asyncio содержит только предел буфера, не данные порта.
            return "ответ длиннее предела строки"
        return f"ошибка сети ({type(error).__name__})"

    @staticmethod
    async def _close(writer: asyncio.StreamWriter) -> None:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()
