"""Запускает настоящий процесс Low с файловым журналом и безвредным трафиком на loopback.

Нужен для получения образца журнала сборщиком телеметрии и для проверки «рабочего
запуска» без Docker: процесс тот же, что и в контейнере (`python -m honeypots.low`).
Трафик создаёт сам скрипт; это не данные реальных клиентов. Скрипт ничего не
устанавливает, не создаёт окружений и не слушает внешние интерфейсы.

    python -m infra.runtime.sample_low_journal --output <каталог>
"""

from __future__ import annotations

import argparse
import contextlib
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from uuid import uuid4

from honeypots.low.__main__ import READY_MARKER

REPO_ROOT = Path(__file__).resolve().parents[2]
LOOPBACK = "127.0.0.1"


def free_port() -> int:
    """Свободный порт на loopback; между проверкой и запуском возможна редкая гонка."""
    with socket.socket() as probe:
        probe.bind((LOOPBACK, 0))
        return int(probe.getsockname()[1])


class StopSignalError(RuntimeError):
    """Сигнал корректной остановки нельзя доставить (например, у процесса нет консоли)."""


@dataclass(frozen=True)
class LowLaunch:
    """Параметры запуска процесса Low."""

    journal_dir: Path
    instance_id: str
    deployment_id: str
    profile_revision: int = 1
    tcp_port: int = 0
    http_port: int = 0
    tcp_public_port: int = 2200
    http_public_port: int = 8800
    max_input_bytes: int = 2048
    banner: str = "SSH-2.0-OpenSSH_9.6"
    http_text: str = "Welcome"


class LowProcess:
    """Настоящий дочерний процесс Low: запуск, ожидание готовности, корректная остановка."""

    def __init__(self, launch: LowLaunch) -> None:
        self.launch = launch
        self._process: subprocess.Popen[bytes] | None = None
        self._stderr_lines: list[str] = []
        self._ready = threading.Event()
        self._pump: threading.Thread | None = None

    def __enter__(self) -> LowProcess:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._process is not None and self._process.poll() is None:
            self._process.kill()
            self._process.wait()

    def command(self) -> list[str]:
        """Аргументы запуска того же модуля, что и в контейнере; без shell."""
        launch = self.launch
        return [
            sys.executable,
            "-m",
            "honeypots.low",
            f"--host={LOOPBACK}",
            f"--tcp-port={launch.tcp_port}",
            f"--http-port={launch.http_port}",
            f"--tcp-public-port={launch.tcp_public_port}",
            f"--http-public-port={launch.http_public_port}",
            f"--max-input-bytes={launch.max_input_bytes}",
            f"--banner={launch.banner}",
            f"--http-body={launch.http_text}",
            f"--journal-dir={launch.journal_dir}",
            f"--instance-id={launch.instance_id}",
            f"--deployment-id={launch.deployment_id}",
            f"--profile-revision={launch.profile_revision}",
        ]

    def start(self, timeout: float = 15.0) -> None:
        """Запускает процесс и ждёт его сообщения о готовности.

        Готовность определяется по строке в stderr, а не пробным подключением: пробные
        соединения попали бы в журнал как события клиентов.
        """
        # Отдельная группа процессов нужна Windows, чтобы послать Ctrl+Break только ребёнку.
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        process = subprocess.Popen(
            self.command(),
            cwd=REPO_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=flags,
        )
        self._process = process
        self._pump = threading.Thread(target=self._read_stderr, args=(process,), daemon=True)
        self._pump.start()
        deadline = time.monotonic() + timeout
        while not self._ready.wait(0.05):
            if process.poll() is not None:
                self._pump.join(timeout=1.0)
                raise RuntimeError(f"Low завершился при запуске: {self.stderr_text()}")
            if time.monotonic() > deadline:
                process.kill()
                raise RuntimeError("Low не сообщил о готовности за отведённое время")

    def _read_stderr(self, process: subprocess.Popen[bytes]) -> None:
        """Читает служебные сообщения процесса построчно, пока канал не закроется."""
        assert process.stderr is not None
        for raw in iter(process.stderr.readline, b""):
            line = raw.decode("utf-8", errors="replace").rstrip()
            self._stderr_lines.append(line)
            if line.startswith(READY_MARKER):
                self._ready.set()

    def stop(self, timeout: float = 10.0) -> int:
        """Останавливает процесс корректным сигналом и возвращает код завершения.

        POSIX: SIGTERM. Windows: Ctrl+Break. Жёсткое завершение не используется: оно
        не даёт Low записать концовку журнала. Если сигнал доставить нельзя
        (нет консоли), поднимается RuntimeError.
        """
        process = self._require()
        try:
            if sys.platform == "win32":
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                process.terminate()
        except OSError as error:
            process.kill()
            raise StopSignalError(f"не удалось послать сигнал остановки: {error}") from error
        try:
            return process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            raise RuntimeError("Low не завершился после сигнала остановки") from None

    def stderr_text(self) -> str:
        """Служебные сообщения процесса; после остановки содержат все строки."""
        if self._pump is not None and not self._pump.is_alive():
            return "\n".join(self._stderr_lines)
        if self._process is not None and self._process.poll() is not None and self._pump:
            self._pump.join(timeout=1.0)
        return "\n".join(self._stderr_lines)

    def _require(self) -> subprocess.Popen[bytes]:
        if self._process is None:
            raise RuntimeError("процесс не запущен")
        return self._process


class SampleTraffic:
    """Безвредные запросы к Low: типичные и намеренно некорректные, только на loopback."""

    def __init__(self, tcp_port: int, http_port: int) -> None:
        self._tcp_port = tcp_port
        self._http_port = http_port

    def run(self) -> int:
        """Отправляет сценарий и возвращает число открытых соединений."""
        exchanges = [
            self._tcp(b"SSH-2.0-OpenSSH_9.9 sample-client\r\n"),
            self._tcp(bytes(range(0, 24))),
            self._http(b"GET / HTTP/1.1\r\nHost: sample.invalid\r\nUser-Agent: sample/1.0\r\n\r\n"),
            self._http(b"HEAD /robots.txt HTTP/1.1\r\nHost: sample.invalid\r\n\r\n"),
            self._http(
                b"POST /login HTTP/1.1\r\nHost: sample.invalid\r\nContent-Length: 0\r\n\r\n"
            ),
            self._http(b"GET / HTTP/1.1\r\nX-Oversized: " + b"a" * 4096 + b"\r\n\r\n"),
            self._http(b"GARBAGE\r\n\r\n"),
        ]
        return len(exchanges)

    def _tcp(self, data: bytes) -> bytes:
        with socket.create_connection((LOOPBACK, self._tcp_port), timeout=3) as connection:
            banner = connection.recv(256)
            connection.sendall(data)
            # Закрываем свою половину: иначе Low ждёт продолжения до таймаута чтения.
            connection.shutdown(socket.SHUT_WR)
            self._drain(connection)
            return banner

    def _http(self, request: bytes) -> bytes:
        with socket.create_connection((LOOPBACK, self._http_port), timeout=3) as connection:
            connection.sendall(request)
            return self._drain(connection)

    @staticmethod
    def _drain(connection: socket.socket) -> bytes:
        received = b""
        with contextlib.suppress(OSError):
            while chunk := connection.recv(4096):
                received += chunk
        return received


@dataclass(frozen=True)
class SampleRun:
    """Итог запуска: что было создано и как завершился процесс."""

    directory: Path
    instance_id: str
    deployment_id: str
    connections: int
    return_code: int
    stderr: str
    files: tuple[Path, ...]


def run_sample(output: Path) -> SampleRun:
    """Запускает Low, отправляет сценарий, останавливает процесс и собирает итог."""
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError("каталог результата должен быть пустым")
    launch = LowLaunch(
        journal_dir=output,
        instance_id=str(uuid4()),
        deployment_id=str(uuid4()),
        tcp_port=free_port(),
        http_port=free_port(),
    )
    with LowProcess(launch) as process:
        connections = SampleTraffic(launch.tcp_port, launch.http_port).run()
        code = process.stop()
        stderr = process.stderr_text()
    files = tuple(sorted(output.glob("low-*.jsonl")))
    return SampleRun(
        output, launch.instance_id, launch.deployment_id, connections, code, stderr, files
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Точка входа: запуск образца и печать итога; ненулевой код при сбое."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--output", type=Path, required=True, help="пустой каталог для журнала")
    args = parser.parse_args(argv)
    try:
        result = run_sample(args.output)
    except (RuntimeError, ValueError) as error:
        print(f"ошибка: {error}", file=sys.stderr)
        return 1
    print(f"Соединений отправлено: {result.connections}; код завершения Low: {result.return_code}")
    for path in result.files:
        print(f"{path} ({path.stat().st_size} байт)")
    return 0 if result.return_code == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
