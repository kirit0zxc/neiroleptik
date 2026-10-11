"""Ограниченный проход доверенного источника без изменения файлов Low."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .durable import RawJournalError, RawJournalRepository
from .models import JournalBinding, JournalReadError
from .reader import LowJournalReader


@dataclass(frozen=True)
class CollectionLimits:
    """Бюджет включает повторное чтение префиксов, а не только новые строки."""

    max_files: int = 64
    max_records: int = 1000
    max_bytes: int = 4 * 1024 * 1024
    max_scan_bytes: int = 64 * 1024 * 1024
    max_line_bytes: int = 65536

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or value < 1
            for value in (
                self.max_files,
                self.max_records,
                self.max_bytes,
                self.max_scan_bytes,
                self.max_line_bytes,
            )
        ):
            raise ValueError("Все лимиты должны быть положительными целыми")


@dataclass(frozen=True)
class CollectionResult:
    """Безопасный результат шага; не подтверждает преобразование или доставку."""

    state: Literal["progress", "waiting", "limited", "error"]
    saved_records: int = 0
    saved_bytes: int = 0
    code: str | None = None
    retry_in: float = 1.0


class LowJournalCollector:
    """Синхронный шаг одного источника; остановка возможна между шагами."""

    def __init__(self, repository: RawJournalRepository, binding: JournalBinding) -> None:
        self.repository = repository
        self.binding = binding

    def step(
        self, paths: Sequence[Path], limits: CollectionLimits | None = None
    ) -> CollectionResult:
        """Сначала проверяет headers всей выборки, затем сохраняет цепочку по порядку."""
        limits = limits or CollectionLimits()
        saved = size = scanned = 0

        def result(
            state: Literal["progress", "waiting", "limited", "error"], code: str | None = None
        ) -> CollectionResult:
            return CollectionResult(state, saved, size, code)

        if len(paths) > limits.max_files:
            return result("limited", "FILE_LIMIT")
        reader = LowJournalReader(max_line_bytes=limits.max_line_bytes)
        generations: dict[str, tuple[dict[str, Any], bytes, list[Path]]] = {}
        try:
            for path in paths:
                if scanned + limits.max_line_bytes + 1 > limits.max_scan_bytes:
                    return result("limited", "REPLAY_LIMIT")
                with reader.open(path, self.binding) as stream:
                    first = next(stream, None)
                    scanned += stream.bytes_read
                    if first is None:
                        if self.repository.known_position(self.binding, path):
                            return result("error", "SOURCE_TRUNCATED")
                        return result("waiting", "INCOMPLETE_HEADER")
                    existing = generations.get(first.journal_id)
                    if existing is not None:
                        if existing[1] != first.raw:
                            return result("error", "POSITION_CONFLICT")
                        existing[2].append(path)
                    else:
                        generations[first.journal_id] = (first.document(), first.raw, [path])
            ordered = sorted(generations.values(), key=lambda item: item[0]["journal_seq"])
            previous_id = None
            previous_seq = 0
            # Проверка всей топологии до INSERT не допускает частичного выбора ветви.
            successors: set[str] = set()
            sequences: set[int] = set()
            for header, _, _ in ordered:
                predecessor = header["previous_journal_id"]
                seq = header["journal_seq"]
                if seq in sequences or (predecessor is not None and predecessor in successors):
                    return result("error", "CHAIN_BRANCH")
                sequences.add(seq)
                if predecessor is not None:
                    successors.add(predecessor)
                visited = {header["journal_id"]}
                cursor = predecessor
                while cursor in generations:
                    if cursor in visited:
                        return result("error", "CHAIN_CYCLE")
                    visited.add(cursor)
                    cursor = generations[cursor][0]["previous_journal_id"]
            for header, _, _ in ordered:
                predecessor = header["previous_journal_id"]
                if predecessor in generations and (
                    header["journal_seq"] != generations[predecessor][0]["journal_seq"] + 1
                ):
                    return result("error", "CHAIN_SEQUENCE")
                if header["journal_seq"] != previous_seq + 1:
                    return result("waiting", "MISSING_PREDECESSOR")
                if header["previous_journal_id"] != previous_id:
                    code = (
                        "MISSING_PREDECESSOR"
                        if header["previous_journal_id"] not in generations
                        else "CHAIN_SEQUENCE"
                    )
                    return result("waiting" if code == "MISSING_PREDECESSOR" else "error", code)
                previous_id = header["journal_id"]
                previous_seq = header["journal_seq"]
            last_seq = 0
            for index, (header, header_raw, copies) in enumerate(ordered):
                if header["first_seq"] <= last_seq:
                    return result("error", "CHAIN_SEQUENCE")
                for path in copies:
                    checkpoint = self.repository.checkpoint(self.binding, header["journal_id"])
                    end = 0
                    closed = False
                    with reader.open(path, self.binding) as stream:
                        while True:
                            # Резерв учитывает буфер reader до limit + 1.
                            if scanned + limits.max_line_bytes + 1 > limits.max_scan_bytes:
                                return result("limited", "REPLAY_LIMIT")
                            before_read = stream.bytes_read
                            record = next(stream, None)
                            scanned += stream.bytes_read - before_read
                            if record is None:
                                closed = stream.summary.status == "closed"
                                break
                            if record.journal_id != header["journal_id"]:
                                return result("error", "SOURCE_CHANGED")
                            if record.kind == "header" and record.raw != header_raw:
                                return result("error", "SOURCE_CHANGED")
                            end = record.next_offset
                            if record.offset >= checkpoint:
                                if (
                                    saved >= limits.max_records
                                    or size + len(record.raw) > limits.max_bytes
                                ):
                                    return result("limited", "STEP_LIMIT")
                            inserted = self.repository.append(self.binding, record, path=path)
                            if inserted:
                                saved += 1
                                size += len(record.raw)
                            document = record.document()
                            if record.kind == "footer":
                                last_seq = document["last_seq"]
                        if end < checkpoint:
                            return result("error", "SOURCE_TRUNCATED")
                    if not closed and index < len(ordered) - 1:
                        return result("waiting", "OPEN_PREDECESSOR")
            return result("progress" if saved else "waiting")
        except JournalReadError as error:
            return result("waiting" if error.code == "IO_ERROR" else "error", error.code)
        except RawJournalError as error:
            return result("limited" if error.code == "STORAGE_LIMIT" else "error", error.code)
