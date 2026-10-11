"""Наш ограниченный hf-test-tty/1; совместимость с Cowrie не заявляется."""

import codecs
import struct
from dataclasses import dataclass
from typing import BinaryIO, Literal

MAGIC = b"HF-TTY/1\n"
HEADER = struct.Struct("!QBI")


@dataclass(frozen=True)
class TtyLimits:
    """Ограничения чтения и количества внутренних DTO."""

    max_bytes: int = 1024 * 1024
    max_frames: int = 1000
    max_payload: int = 65536

    def __post_init__(self) -> None:
        if any(
            type(v) is not int or v < 1 for v in (self.max_bytes, self.max_frames, self.max_payload)
        ):
            raise ValueError("Нужны положительные целые лимиты")


@dataclass(frozen=True)
class TtyFragment:
    """Символ, пересекающий payload, появляется во фрагменте завершения UTF-8."""

    timestamp_us: int
    direction: Literal["input", "output"]
    text: str


@dataclass(frozen=True)
class TtyResult:
    """incomplete означает, что результат нельзя выдавать за полный replay."""

    fragments: tuple[TtyFragment, ...]
    incomplete: bool
    code: str | None = None


class FixtureTtyConverter:
    """Потоковый разбор с отдельным decoder направления; терминал не исполняется."""

    def convert(self, stream: BinaryIO, limits: TtyLimits | None = None) -> TtyResult:
        limits = limits or TtyLimits()
        fragments: list[TtyFragment] = []
        used = 0

        def read_exact(count: int) -> bytes:
            nonlocal used
            parts = bytearray()
            while len(parts) < count:
                # Единственный дополнительный byte позволяет отличить EOF от превышения.
                request = min(4096, count - len(parts), limits.max_bytes - used + 1)
                if request <= 0:
                    raise ValueError("BYTES_LIMIT")
                part = stream.read(request)
                if not part:
                    break
                used += len(part)
                if used > limits.max_bytes:
                    raise ValueError("BYTES_LIMIT")
                parts.extend(part)
            return bytes(parts)

        def failure(code: str) -> TtyResult:
            return TtyResult(tuple(fragments), True, code)

        decoders = {d: codecs.getincrementaldecoder("utf-8")("strict") for d in (1, 2)}
        previous = 0
        try:
            magic = read_exact(len(MAGIC))
            if magic != MAGIC:
                return failure("EMPTY_INPUT" if not magic else "INVALID_MAGIC")
            while True:
                header = read_exact(HEADER.size)
                if not header:
                    for decoder in decoders.values():
                        decoder.decode(b"", final=True)
                    return TtyResult(tuple(fragments), False)
                if len(header) != HEADER.size:
                    return failure("INCOMPLETE_HEADER")
                if len(fragments) >= limits.max_frames:
                    return failure("FRAME_LIMIT")
                timestamp, direction, length = HEADER.unpack(header)
                if direction not in decoders:
                    return failure("UNKNOWN_DIRECTION")
                if timestamp < previous:
                    return failure("REVERSED_TIME")
                if length > limits.max_payload:
                    return failure("PAYLOAD_LIMIT")
                payload = read_exact(length)
                if len(payload) != length:
                    return failure("INCOMPLETE_PAYLOAD")
                text = decoders[direction].decode(payload)
                visible = "".join(
                    f"\\x{ord(char):02x}"
                    if (ord(char) < 32 and char not in "\n\t") or ord(char) == 127
                    else char
                    for char in text
                )
                fragments.append(
                    TtyFragment(timestamp, "input" if direction == 1 else "output", visible)
                )
                previous = timestamp
        except UnicodeDecodeError:
            return failure("INVALID_UTF8")
        except ValueError:
            return failure("BYTES_LIMIT")
        except OSError:
            return failure("IO_ERROR")
