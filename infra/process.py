"""Общий исполнитель диагностических команд с пределами времени и вывода."""

from __future__ import annotations

import math
import os
import signal
import subprocess
import sys
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class CommandResult:
    """Ограниченный вывод команды; UNKNOWN отличает неполное наблюдение от успеха."""

    returncode: int | None
    output: str = ""
    timed_out: bool = False
    truncated: bool = False
    error: str | None = None


class Runner(Protocol):
    """Исполнитель принимает только готовый список аргументов диагностической команды."""

    def run(self, command: Sequence[str]) -> CommandResult:
        """Выполняет команду и возвращает ограниченный результат; исключений не бросает."""
        ...


class BoundedRunner:
    """Ограничивает время и общий объём stdout/stderr, не используя shell."""

    def __init__(self, timeout: float = 5.0, output_limit: int = 8192) -> None:
        """Таймаут 0–30 с и предел вывода 1–64 КиБ проверяются сразу."""
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or not 0 < timeout <= 30
        ):
            raise ValueError("Таймаут должен быть больше 0 и не больше 30 секунд")
        if (
            isinstance(output_limit, bool)
            or not isinstance(output_limit, int)
            or not 1 <= output_limit <= 65536
        ):
            raise ValueError("Лимит вывода должен быть от 1 до 65536 байт")
        self.timeout = timeout
        self.output_limit = output_limit

    @staticmethod
    def _kill(process: subprocess.Popen[bytes]) -> None:
        """На POSIX завершает также потомков диагностической команды при отказе.

        Проверка идёт по sys.platform, а не по os.name: именно такое условие
        понимает mypy, поэтому типы проверяются одинаково на Windows и Linux.
        """
        try:
            if sys.platform == "win32":
                process.kill()
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def run(self, command: Sequence[str]) -> CommandResult:
        """Запускает процесс без shell и ждёт его не дольше таймаута.

        Вывод читается отдельным потоком и обрезается по лимиту; при превышении времени
        или объёма процесс (на POSIX — вся группа) завершается. Ошибки запуска
        возвращаются в поле error, а не исключением.
        """
        try:
            process = subprocess.Popen(
                list(command),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                shell=False,
                start_new_session=sys.platform != "win32",
            )
        except OSError as exc:
            return CommandResult(None, error=str(exc))

        captured = bytearray()
        exceeded = threading.Event()
        read_errors: list[str] = []
        stream = process.stdout
        assert stream is not None

        def read_output() -> None:
            # Читаем не более лимита плюс один байт: communicate() копит весь
            # вывод в памяти и не подходит для диагностики недоверенного хоста.
            try:
                while True:
                    chunk = stream.read(min(4096, self.output_limit + 1 - len(captured)))
                    if not chunk:
                        return
                    captured.extend(chunk)
                    if len(captured) > self.output_limit:
                        exceeded.set()
                        self._kill(process)
                        return
            except OSError as exc:
                read_errors.append(str(exc))
            finally:
                stream.close()

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        timed_out = False
        try:
            process.wait(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._kill(process)
            process.wait(timeout=1.0)
        reader.join(timeout=1.0)
        if reader.is_alive():
            # Потомок мог унаследовать канал вывода после выхода родителя.
            self._kill(process)
            reader.join(timeout=1.0)
            read_errors.append("Канал вывода не закрылся после завершения команды")
        return CommandResult(
            process.returncode,
            bytes(captured[: self.output_limit]).decode("utf-8", errors="replace"),
            timed_out,
            exceeded.is_set(),
            "; ".join(read_errors) or None,
        )
