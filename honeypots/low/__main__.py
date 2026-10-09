"""Запуск локальной приманки; внешний bind требуется указать явно."""

import argparse
import asyncio
import signal
import sys

from honeypots.low import JsonLineEventSink, LowConfig, LowService


async def _run(config: LowConfig) -> None:
    """Завершает активные подключения при остановке процесса через поддерживаемые сигналы."""
    service = LowService(config, JsonLineEventSink(sys.stdout))
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    handlers: list[signal.Signals] = []
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stopping.set)
        except NotImplementedError:
            continue
        handlers.append(signum)
    try:
        await service.start()
        await stopping.wait()
    finally:
        await service.stop()
        for signum in handlers:
            loop.remove_signal_handler(signum)


def main() -> None:
    """Преобразует только разрешённые аргументы CLI в проверяемую конфигурацию."""
    parser = argparse.ArgumentParser(description="Ограниченная TCP/HTTP-приманка HoneyForge")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--tcp-port", type=int, default=2222)
    parser.add_argument("--http-port", type=int, default=8080)
    parser.add_argument("--banner", default="SSH-2.0-HoneyForge\r\n")
    parser.add_argument("--http-body", default="Service available\n")
    parser.add_argument("--read-timeout", type=float, default=2.0)
    parser.add_argument("--max-input-bytes", type=int, default=4096)
    parser.add_argument("--max-connections", type=int, default=32)
    args = parser.parse_args()
    try:
        config = LowConfig(**vars(args))
    except ValueError as error:
        parser.error(str(error))
    try:
        asyncio.run(_run(config))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
