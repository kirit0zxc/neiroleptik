"""Проверки конфигурации и честной диагностики ошибок локального журнала."""

import io
import json
from typing import Any

import pytest

from honeypots.low import JsonLineEventSink, LowConfig


@pytest.mark.parametrize(
    "changes",
    [
        {"host": "localhost"},
        {"host": 123},
        {"tcp_port": True},
        {"http_port": 65536},
        {"tcp_port": 8080, "http_port": 8080},
        {"max_connections": 0},
        {"max_connections": True},
        {"max_input_bytes": 0},
        {"max_input_bytes": 65537},
        {"read_timeout": 0},
        {"read_timeout": True},
        {"read_timeout": float("inf")},
        {"read_timeout": float("nan")},
        {"banner": "SSH\r\nspoof"},
        {"banner": "SSH\n"},
        {"banner": "SSH\x1b"},
        {"banner": "Сервис"},
        {"banner": ""},
        {"banner": "a" * 256},
        {"http_body": "\ud800"},
        {"http_body": "a" * 65537},
    ],
)
def test_invalid_config_is_rejected(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        LowConfig(**changes)


def test_loopback_defaults_and_ipv6_configuration() -> None:
    assert LowConfig().host == "127.0.0.1"
    assert LowConfig(host="::1", tcp_port=0, http_port=0).host == "::1"


def test_jsonl_encodes_one_record_without_line_injection() -> None:
    stream = io.StringIO()
    event = {"event": "request", "payload_b64": "AAo=", "text": "строка\nстрока"}
    JsonLineEventSink(stream).emit(event)
    assert len(stream.getvalue().splitlines()) == 1
    assert json.loads(stream.getvalue()) == event


class BrokenFlush(io.StringIO):
    """Имитирует отказ вывода после записи в пользовательский буфер."""

    def flush(self) -> None:
        raise OSError("Журнал недоступен")


class ShortWrite(io.StringIO):
    """Имитирует неполную запись без исключения в самом потоке."""

    def write(self, text: str) -> int:
        return super().write(text[:1])


@pytest.mark.parametrize("stream", [BrokenFlush(), ShortWrite()])
def test_jsonl_does_not_hide_incomplete_write(stream: io.StringIO) -> None:
    with pytest.raises(OSError):
        JsonLineEventSink(stream).emit({"event": "connection"})
