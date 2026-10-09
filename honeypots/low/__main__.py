"""Запуск локальной приманки; внешний bind требуется указать явно."""

import argparse
import asyncio
import signal
import sys

from honeypots.low import JsonLineEventSink, LowConfig, LowService


<<<<<<< Updated upstream
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
=======
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
>>>>>>> Stashed changes


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
