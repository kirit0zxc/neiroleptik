"""Локальный диагностический JSONL; устойчивую доставку обеспечивает отдельный spool."""

import json
from collections.abc import Mapping
from typing import Protocol, TextIO


class EventSink(Protocol):
    """Успешный возврат означает запись в локальный приёмник, но не доставку в центр."""

    def emit(self, event: Mapping[str, object]) -> None:
        """При невозможности записи выбрасывает исключение вызывающей стороне."""
        ...


class JsonLineEventSink:
    """Пишет отдельную JSON-строку и передаёт ошибки записи или flush вызывающему коду."""

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream

    def emit(self, event: Mapping[str, object]) -> None:
        """Сброс буфера потока не заявляется как fsync или подтверждение сервера."""
        line = json.dumps(dict(event), ensure_ascii=True, allow_nan=False) + "\n"
        written = self._stream.write(line)
        if written != len(line):
            raise OSError("Диагностический журнал записан не полностью")
        self._stream.flush()
