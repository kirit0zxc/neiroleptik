"""Запуск приманки и файлового журнала с корректной остановкой."""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from honeypots.low import JsonLineEventSink, LowConfig, LowService
from honeypots.low.events import EventSink
from honeypots.low.journal import (
    DEFAULT_FILE_BYTES,
    DEFAULT_QUOTA_BYTES,
    JournalAllocationError,
    JournalSettings,
    RotatingJournalSink,
)

READY_MARKER = "honeyforge-low: запущен"


def _install_stop_handlers(
    loop: asyncio.AbstractEventLoop, stopping: asyncio.Event
) -> Callable[[], None]:
    """Включает остановку по сигналам и возвращает функцию, снимающую обработчики.

    На POSIX используются SIGINT и SIGTERM. У Windows нет add_signal_handler, поэтому
    дочерний процесс останавливается сигналом Ctrl+Break (SIGBREAK); Ctrl+C в консоли
    обрабатывает сам asyncio.run.
    """
    if sys.platform == "win32":

        def on_break(_signum: int, _frame: object) -> None:
            loop.call_soon_threadsafe(stopping.set)

        previous = signal.signal(signal.SIGBREAK, on_break)

        def restore_break() -> None:
            signal.signal(signal.SIGBREAK, previous)

        return restore_break

    installed: list[signal.Signals] = []
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stopping.set)
        installed.append(signum)

    def restore() -> None:
        for signum in installed:
            loop.remove_signal_handler(signum)

    return restore


async def _run(config: LowConfig, journal: JournalSettings | None) -> None:
    """Запускает сервис, ждёт сигнала остановки, затем закрывает слушатели и журнал.

    Обработчики сигналов ставятся до открытия журнала: SIGTERM, пришедший между созданием
    файла и запуском сервиса, иначе завершил бы процесс действием по умолчанию и оставил
    файл без концовки. Такой сигнал просто завершит работу сразу после запуска.
    """
    stopping = asyncio.Event()
    restore = _install_stop_handlers(asyncio.get_running_loop(), stopping)
    journal_sink: RotatingJournalSink | None = None
    try:
        sink: EventSink
        if journal is None:
            sink = JsonLineEventSink(sys.stdout)
        else:
            # Недоступный каталог журнала должен обнаруживаться при запуске, а не при
            # первой атаке.
            journal_sink = RotatingJournalSink(journal)
            journal_sink.open()
            sink = journal_sink
        service = LowService(config, sink)
        try:
            await service.start()
            print(
                f"{READY_MARKER}, tcp={service.tcp_port}, http={service.http_port}",
                file=sys.stderr,
                flush=True,
            )
            await stopping.wait()
        finally:
            await service.stop()
    finally:
        if journal_sink is not None:
            journal_sink.close()
        restore()


def build_parser() -> argparse.ArgumentParser:
    """Разрешённые параметры сервиса и отдельные параметры журнала."""
    parser = argparse.ArgumentParser(description="TCP/HTTP-приманка HoneyForge")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--tcp-port", type=int, default=2222)
    parser.add_argument("--http-port", type=int, default=8080)
    parser.add_argument("--tcp-public-port", type=int)
    parser.add_argument("--http-public-port", type=int)
    parser.add_argument("--banner", default="SSH-2.0-HoneyForge\r\n")
    parser.add_argument("--http-body", default="Service available\n")
    parser.add_argument("--read-timeout", type=float, default=2.0)
    parser.add_argument("--max-input-bytes", type=int, default=4096)
    parser.add_argument("--max-connections", type=int, default=32)
    parser.add_argument("--journal-dir", type=Path)
    parser.add_argument("--instance-id")
    parser.add_argument("--deployment-id")
    parser.add_argument("--profile-revision", type=int)
    parser.add_argument("--journal-max-bytes", type=int, default=DEFAULT_FILE_BYTES)
    parser.add_argument("--journal-quota-bytes", type=int, default=DEFAULT_QUOTA_BYTES)
    parser.add_argument("--journal-fsync", action="store_true")
    # Startup-выделение первого поколения передаёт runtime, когда привязывает писателя
    # к новой цепочке журналов. Задаются вместе и только вместе с --journal-dir.
    parser.add_argument("--journal-first-id")
    parser.add_argument("--journal-first-seq", type=int)
    return parser


def config_from_args(args: argparse.Namespace) -> LowConfig:
    """Нормализует терминаторы; параметры журнала не попадают в LowConfig."""
    values = {name: getattr(args, name) for name in LowConfig.__dataclass_fields__}
    banner = args.banner
    values["banner"] = banner if banner.endswith("\r\n") else banner + "\r\n"
    body = args.http_body
    values["http_body"] = body if not body or body.endswith("\n") else body + "\n"
    return LowConfig(**values)


def journal_from_args(args: argparse.Namespace) -> JournalSettings | None:
    """Файловый режим требует полной идентичности назначения и экземпляра."""
    if args.journal_dir is None:
        if args.journal_first_id is not None or args.journal_first_seq is not None:
            raise ValueError("journal-first-id и journal-first-seq требуют journal-dir")
        return None
    if args.instance_id is None or args.deployment_id is None or args.profile_revision is None:
        raise ValueError("journal-dir требует instance-id, deployment-id и profile-revision")
    return JournalSettings(
        args.journal_dir,
        args.instance_id,
        args.deployment_id,
        args.profile_revision,
        args.journal_max_bytes,
        args.journal_quota_bytes,
        args.journal_fsync,
        args.journal_first_id,
        args.journal_first_seq,
    )


def main(argv: Sequence[str] | None = None) -> None:
    """Проверяет конфигурацию до запуска; ошибки ресурсов выводит без трассировки."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = config_from_args(args)
        journal = journal_from_args(args)
    except ValueError as error:
        parser.error(str(error))
    try:
        asyncio.run(_run(config, journal))
    except JournalAllocationError as error:
        # Выделение проверяется до открытия слушателей: ловушка не принимает соединения.
        print(f"honeyforge-low: {error}", file=sys.stderr)
        raise SystemExit(2) from error
    except OSError as error:
        print(f"honeyforge-low: не удалось запустить сервис: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
