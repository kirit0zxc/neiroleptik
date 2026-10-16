"""Проверка сохранённого префикса и ограниченное чтение нового JSONL snapshot."""

import errno
import sqlite3
from contextlib import closing

from .models import (
    CowrieCollectionLimits,
    CowrieJournalError,
    CowrieJournalSource,
    CowrieProgress,
)
from .repository import CowrieJournalRepository
from .source import CowrieFile


class CowrieJournalCollector:
    """Ошибки одного поколения не удаляют его raw и не затрагивают другие."""

    def __init__(self, repository: CowrieJournalRepository) -> None:
        self.repository = repository

    def step(
        self, source: CowrieJournalSource, limits: CowrieCollectionLimits | None = None
    ) -> CowrieProgress:
        limits = limits or CowrieCollectionLimits()
        saved = size = 0
        registered = False
        try:
            self.repository.register(source)
            registered = True
            _, _, checkpoint, old_tail, blocked = self.repository.state(source.journal_id)
            if blocked:
                return CowrieProgress("error", code=blocked)
            with closing(CowrieFile(source)) as stream:
                self.repository.identify(source.journal_id, stream.identity)
                if stream.size < checkpoint + len(old_tail):
                    raise CowrieJournalError("SOURCE_TRUNCATED")
                if stream.size > limits.max_scan_bytes:
                    return CowrieProgress("limited", code="SCAN_LIMIT")
                offset = read = 0
                buffer = bytearray()
                while read < stream.size or buffer:
                    newline = buffer.find(b"\n")
                    if newline >= 0:
                        raw = bytes(buffer[: newline + 1])
                        del buffer[: newline + 1]
                        if len(raw) > 65536:
                            raise CowrieJournalError("RECORD_LIMIT")
                        if offset < checkpoint:
                            self.repository.verify(source.journal_id, offset, raw)
                        else:
                            if offset == checkpoint and not raw.startswith(old_tail):
                                raise CowrieJournalError("POSITION_CONFLICT")
                            if saved >= limits.max_records or size + len(raw) > limits.max_bytes:
                                return CowrieProgress("limited", saved, size, "STEP_LIMIT")
                            stream.check()
                            self.repository.append(source.journal_id, offset, raw)
                            saved += 1
                            size += len(raw)
                        offset += len(raw)
                        continue
                    if len(buffer) > 65536:
                        raise CowrieJournalError("RECORD_LIMIT")
                    if read == stream.size:
                        break
                    part = stream.read(min(4096, stream.size - read, 65537 - len(buffer)))
                    if not part:
                        raise CowrieJournalError("SOURCE_TRUNCATED")
                    buffer.extend(part)
                    read += len(part)
                    stream.check()
                if offset < checkpoint:
                    raise CowrieJournalError("POSITION_CONFLICT")
                stream.check()
                self.repository.tail(source.journal_id, bytes(buffer))
            return CowrieProgress(
                "progress" if saved else "waiting",
                saved,
                size,
                "INCOMPLETE_LINE" if buffer else None,
            )
        except CowrieJournalError as error:
            code = error.code
            if registered and code in {
                "POSITION_CONFLICT",
                "SOURCE_TRUNCATED",
                "SOURCE_REPLACED",
                "GENERATION_CONFLICT",
                "RECORD_LIMIT",
                "UNSAFE_PATH",
            }:
                self.repository.block(source.journal_id, code)
            return CowrieProgress(
                "limited" if code == "STORAGE_LIMIT" else "error", saved, size, code
            )
        except FileNotFoundError:
            return CowrieProgress("waiting", saved, size, "FILE_MISSING")
        except OSError as error:
            unsafe = error.errno in {errno.ELOOP, errno.ENOTDIR}
            if registered and unsafe:
                self.repository.block(source.journal_id, "UNSAFE_PATH")
            return CowrieProgress("error", saved, size, "UNSAFE_PATH" if unsafe else "IO_ERROR")
        except sqlite3.Error:
            return CowrieProgress("retry", saved, size, "DB_ERROR")
