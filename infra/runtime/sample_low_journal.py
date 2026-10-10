"""Настоящий процесс Low и семь loopback-соединений; без Docker и внешней сети."""

from __future__ import annotations

import argparse
import json
import re
import signal
import socket
import subprocess
import sys
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4


class StopSignalError(RuntimeError):
    """Среда не позволяет отправить дочернему процессу мягкий сигнал остановки."""


@dataclass(frozen=True)
class SampleResult:
    """Результат реального запуска; файлы содержат только синтетический трафик."""

    return_code: int
    stderr: str
    files: tuple[Path, ...]
    instance_id: str
    deployment_id: str
    connections: int = 7


class LowSample:
    """Владеет только собственным дочерним процессом, никогда не меняет чужие сервисы."""

    @staticmethod
    def _exchange(port: int, payload: bytes, *, tcp: bool = False) -> None:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
            if tcp:
                client.recv(512)
            client.sendall(payload)
            client.shutdown(socket.SHUT_WR)
            try:
                while client.recv(4096):
                    pass
            except ConnectionResetError:
                pass

    def run(self, output: Path, *, rotate: bool = False) -> SampleResult:
        """Отказывает для занятого каталога и дожидается штатного завершения Low."""
        output = Path(output).resolve()
        if output.exists() and (not output.is_dir() or any(output.iterdir())):
            raise ValueError("Нужен пустой каталог результата")
        output.mkdir(parents=True, exist_ok=True)
        instance = str(uuid4())
        deployment = str(uuid4())
        command = [
            sys.executable,
            "-X",
            "utf8",
            "-m",
            "honeypots.low",
            "--tcp-port=0",
            "--http-port=0",
            "--tcp-public-port=2200",
            "--http-public-port=8800",
            "--max-input-bytes=2048",
            f"--journal-dir={output}",
            f"--instance-id={instance}",
            f"--deployment-id={deployment}",
            "--profile-revision=1",
        ]
        if rotate:
            command.extend(["--journal-max-bytes=262144", "--journal-quota-bytes=2097152"])
        creation_flags = 0
        if sys.platform == "win32":
            creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=creation_flags,
        )
        ready = threading.Event()
        ports: list[int] = []
        lines: list[str] = []
        assert process.stderr is not None

        def read() -> None:
            assert process.stderr is not None
            for line in process.stderr:
                text = line.decode("utf8", errors="replace")
                lines.append(text)
                match = re.search(r"honeyforge-low: запущен, tcp=(\d+), http=(\d+)", text)
                if match:
                    ports.extend(map(int, match.groups()))
                    ready.set()
            ready.set()

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        try:
            if not ready.wait(10) or len(ports) != 2:
                raise RuntimeError("Процесс Low не подтвердил готовность")
            tcp, http = ports
            self._exchange(tcp, b"SSH-2.0-TestClient\r\n", tcp=True)
            self._exchange(tcp, b"hello\n", tcp=True)
            self._exchange(http, b"GET / HTTP/1.1\r\nHost: sample\r\n\r\n")
            self._exchange(http, b"POST /login HTTP/1.1\r\nHost: sample\r\n\r\n")
            self._exchange(http, b"HEAD / HTTP/1.0\r\n\r\n")
            self._exchange(http, b"GET /probe HTTP/1.0\r\n\r\n")
            self._exchange(http, b"GET / HTTP/1.0\r\nX-Payload: " + b"x" * 2500)
            if rotate:
                # Синтетические усечённые запросы заполняют минимальный файл журнала;
                # следующий header получает новый journal_id и больший journal_seq.
                for _ in range(160):
                    self._exchange(http, b"POST /rotation HTTP/1.0\r\nX-Payload: " + b"r" * 2500)
            try:
                if sys.platform == "win32":
                    process.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    process.send_signal(signal.SIGTERM)
                process.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired) as error:
                if sys.platform == "win32":
                    raise StopSignalError("Ctrl+Break недоступен в этой среде") from error
                raise RuntimeError("Low не завершился по SIGTERM") from error
            reader.join(timeout=2)
            result = SampleResult(
                process.returncode,
                "".join(lines),
                tuple(sorted(output.glob("low-*.jsonl"))),
                instance,
                deployment,
                connections=167 if rotate else 7,
            )
            if result.return_code != 0:
                raise RuntimeError("Low завершился с ошибкой")
            return result
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            reader.join(timeout=2)
            process.stderr.close()


def run_sample(output: Path, *, rotate: bool = False) -> SampleResult:
    """Точка входа для локального smoke и тестов."""
    return LowSample().run(output, rotate=rotate)


def main() -> None:
    """Создаёт образец только в явно указанном каталоге."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rotate", action="store_true", help="Создать реальные файлы ротации")
    args = parser.parse_args()
    try:
        result = run_sample(args.output, rotate=args.rotate)
    except (ValueError, RuntimeError) as error:
        parser.exit(1, str(error) + "\n")
    print(json.dumps(asdict(result), default=str, ensure_ascii=False))


if __name__ == "__main__":
    main()
