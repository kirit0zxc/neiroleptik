"""WS читает committed страницы; уведомления и локальный список не нужны."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from honeyforge.common import DomainError
from honeyforge.telemetry.db_operations import DBOperations
from honeyforge.telemetry.presentation import PublicEventPresenter
from honeyforge.telemetry.read_contract import PublicationReader


class LiveStreamService:
    """Одна страница в памяти; медленная отправка завершается по таймауту.

    authenticate обязана выполнять реальную проверку cookie/Origin и завершать
    Session до возврата. Чтение выполняется в worker thread, не в event loop.
    """

    def __init__(
        self,
        reader: PublicationReader,
        presenter: PublicEventPresenter,
        poll_seconds: float = 0.25,
        send_seconds: float = 5,
        page_size: int = 100,
    ) -> None:
        if not 0 < poll_seconds <= 60 or not 0 < send_seconds <= 60:
            raise ValueError("Некорректные таймауты")
        if not 1 <= page_size <= 500:
            raise ValueError("Некорректный размер страницы")
        self.reader, self.presenter = reader, presenter
        self.poll_seconds, self.send_seconds = poll_seconds, send_seconds
        self.page_size = page_size

    async def run(
        self, send: Callable[[dict[str, Any]], Awaitable[None]], authenticate: Callable[[], None]
    ) -> None:
        operations = DBOperations()

        async def deliver(payload: dict[str, Any]) -> None:
            await operations.call(authenticate)
            await asyncio.wait_for(send(payload), self.send_seconds)

        await operations.call(authenticate)
        last = await operations.call(self.reader.watermark)
        await deliver({"type": "hello", "watermark": str(last)})
        while True:
            await operations.call(authenticate)
            floor = await operations.call(self.reader.retention_floor)
            if floor is not None and last < floor:
                raise DomainError(410, "История потока недоступна")
            watermark = await operations.call(self.reader.watermark)
            rows = await operations.call(self.reader.stream, last, watermark, self.page_size)
            for row in rows:
                await deliver(
                    {
                        "type": "event",
                        "stream_seq": str(row.seq),
                        "event": self.presenter.present(row),
                    }
                )
                last = row.seq
            if not rows:
                await asyncio.sleep(self.poll_seconds)
