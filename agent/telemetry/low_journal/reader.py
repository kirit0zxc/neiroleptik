"""Одноразовое потоковое чтение фиксированного префикса и его структуры."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import TracebackType
from typing import Any, cast

from .models import (
    JournalBinding,
    JournalReadError,
    JournalRecord,
    JournalStatus,
    JournalSummary,
    RecordKind,
)
from .source import JournalSource
from .validation import RecordValidator


class LowJournalReader:
    """Создаёт независимый проход с нуля; не хранит историю или позиции на диске."""

    def __init__(self, *, max_line_bytes: int = 65536) -> None:
        if type(max_line_bytes) is not int or max_line_bytes < 1:
            raise ValueError("max_line_bytes должен быть целым не меньше 1")
        self._max_line_bytes = max_line_bytes

    def open(self, path: Path, binding: JournalBinding) -> JournalStream:
        """Файл открывается при входе в контекст; идентичность задаётся вызывающим кодом."""
        return JournalStream(Path(path), binding, self._max_line_bytes)


class JournalStream(Iterator[JournalRecord]):
    """Один дескриптор и ограниченный буфер, без фонового слежения за ротацией."""

    def __init__(self, path: Path, binding: JournalBinding, limit: int) -> None:
        self._path = path
        self._binding = binding
        self._limit = limit
        self._source: JournalSource | None = None
        self._entered = False
        self._finished = False
        self._summary: JournalSummary | None = None
        self._buffer = bytearray()
        self._read_offset = 0
        self._offset = 0
        self._journal_id: str | None = None
        self._last_seq = 0
        self._footer = False
        self._validator = RecordValidator()

    def __enter__(self) -> JournalStream:
        if self._entered:
            raise RuntimeError("Поток одноразовый; откройте новый проход")
        self._entered = True
        try:
            self._source = JournalSource(self._path)
        except OSError:
            raise JournalReadError("IO_ERROR", None) from None
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if exc_type is not None:
            self._summary = None
        self._close()

    @property
    def bytes_read(self) -> int:
        """Фактические bytes снимка, включая упреждающее чтение и неполный хвост."""
        return self._read_offset

    @property
    def summary(self) -> JournalSummary:
        if self._summary is None:
            raise RuntimeError("Итог доступен только после успешного исчерпания потока")
        return self._summary

    def _close(self) -> None:
        source, self._source = self._source, None
        self._buffer.clear()
        if source is not None:
            try:
                source.close()
            except OSError:
                self._summary = None
                raise JournalReadError("IO_ERROR", self._offset) from None

    def __next__(self) -> JournalRecord:
        if self._finished:
            raise StopIteration
        if self._source is None:
            raise RuntimeError("Чтение требует открытого контекста")
        try:
            raw = self._line()
            if raw is None:
                self._finish()
                raise StopIteration
            row = self._validator.parse(raw, self._offset)
            self._structure(row)
            assert self._journal_id is not None
            record = JournalRecord(
                cast(RecordKind, row["record"]),
                self._journal_id,
                self._offset,
                self._offset + len(raw),
                raw,
            )
            self._offset = record.next_offset
            return record
        except (OSError, JournalReadError) as error:
            self._summary = None
            self._close()
            if isinstance(error, JournalReadError):
                raise
            raise JournalReadError("IO_ERROR", self._offset) from None

    def _line(self) -> bytes | None:
        """Читает не более limit+1 bytes в буфер и никогда не выходит за снимок."""
        source = self._source
        assert source is not None
        source.check_size(self._offset)
        if self._footer and self._offset < source.size:
            raise JournalReadError("INVALID_STRUCTURE", self._offset)
        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                if newline + 1 > self._limit:
                    raise JournalReadError("LINE_TOO_LONG", self._offset)
                raw = bytes(self._buffer[: newline + 1])
                del self._buffer[: newline + 1]
                return raw
            if len(self._buffer) > self._limit:
                raise JournalReadError("LINE_TOO_LONG", self._offset)
            if self._read_offset == source.size:
                return None
            count = min(8192, source.size - self._read_offset, self._limit + 1 - len(self._buffer))
            chunk = source.read(count)
            if not chunk:
                raise JournalReadError("SOURCE_CHANGED", self._offset)
            self._read_offset += len(chunk)
            self._buffer.extend(chunk)
            source.check_size(self._offset)

    def _structure(self, row: dict[str, Any]) -> None:
        kind = row["record"]
        if self._journal_id is None:
            if kind != "header":
                raise JournalReadError("INVALID_STRUCTURE", self._offset)
            if (
                row["deployment_id"] != self._binding.deployment_id
                or row["instance_id"] != self._binding.instance_id
                or row["profile_revision"] != self._binding.profile_revision
            ):
                raise JournalReadError("IDENTITY_MISMATCH", self._offset)
            self._journal_id = row["journal_id"]
            self._last_seq = row["first_seq"] - 1
        elif kind == "header":
            raise JournalReadError("INVALID_STRUCTURE", self._offset)
        elif kind == "event":
            if row["seq"] <= self._last_seq:
                raise JournalReadError("INVALID_SEQUENCE", self._offset)
            self._last_seq = row["seq"]
        else:
            if row["journal_id"] != self._journal_id:
                raise JournalReadError("INVALID_STRUCTURE", self._offset)
            if row["last_seq"] != self._last_seq:
                raise JournalReadError("INVALID_SEQUENCE", self._offset)
            self._footer = True

    def _finish(self) -> None:
        status: JournalStatus = (
            "closed" if self._footer else "open" if self._journal_id else "incomplete"
        )
        self._summary = JournalSummary(status, self._offset, self._journal_id)
        self._finished = True
        self._close()
