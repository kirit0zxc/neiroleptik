"""Реальные loopback-проверки входных данных, ограничений и остановки приманок."""

import asyncio
import base64
import io
import json
import socket
from collections.abc import Callable, Mapping
from typing import Any

import pytest

from honeypots.low import EventSink, JsonLineEventSink, LowConfig, LowService


async def wait_until(predicate: Callable[[], bool]) -> None:
    """Ожидает наблюдаемого состояния вместо предположений о скорости event loop."""
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.005)


async def close_client(writer: asyncio.StreamWriter) -> None:
    writer.close()
    try:
        await writer.wait_closed()
    except ConnectionError:
        pass


async def http_exchange(service: LowService, request: bytes) -> bytes:
    assert service.http_port is not None
    reader, writer = await asyncio.open_connection(service.config.host, service.http_port)
    try:
        writer.write(request)
        await writer.drain()
        return await asyncio.wait_for(reader.read(), timeout=2)
    finally:
        await close_client(writer)


@pytest.mark.parametrize(
    ("raw_request", "status", "expected_body"),
    [
        (b"GET / HTTP/1.1\r\nHost: example\r\n\r\n", 200, "Привет\n".encode()),
        (b"GET / HTTP/1.0\r\n\r\n", 200, "Привет\n".encode()),
        (b"HEAD / HTTP/1.1\r\nHost: example\r\n\r\n", 200, b""),
        (b"POST / HTTP/1.1\r\nHost: example\r\n\r\n", 405, b"Method Not Allowed\n"),
        (b"GET / HTTP/2.0\r\n\r\n", 400, b"Bad Request\n"),
        (b"GET / HTTP/1.1\r\nMalformed\r\n\r\n", 400, b"Bad Request\n"),
        (b"GET / HTTP/1.1\r\nHeader: abc\x00\r\n\r\n", 400, b"Bad Request\n"),
        (b"GET / secret HTTP/1.1\r\n\r\n", 400, b"Bad Request\n"),
        (
            b"GET / HTTP/1.1\r\nX: " + b"a" * 150 + b"\r\n\r\n",
            431,
            b"Request Header Fields Too Large\n",
        ),
    ],
)
def test_http_static_responses(raw_request: bytes, status: int, expected_body: bytes) -> None:
    async def scenario() -> None:
        stream = io.StringIO()
        config = LowConfig(tcp_port=0, http_port=0, http_body="Привет\n", max_input_bytes=128)
        service = LowService(config, JsonLineEventSink(stream))
        await service.start()
        try:
            response = await http_exchange(service, raw_request)
            headers, body = response.split(b"\r\n\r\n", 1)
            assert headers.startswith(f"HTTP/1.1 {status} ".encode())
            assert body == expected_body
            expected_length = len(config.http_body.encode()) if status == 200 else len(body)
            assert f"Content-Length: {expected_length}\r\n".encode() in headers + b"\r\n"
            if status == 405:
                assert b"Allow: GET, HEAD" in headers
            events = [json.loads(line) for line in stream.getvalue().splitlines()]
            assert [event["event"] for event in events] == ["connection", "request"]
            assert events[0]["protocol"] == "http"
            assert events[0]["peer"]["host"] == "127.0.0.1"
            assert events[0]["timestamp"].endswith("Z")
            assert base64.b64decode(events[1]["payload_b64"]) == raw_request[:128]
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_tcp_banner_and_binary_input_limit() -> None:
    async def scenario() -> None:
        stream = io.StringIO()
        service = LowService(
            LowConfig(tcp_port=0, http_port=0, max_input_bytes=8), JsonLineEventSink(stream)
        )
        await service.start()
        assert service.tcp_port is not None
        reader, writer = await asyncio.open_connection("127.0.0.1", service.tcp_port)
        try:
            assert await reader.readline() == service.config.banner.encode()
            writer.write(b"\x00\xff\n\r\x1babcEXCESS")
            await writer.drain()
            assert await asyncio.wait_for(reader.read(), timeout=2) == b""
            events = [json.loads(line) for line in stream.getvalue().splitlines()]
            assert len(events) == 2
            assert base64.b64decode(events[1]["payload_b64"]) == b"\x00\xff\n\r\x1babc"
        finally:
            await close_client(writer)
            await service.stop()

    asyncio.run(scenario())


def test_http_limit_accepts_exact_header_length() -> None:
    async def scenario() -> None:
        request = b"GET / HTTP/1.0\r\n\r\n"
        service = LowService(
            LowConfig(tcp_port=0, http_port=0, max_input_bytes=len(request)),
            JsonLineEventSink(io.StringIO()),
        )
        await service.start()
        try:
            assert (await http_exchange(service, request)).startswith(b"HTTP/1.1 200 ")
            assert (await http_exchange(service, request.replace(b"/", b"/x"))).startswith(
                b"HTTP/1.1 431 "
            )
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_tcp_idle_timeout_and_http_total_deadline() -> None:
    async def scenario() -> None:
        service = LowService(
            LowConfig(tcp_port=0, http_port=0, read_timeout=0.15), JsonLineEventSink(io.StringIO())
        )
        await service.start()
        assert service.tcp_port is not None and service.http_port is not None
        reader, writer = await asyncio.open_connection("127.0.0.1", service.tcp_port)
        http_reader, http_writer = await asyncio.open_connection("127.0.0.1", service.http_port)
        try:
            assert await reader.readline() == service.config.banner.encode()
            http_writer.write(b"GET")
            await http_writer.drain()
            await asyncio.sleep(0.1)
            http_writer.write(b" /")
            await http_writer.drain()
            assert await asyncio.wait_for(reader.read(), timeout=0.2) == b""
            assert (await asyncio.wait_for(http_reader.read(), timeout=0.1)).startswith(
                b"HTTP/1.1 400 "
            )
        finally:
            await close_client(writer)
            await close_client(http_writer)
            await service.stop()

    asyncio.run(scenario())


def test_connection_limit_and_stop_close_active_clients() -> None:
    async def scenario() -> None:
        service = LowService(
            LowConfig(tcp_port=0, http_port=0, max_connections=1, read_timeout=30),
            JsonLineEventSink(io.StringIO()),
        )
        await service.start()
        ports = service.tcp_port, service.http_port
        await service.start()
        assert ports == (service.tcp_port, service.http_port)
        assert service.tcp_port is not None and service.http_port is not None
        reader, writer = await asyncio.open_connection("127.0.0.1", service.tcp_port)
        rejected_reader, rejected_writer = await asyncio.open_connection(
            "127.0.0.1", service.http_port
        )
        try:
            assert await reader.readline() == service.config.banner.encode()
            assert await asyncio.wait_for(rejected_reader.read(), timeout=1) == b""
            assert service.active_connections == 1
            await asyncio.wait_for(service.stop(), timeout=0.5)
            assert await asyncio.wait_for(reader.read(), timeout=0.5) == b""
            assert service.active_connections == 0
            await service.stop()
            assert service.tcp_port is None and service.http_port is None
            await service.start()
            assert service.tcp_port is not None
        finally:
            await close_client(writer)
            await close_client(rejected_writer)
            await service.stop()

    asyncio.run(scenario())


def test_partial_bind_failure_releases_first_socket() -> None:
    async def scenario() -> None:
        with socket.socket() as temporary:
            temporary.bind(("127.0.0.1", 0))
            first_port = temporary.getsockname()[1]
        with socket.socket() as blocker:
            blocker.bind(("127.0.0.1", 0))
            blocker.listen()
            blocked_port = blocker.getsockname()[1]
            service = LowService(
                LowConfig(tcp_port=first_port, http_port=blocked_port),
                JsonLineEventSink(io.StringIO()),
            )
            with pytest.raises(OSError):
                await service.start()
            assert service.tcp_port is None and service.http_port is None
            with socket.socket() as released:
                released.bind(("127.0.0.1", first_port))
        await service.start()
        await service.stop()

    asyncio.run(scenario())


class FailingSink:
    """Позволяет отличить отказ события соединения от отказа записи запроса."""

    def __init__(self, fail_on: str) -> None:
        self.fail_on = fail_on

    def emit(self, event: Mapping[str, object]) -> None:
        if event["event"] == self.fail_on:
            raise OSError("Диск недоступен")


@pytest.mark.parametrize("fail_on", ["connection", "request"])
def test_sink_error_is_observable_and_http_has_no_success(fail_on: str) -> None:
    async def scenario() -> None:
        sink: EventSink = FailingSink(fail_on)
        service = LowService(LowConfig(tcp_port=0, http_port=0), sink)
        observed: list[dict[str, Any]] = []
        loop = asyncio.get_running_loop()
        previous = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: observed.append(context))
        await service.start()
        try:
            try:
                response = await http_exchange(service, b"GET / HTTP/1.0\r\n\r\n")
            except ConnectionError:
                response = b""
            assert response == b""
            await wait_until(lambda: bool(observed))
            assert isinstance(service.last_error, OSError)
            assert observed[0]["exception"] is service.last_error
            assert "не подтверждена" in observed[0]["message"]
        finally:
            await service.stop()
            loop.set_exception_handler(previous)

    asyncio.run(scenario())


def ipv6_loopback_usable() -> bool:
    """Проверяет окружение независимо от тестируемого кода: bind и реальное соединение.

    На части систем bind на ::1 проходит, а connect запрещён (например, WinError 5
    в ограниченной среде). Такой отказ окружения не является дефектом LowService.
    """
    if not socket.has_ipv6:
        return False
    try:
        with socket.socket(socket.AF_INET6) as server:
            server.bind(("::1", 0))
            server.listen(1)
            with socket.socket(socket.AF_INET6) as client:
                client.settimeout(1)
                client.connect(server.getsockname())
    except OSError:
        return False
    return True


def test_ipv6_loopback_when_available() -> None:
    if not ipv6_loopback_usable():
        pytest.skip("IPv6 loopback недоступен в окружении")

    async def scenario() -> None:
        service = LowService(
            LowConfig(host="::1", tcp_port=0, http_port=0), JsonLineEventSink(io.StringIO())
        )
        await service.start()
        try:
            assert (await http_exchange(service, b"GET / HTTP/1.0\r\n\r\n")).startswith(
                b"HTTP/1.1 200 "
            )
        finally:
            await service.stop()

    asyncio.run(scenario())
