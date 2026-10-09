"""Запуск локальной приманки; внешний bind требуется указать явно."""

import argparse
import asyncio
import io
import signal
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from honeypots.low import JsonLineEventSink, LowConfig, LowService
from honeypots.low.events import EventSink
from honeypots.low.journal import (
    DEFAULT_FILE_BYTES,
    DEFAULT_QUOTA_BYTES,
    JournalSettings,
    RotatingJournalSink,
)

# Начало сообщения о готовности в stderr: по нему оператор и скрипты узнают, что порты открыты.
READY_MARKER = "honeyforge-low: запущен"

# Поля LowConfig, которые приходят из аргументов; остальные аргументы — параметры журнала.
_CONFIG_FIELDS = (
    "host",
    "tcp_port",
    "http_port",
    "banner",
    "http_body",
    "read_timeout",
    "max_input_bytes",
    "max_connections",
    "tcp_public_port",
    "http_public_port",
)


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
    """Запускает сервис, ждёт сигнала остановки, затем закрывает слушатели и журнал."""
    sink: EventSink
    journal_sink: RotatingJournalSink | None = None
    if journal is None:
        sink = JsonLineEventSink(sys.stdout)
    else:
        # Недоступный каталог журнала должен обнаруживаться при запуске, а не при первой атаке.
        journal_sink = RotatingJournalSink(journal)
        journal_sink.open()
        sink = journal_sink
    service = LowService(config, sink)
    stopping = asyncio.Event()
    restore = _install_stop_handlers(asyncio.get_running_loop(), stopping)
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
        if journal_sink is not None:
            journal_sink.close()
        restore()


def build_parser() -> argparse.ArgumentParser:
    """Единственный разбор аргументов: им же пользуются тесты драйвера контейнера."""
    parser = argparse.ArgumentParser(description="Ограниченная TCP/HTTP-приманка HoneyForge")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--tcp-port", type=int, default=2222)
    parser.add_argument("--http-port", type=int, default=8080)
    parser.add_argument("--banner", default="SSH-2.0-HoneyForge\r\n")
    parser.add_argument("--http-body", default="Service available\n")
    parser.add_argument("--read-timeout", type=float, default=2.0)
    parser.add_argument("--max-input-bytes", type=int, default=4096)
    parser.add_argument("--max-connections", type=int, default=32)
    parser.add_argument(
        "--tcp-public-port", type=int, default=None, help="порт TCP, который видит клиент"
    )
    parser.add_argument(
        "--http-public-port", type=int, default=None, help="порт HTTP, который видит клиент"
    )
    parser.add_argument(
        "--journal-dir",
        type=Path,
        default=None,
        help="каталог файлового журнала; без него JSONL выводится в stdout",
    )
    parser.add_argument("--instance-id", default=None)
    parser.add_argument("--deployment-id", default=None)
    parser.add_argument("--profile-revision", type=int, default=None)
    parser.add_argument("--journal-max-bytes", type=int, default=DEFAULT_FILE_BYTES)
    parser.add_argument("--journal-quota-bytes", type=int, default=DEFAULT_QUOTA_BYTES)
    parser.add_argument("--journal-fsync", action="store_true")
    return parser


def config_from_args(args: argparse.Namespace) -> LowConfig:
    """Строит конфигурацию из аргументов, добавляя терминаторы строк.

    Баннер профиля и текст страницы приходят одной строкой без управляющих символов,
    поэтому аргументы командной строки контейнера не содержат CR и LF. Если баннер не
    оканчивается на CRLF, добавляется CRLF; непустой текст страницы получает
    завершающий LF. Остальную проверку выполняет сам LowConfig.
    """
    values = {name: getattr(args, name) for name in _CONFIG_FIELDS}
    if not values["banner"].endswith("\r\n"):
        values["banner"] += "\r\n"
    if values["http_body"] and not values["http_body"].endswith("\n"):
        values["http_body"] += "\n"
    return LowConfig(**values)


def journal_from_args(args: argparse.Namespace) -> JournalSettings | None:
    """Параметры файлового журнала либо None, если каталог не задан."""
    if args.journal_dir is None:
        return None
    if args.instance_id is None or args.deployment_id is None or args.profile_revision is None:
        raise ValueError(
            "--journal-dir требует --instance-id, --deployment-id и --profile-revision"
        )
    return JournalSettings(
        directory=args.journal_dir,
        instance_id=args.instance_id,
        deployment_id=args.deployment_id,
        profile_revision=args.profile_revision,
        max_file_bytes=args.journal_max_bytes,
        max_total_bytes=args.journal_quota_bytes,
        fsync_each_event=args.journal_fsync,
    )


def main(argv: Sequence[str] | None = None) -> None:
    """Преобразует только разрешённые аргументы CLI в проверяемую конфигурацию."""
    # Служебные сообщения всегда в UTF-8: иначе при выводе в канал Windows использует
    # локальную кодировку, и сборщик или оператор получает нечитаемый текст.
    if isinstance(sys.stderr, io.TextIOWrapper):
        sys.stderr.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = config_from_args(args)
        journal = journal_from_args(args)
    except ValueError as error:
        parser.error(str(error))
    try:
        asyncio.run(_run(config, journal))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
