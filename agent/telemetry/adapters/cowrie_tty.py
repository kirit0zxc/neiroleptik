"""Ограниченный перевод закрытого TTY Cowrie в чанки существующего Event v1."""

import base64
import os
import re
import stat
import struct
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from agent.telemetry.adapters.low import SourceRecord
from agent.telemetry.schemas import Event
from agent.telemetry.spool import SpoolRepository

HEADER = struct.Struct("<iLiiLL")
MAX_CHUNKS = 4000


class CowrieTtyCollector:
    """Пароли SSH аутентификации отсутствуют в TTY: запись начинается после входа."""

    def __init__(self, spool: SpoolRepository) -> None:
        self.spool = spool

    def step(self, directory: Path, raw: dict[str, Any], basis: Event) -> bool:
        """До завершения всех чанков JSON checkpoint остаётся на log.closed."""
        name = str(raw.get("ttylog", "")).replace("\\", "/").rsplit("/", 1)[-1]
        if not re.fullmatch(r"[a-f0-9]{64}", name):
            raise ValueError("Cowrie TTY: некорректное имя закрытого файла")
        path = directory / "tty" / name
        for part in (path, *path.parents):
            info = part.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("Cowrie TTY: ссылки запрещены")
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError("Cowrie TTY: требуется обычный файл")
        source = f"cowrie-tty:{basis.event_id}"
        after = self.spool.checkpoint(source)
        processed = 0
        for seq, chunk in enumerate(self.chunks(path)):
            if seq < after:
                continue
            event = basis.model_copy(
                update={
                    "event_id": uuid5(NAMESPACE_URL, f"{source}:{seq}"),
                    "type": "session.output",
                    "source_seq": seq,
                    "data": {
                        "replay": {
                            **chunk,
                            "seq": basis.source_seq * 8192 + seq,
                            "capture_seq": seq,
                            "recording_id": str(basis.event_id),
                        }
                    },
                }
            )
            event = Event.model_validate(event.model_dump())
            self.spool.append(SourceRecord(source, seq, seq + 1, event))
            processed += 1
            if processed >= 100:
                return False
        return True

    @staticmethod
    def chunks(path: Path) -> Iterator[dict[str, Any]]:
        """Не более 4000 чанков по 4096 байт; превышение явно завершает запись разрывом."""
        descriptor = os.open(
            path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        )
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_size > 64 * 1024 * 1024
            ):
                raise ValueError("Cowrie TTY: небезопасный или слишком большой файл")
            first = stream.read(HEADER.size)
            if len(first) != HEADER.size:
                raise ValueError("Cowrie TTY: отсутствует заголовок")
            op, _, length, _, sec, usec = HEADER.unpack(first)
            if op != 1 or length != 0 or usec >= 1000000:
                raise ValueError("Cowrie TTY: некорректное начало")
            start = sec * 1000000 + usec
            started = datetime.fromtimestamp(start / 1000000, UTC).isoformat()
            count, offset, complete = 0, 0, False
            while count < MAX_CHUNKS:
                header = stream.read(HEADER.size)
                if len(header) != HEADER.size:
                    break
                op, _, length, direction, sec, usec = HEADER.unpack(header)
                if not 0 <= length <= 1024 * 1024 or usec >= 1000000:
                    break
                offset = max(offset, (sec * 1000000 + usec - start) // 1000)
                if op == 2 and length == 0:
                    complete = stream.read(1) == b""
                    break
                if op != 3 or direction not in (1, 2):
                    break
                content = stream.read(length)
                if len(content) != length:
                    break
                for position in range(0, length, 4096):
                    if count >= MAX_CHUNKS:
                        break
                    yield {
                        "offset_ms": offset,
                        "direction": "in" if direction == 1 else "out",
                        "data_base64": base64.b64encode(
                            content[position : position + 4096]
                        ).decode(),
                        "truncated": False,
                        "started_at": started,
                        "final": False,
                    }
                    count += 1
            yield {
                "offset_ms": offset,
                "direction": "out",
                "data_base64": "",
                "truncated": not complete,
                "started_at": started,
                "final": True,
            }
