"""Поля событий Low для сборщика: connection_id, seq, service и запись в файловый журнал."""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from honeypots.low import JsonLineEventSink, LowConfig, LowService
from honeypots.low.journal import MIN_FILE_BYTES, JournalSettings, RotatingJournalSink

INSTANCE = str(uuid4())
DEPLOYMENT = str(uuid4())


async def tcp_exchange(port: int, data: bytes = b"hello") -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        await reader.readline()
        writer.write(data)
        await writer.drain()
        # Закрываем свою половину соединения, иначе сервер ждёт продолжения до таймаута.
        writer.write_eof()
        await asyncio.wait_for(reader.read(), timeout=2)
    finally:
        writer.close()


async def http_exchange(port: int, request: bytes = b"GET / HTTP/1.0\r\n\r\n") -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(request)
        await writer.drain()
        return await asyncio.wait_for(reader.read(), timeout=2)
    finally:
        writer.close()


def parse(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_events_of_one_connection_share_a_stable_connection_id() -> None:
    async def scenario() -> None:
        stream = io.StringIO()
        service = LowService(LowConfig(tcp_port=0, http_port=0), JsonLineEventSink(stream))
        await service.start()
        try:
            assert service.http_port is not None and service.tcp_port is not None
            await http_exchange(service.http_port)
            await http_exchange(service.http_port, b"POST / HTTP/1.1\r\n\r\n")
            await tcp_exchange(service.tcp_port)
        finally:
            await service.stop()
        events = parse(stream)

        assert [e["event"] for e in events].count("connection") == 3
        by_connection: dict[str, list[str]] = {}
        for item in events:
            assert str(UUID(item["connection_id"])) == item["connection_id"]
            by_connection.setdefault(item["connection_id"], []).append(item["event"])
        assert len(by_connection) == 3
        assert all(kinds == ["connection", "request"] for kinds in by_connection.values())

    asyncio.run(scenario())


def test_seq_is_strictly_increasing_without_gaps() -> None:
    async def scenario() -> None:
        stream = io.StringIO()
        service = LowService(LowConfig(tcp_port=0, http_port=0), JsonLineEventSink(stream))
        await service.start()
        try:
            assert service.http_port is not None
            for _ in range(5):
                await http_exchange(service.http_port)
        finally:
            await service.stop()

        assert [e["seq"] for e in parse(stream)] == list(range(1, 11))

    asyncio.run(scenario())


def test_service_reports_the_public_port_or_the_bound_port() -> None:
    async def scenario() -> None:
        stream = io.StringIO()
        config = LowConfig(tcp_port=0, http_port=0, http_public_port=18080)
        service = LowService(config, JsonLineEventSink(stream))
        await service.start()
        try:
            assert service.http_port is not None and service.tcp_port is not None
            await http_exchange(service.http_port)
            await tcp_exchange(service.tcp_port)
            bound_tcp = service.tcp_port
        finally:
            await service.stop()
        events = parse(stream)

        http = [e for e in events if e["protocol"] == "http"]
        tcp = [e for e in events if e["protocol"] == "tcp"]
        assert {e["service"]["port"] for e in http} == {18080}
        assert {e["service"]["port"] for e in tcp} == {bound_tcp}
        assert {e["service"]["protocol"] for e in events} == {"http", "tcp"}

    asyncio.run(scenario())


def test_events_keep_the_diagnostic_fields_of_d1() -> None:
    async def scenario() -> None:
        stream = io.StringIO()
        service = LowService(LowConfig(tcp_port=0, http_port=0), JsonLineEventSink(stream))
        await service.start()
        try:
            assert service.http_port is not None
            await http_exchange(service.http_port)
        finally:
            await service.stop()

        first, second = parse(stream)
        assert first["payload_b64"] == "" and second["payload_b64"]
        assert first["peer"]["host"] == "127.0.0.1"
        assert first["timestamp"].endswith("Z")

    asyncio.run(scenario())


def test_file_journal_receives_header_events_and_footer(tmp_path: Path) -> None:
    async def scenario() -> None:
        sink = RotatingJournalSink(
            JournalSettings(
                directory=tmp_path,
                instance_id=INSTANCE,
                deployment_id=DEPLOYMENT,
                profile_revision=7,
            )
        )
        sink.open()
        service = LowService(LowConfig(tcp_port=0, http_port=0, http_public_port=8800), sink)
        await service.start()
        try:
            assert service.http_port is not None
            response = await http_exchange(service.http_port)
            assert response.startswith(b"HTTP/1.1 200")
        finally:
            await service.stop()
            sink.close()

    asyncio.run(scenario())

    [path] = tmp_path.glob("low-*.jsonl")
    header, connection, request, footer = [
        json.loads(line) for line in path.read_text(encoding="ascii").splitlines()
    ]
    assert header["instance_id"] == INSTANCE and header["profile_revision"] == 7
    assert (connection["record"], connection["seq"], request["seq"]) == ("event", 1, 2)
    assert connection["connection_id"] == request["connection_id"]
    assert request["service"] == {"protocol": "http", "port": 8800}
    assert footer["last_seq"] == 2 and footer["reason"] == "shutdown"


def test_full_quota_closes_the_connection_without_a_response(tmp_path: Path) -> None:
    """Событие не записано — ответ не отдаётся (спецификация runtime-d1, раздел Low)."""

    async def scenario() -> tuple[bytes, Exception | None]:
        sink = RotatingJournalSink(
            JournalSettings(
                directory=tmp_path,
                instance_id=INSTANCE,
                deployment_id=DEPLOYMENT,
                profile_revision=1,
                max_file_bytes=MIN_FILE_BYTES,
                max_total_bytes=MIN_FILE_BYTES,
            )
        )
        # Заголовок ещё помещается в квоту, а первое же событие — уже нет.
        (tmp_path / f"low-{uuid4()}-000001.jsonl").write_bytes(b"x" * (MIN_FILE_BYTES - 700))
        sink.open()
        service = LowService(LowConfig(tcp_port=0, http_port=0), sink)
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda _loop, _context: None)
        await service.start()
        try:
            assert service.http_port is not None
            try:
                response = await http_exchange(service.http_port)
            except ConnectionError:
                response = b""
            await asyncio.sleep(0.05)
            return response, service.last_error
        finally:
            await service.stop()
            sink.close()

    response, error = asyncio.run(scenario())

    assert response == b""
    assert isinstance(error, OSError)
