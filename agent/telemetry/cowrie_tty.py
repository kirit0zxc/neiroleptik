"""Ограниченный parser бинарного ttylog Cowrie, без transport и владения потоком."""

import base64
import struct
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import BinaryIO

HEADER = struct.Struct("<iLiiLL")


@dataclass(frozen=True)
class CowrieTtyLimits:
    """Пределы чтения, всех принятых кадров и одного WRITE до выделения памяти."""

    max_bytes: int = 1048576
    max_frames: int = 10000
    max_payload: int = 65536

    def __post_init__(self) -> None:
        if any(
            type(v) is not int or v < 1 for v in (self.max_bytes, self.max_frames, self.max_payload)
        ):
            raise ValueError("Нужны положительные целые лимиты")


@dataclass(frozen=True)
class CowrieTtyResult:
    """Принятый префикс; consumed_bytes исключает ошибочный кадр и lookahead."""

    chunks: tuple[dict[str, int | str | bool], ...]
    started_at: datetime | None
    complete: bool
    code: str | None
    consumed_bytes: int


class _ReadError(ValueError):
    """Внутренняя безопасная категория отказа чтения."""


class _Reader:
    """Обрабатывает короткие read и допускает ровно один byte сверх бюджета."""

    def __init__(self, stream: BinaryIO, budget: int) -> None:
        self.stream, self.budget, self.used = stream, budget, 0

    def read(self, count: int) -> bytes:
        parts = bytearray()
        while len(parts) < count:
            request = min(4096, count - len(parts), self.budget - self.used + 1)
            if request <= 0:
                raise _ReadError("BYTES_LIMIT")
            try:
                part = self.stream.read(request)
            except Exception:
                # Внешний поток может выбросить произвольный exception с секретом.
                raise _ReadError("IO_ERROR") from None
            if not isinstance(part, bytes) or len(part) > request:
                raise _ReadError("IO_ERROR")
            if not part:
                break
            self.used += len(part)
            if self.used > self.budget:
                raise _ReadError("BYTES_LIMIT")
            parts.extend(part)
        return bytes(parts)


class CowrieTtyConverter:
    """Принимает OPEN/WRITE/CLOSE; сохраняет bytes и целочисленное время WRITE."""

    def convert(
        self, stream: BinaryIO, *, sealed: bool = False, limits: CowrieTtyLimits | None = None
    ) -> CowrieTtyResult:
        if type(sealed) is not bool:
            raise ValueError("Нужен строгий bool seal")
        limits = limits or CowrieTtyLimits()
        reader = _Reader(stream, limits.max_bytes)
        chunks: list[dict[str, int | str | bool]] = []
        started_at = None
        consumed = frames = start_us = previous_us = 0
        closed = False

        def result(code: str | None) -> CowrieTtyResult:
            return CowrieTtyResult(tuple(chunks), started_at, code is None, code, consumed)

        try:
            while True:
                # Один byte различает точный EOF и следующий кадр без лишнего чтения.
                first = reader.read(1)
                if not first:
                    if not frames:
                        return result("EMPTY_INPUT")
                    if closed:
                        return result(None if sealed else "AWAITING_SEAL")
                    return result("MISSING_CLOSE" if sealed else "RECORDING_ACTIVE")
                header = first + reader.read(HEADER.size - 1)
                if len(header) != HEADER.size:
                    return result("INCOMPLETE_HEADER")
                op, tty, length, direction, seconds, microseconds = HEADER.unpack(header)
                if tty != 0 or length < 0:
                    return result("INVALID_HEADER")
                if op not in (1, 2, 3):
                    return result("UNKNOWN_OPERATION")
                if op in (1, 2) and (length != 0 or direction != 0):
                    return result("INVALID_HEADER")
                if op == 3 and direction not in (1, 2):
                    return result("UNKNOWN_DIRECTION")
                if microseconds > 999999:
                    return result("INVALID_TIME")
                timestamp = seconds * 1000000 + microseconds
                if (not frames and op != 1) or (frames and op == 1) or closed:
                    return result("INVALID_SEQUENCE")
                if frames and timestamp < previous_us:
                    return result("REVERSED_TIME")
                if length > limits.max_payload:
                    return result("PAYLOAD_LIMIT")
                payload = reader.read(length)
                if len(payload) != length:
                    return result("INCOMPLETE_PAYLOAD")
                # Неполный snapshot не объявляется исчерпавшим frame budget.
                # До этого чтения действуют byte/payload лимиты, кадр ещё не принят.
                if frames >= limits.max_frames:
                    return result("FRAME_LIMIT")
                if op == 1:
                    start_us = timestamp
                    # Без float и платформенного fromtimestamp для диапазона uint32.
                    started_at = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(
                        seconds=seconds, microseconds=microseconds
                    )
                elif op == 2:
                    closed = True
                else:
                    chunks.append(
                        {
                            "seq": len(chunks),
                            "offset_ms": (timestamp - start_us) // 1000,
                            "direction": "in" if direction == 1 else "out",
                            "data_base64": base64.b64encode(payload).decode("ascii"),
                            "truncated": False,
                        }
                    )
                consumed += HEADER.size + length
                frames += 1
                previous_us = timestamp
        except _ReadError as error:
            return result(str(error))
        except (OSError, ValueError, TypeError):
            return result("IO_ERROR")
