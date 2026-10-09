"""Ограниченный lifecycle двух сетевых приманок и запись полученных байтов."""

import asyncio
import base64
import re
from datetime import UTC, datetime
from functools import partial

from honeypots.low.config import LowConfig
from honeypots.low.events import EventSink

_HTTP_TOKEN = re.compile(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+")


class _SinkFailure(Exception):
    """Внутренний сигнал: ошибка уже передана обработчику событий цикла."""


class LowService:
    """Закрывает каждое соединение после одного ограниченного обмена."""

    def __init__(self, config: LowConfig, sink: EventSink) -> None:
        self.config = config
        self._sink = sink
        self._servers: tuple[asyncio.Server, ...] = ()
        self._writers: set[asyncio.StreamWriter] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        self._lock = asyncio.Lock()
        self._stopping = True
        self.last_error: Exception | None = None

    @property
    def tcp_port(self) -> int | None:
        """Фактический порт после bind, включая автоматически назначенный порт 0."""
        return int(self._servers[0].sockets[0].getsockname()[1]) if self._servers else None

    @property
    def http_port(self) -> int | None:
        """Фактический HTTP-порт либо None, пока сервис остановлен."""
        return int(self._servers[1].sockets[0].getsockname()[1]) if self._servers else None

    @property
    def active_connections(self) -> int:
        """Число принятых соединений, ещё не завершивших обработку."""
        return len(self._writers)

    async def start(self) -> None:
        """Открывает оба порта атомарно для lifecycle; откатывает частичный bind."""
        async with self._lock:
            if self._servers:
                return
            created: list[asyncio.Server] = []
            try:
                for protocol, port in (
                    ("tcp", self.config.tcp_port),
                    ("http", self.config.http_port),
                ):
                    created.append(
                        await asyncio.start_server(
                            partial(self._accept, protocol=protocol),
                            self.config.host,
                            port,
                            limit=self.config.max_input_bytes + 1,
                            start_serving=False,
                        )
                    )
                self._servers = tuple(created)
                self._stopping = False
                for server in created:
                    await server.start_serving()
            except BaseException:
                self._stopping = True
                for server in created:
                    server.close()
                await asyncio.gather(*(server.wait_closed() for server in created))
                await self._close_connections()
                self._servers = ()
                raise

    async def stop(self) -> None:
        """Закрывает слушатели и активные подключения; допускает повторный вызов."""
        async with self._lock:
            self._stopping = True
            servers, self._servers = self._servers, ()
            for server in servers:
                server.close()
            await self._close_connections()
            await asyncio.gather(*(server.wait_closed() for server in servers))

    async def _close_connections(self) -> None:
        """Прерывает ввод-вывод до ожидания задач, чтобы stop не ждал таймаута клиента."""
        for writer in tuple(self._writers):
            writer.transport.abort()
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._writers.clear()
        self._tasks.clear()

    def _accept(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, protocol: str
    ) -> None:
        """Проверяет лимит синхронно, до выделения задачи обработки соединения."""
        if self._stopping or len(self._writers) >= self.config.max_connections:
            writer.close()
            return
        self._writers.add(writer)
        task = asyncio.create_task(self._handle(reader, writer, protocol))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, protocol: str
    ) -> None:
        """Ошибки клиента закрывают соединение; ошибка журнала не позволяет отвечать успешно."""
        try:
            self._emit("connection", protocol, writer, b"")
            if protocol == "tcp":
                await self._tcp(reader, writer)
            else:
                await self._http(reader, writer)
        except (_SinkFailure, ConnectionError, TimeoutError):
            pass
        finally:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
            except (ConnectionError, TimeoutError):
                writer.transport.abort()
            finally:
                self._writers.discard(writer)

    def _emit(self, kind: str, protocol: str, writer: asyncio.StreamWriter, payload: bytes) -> None:
        """Кодирует недоверенные байты, не вставляя их в строки журнала как текст."""
        peer = writer.get_extra_info("peername")
        event: dict[str, object] = {
            "event": kind,
            "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "protocol": protocol,
            "peer": {"host": str(peer[0]), "port": int(peer[1])} if peer else None,
            "payload_b64": base64.b64encode(payload[: self.config.max_input_bytes]).decode("ascii"),
        }
        try:
            self._sink.emit(event)
        except Exception as error:
            self.last_error = error
            asyncio.get_running_loop().call_exception_handler(
                {
                    "message": "LowService: запись события не подтверждена; соединение закрывается",
                    "exception": error,
                    "service": self,
                }
            )
            raise _SinkFailure from error

    async def _tcp(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Отправляет фиксированный баннер и собирает вход только до лимита или срока."""
        writer.write(self.config.banner.encode("ascii"))
        await asyncio.wait_for(writer.drain(), timeout=self.config.read_timeout)
        payload = bytearray()
        try:
            async with asyncio.timeout(self.config.read_timeout):
                while len(payload) < self.config.max_input_bytes:
                    chunk = await reader.read(min(4096, self.config.max_input_bytes - len(payload)))
                    if not chunk:
                        break
                    payload.extend(chunk)
        except TimeoutError:
            pass
        self._emit("request", "tcp", writer, bytes(payload))

    async def _http(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Сохраняет ограниченный запрос перед отправкой статического ответа."""
        payload, status = await self._read_headers(reader)
        head = False
        if status == 200:
            status, head = self._parse_headers(payload)
        self._emit("request", "http", writer, payload)
        descriptions = {
            200: "OK",
            400: "Bad Request",
            405: "Method Not Allowed",
            431: "Request Header Fields Too Large",
        }
        body = (
            self.config.http_body.encode("utf-8")
            if status == 200
            else (descriptions[status] + "\n").encode("ascii")
        )
        headers = (
            f"HTTP/1.1 {status} {descriptions[status]}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "Connection: close\r\n"
        )
        if status == 405:
            headers += "Allow: GET, HEAD\r\n"
        writer.write((headers + "\r\n").encode("ascii") + (b"" if head else body))
        await asyncio.wait_for(writer.drain(), timeout=self.config.read_timeout)

    async def _read_headers(self, reader: asyncio.StreamReader) -> tuple[bytes, int]:
        """Единый срок чтения препятствует бесконечному продлению запроса по одному байту."""
        payload = bytearray()
        try:
            async with asyncio.timeout(self.config.read_timeout):
                while True:
                    chunk = await reader.read(
                        min(4096, self.config.max_input_bytes + 1 - len(payload))
                    )
                    if not chunk:
                        return bytes(payload), 400
                    payload.extend(chunk)
                    end = payload.find(b"\r\n\r\n")
                    if end >= 0 and end + 4 <= self.config.max_input_bytes:
                        return bytes(payload[: end + 4]), 200
                    if len(payload) > self.config.max_input_bytes:
                        return bytes(payload[: self.config.max_input_bytes]), 431
        except TimeoutError:
            return bytes(payload), 400

    @staticmethod
    def _parse_headers(payload: bytes) -> tuple[int, bool]:
        """Проверяет синтаксис без маршрутизации, чтения файлов или отражения запроса."""
        lines = payload[:-4].split(b"\r\n")
        request = lines[0].split(b" ")
        if len(request) != 3:
            return 400, False
        method, target, version = request
        if (
            _HTTP_TOKEN.fullmatch(method) is None
            or not target
            or any(byte < 33 or byte > 126 for byte in target)
            or version not in (b"HTTP/1.0", b"HTTP/1.1")
        ):
            return 400, False
        head = method == b"HEAD"
        for line in lines[1:]:
            name, separator, value = line.partition(b":")
            if (
                not separator
                or _HTTP_TOKEN.fullmatch(name) is None
                or any(byte < 32 and byte != 9 or byte == 127 for byte in value)
            ):
                return 400, head
        return (200 if method in (b"GET", b"HEAD") else 405), head
