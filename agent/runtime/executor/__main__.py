"""Polling центра: токен только из окружения, закрытый журнал и Docker LowDriver."""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
from pathlib import Path
from types import FrameType

from agent.runtime.drivers.containers import BackendError, DockerCli
from agent.runtime.drivers.low import LowSettings
from agent.runtime.executor.client import ClientError, HttpCommandClient
from agent.runtime.executor.engine import CommandExecutor, ExecutionError
from agent.runtime.executor.store import ExecutionStore, StoreError


async def _poll(args: argparse.Namespace, token: str) -> None:
    """Transient сеть повторяется с паузой; конфликты состояния требуют вмешательства."""
    client = HttpCommandClient(args.center, token, allow_http=args.allow_http_loopback)
    settings = LowSettings(
        image=args.image,
        bind_address=args.bind_address,
        network=args.network,
        journal_root=args.journal_root,
    )
    with ExecutionStore(args.state) as store:
        executor = CommandExecutor(client, store, DockerCli(), settings)
        while True:
            try:
                processed = await executor.run_once()
            except ClientError as error:
                if (
                    error.status is not None
                    and error.status < 500
                    and error.code not in ("LEASE_EXPIRED", "LEASE_MISMATCH")
                ):
                    raise
                if args.once:
                    raise
                print(f"Повтор обращения к центру: {error.code}", flush=True)
                processed = False
            if args.once:
                return
            await asyncio.sleep(0.1 if processed else 2.0)


async def run(args: argparse.Namespace, token: str) -> None:
    """SIGTERM отменяет polling, ожидая shield операции до освобождения store lock."""
    loop = asyncio.get_running_loop()
    polling = asyncio.current_task()
    assert polling is not None

    def terminate(_signum: int, _frame: FrameType | None) -> None:
        loop.call_soon_threadsafe(polling.cancel)

    previous = signal.signal(signal.SIGTERM, terminate)
    try:
        await _poll(args, token)
    finally:
        signal.signal(signal.SIGTERM, previous)


def main() -> None:
    """Аргументы не включают Bearer, конфигурацию профиля и произвольные команды."""
    parser = argparse.ArgumentParser(description="Исполнитель start/stop Low центра HoneyForge")
    parser.add_argument("--center", required=True)
    parser.add_argument(
        "--state", required=True, type=Path, help="Путь к SQLite на локальном диске"
    )
    parser.add_argument("--journal-root", required=True, type=Path)
    parser.add_argument("--image", default="honeyforge-low:local")
    parser.add_argument("--bind-address", default="127.0.0.1")
    parser.add_argument("--network")
    parser.add_argument("--allow-http-loopback", action="store_true")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    token = os.environ.get("HF_AGENT_TOKEN")
    if not token:
        parser.exit(1, "Задайте HF_AGENT_TOKEN в окружении процесса\n")
    try:
        asyncio.run(run(args, token))
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    except (ClientError, StoreError, ExecutionError, BackendError, ValueError, OSError) as error:
        # Не печатаем сообщение OSError/ValueError: оно может содержать путь или ввод.
        code = getattr(error, "code", "LOCAL_CONFIGURATION_ERROR")
        parser.exit(1, f"Исполнитель остановлен: {code}\n")


if __name__ == "__main__":
    main()
