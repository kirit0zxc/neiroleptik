"""Файловый журнал Low: поколения, ротация, квота и концовка (specs/low-journal-v1.md).

Журнал — буфер между ловушкой и сборщиком телеметрии, а не устойчивое хранилище.
Один экземпляр Low пишет собственный набор файлов; закрытые файлы не изменяются, а
удалять обработанные файлы может только сборщик. Класс рассчитан на один поток
(цикл событий asyncio): блокировок нет.
"""

from __future__ import annotations

import errno
import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO
from uuid import UUID, uuid4

SCHEMA = "honeyforge-low-journal/1"
MIN_FILE_BYTES = 256 * 1024
DEFAULT_FILE_BYTES = 8 * 1024 * 1024
DEFAULT_QUOTA_BYTES = 256 * 1024 * 1024
# Запас под концовку, чтобы закрытие файла никогда не упиралось в его предельный размер.
_FOOTER_RESERVE = 512
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_NAME = re.compile(rf"low-({_UUID})-(\d{{6,}})\.jsonl")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _canonical_uuid(value: str, name: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, AttributeError, TypeError) as error:
        raise ValueError(f"{name} должен быть UUID") from error


@dataclass(frozen=True)
class JournalSettings:
    """Параметры журнала экземпляра; каталог должен существовать и быть доступен на запись."""

    directory: Path
    instance_id: str
    deployment_id: str
    profile_revision: int
    max_file_bytes: int = DEFAULT_FILE_BYTES
    max_total_bytes: int = DEFAULT_QUOTA_BYTES
    fsync_each_event: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "directory", Path(self.directory))
        object.__setattr__(self, "instance_id", _canonical_uuid(self.instance_id, "instance_id"))
        object.__setattr__(
            self, "deployment_id", _canonical_uuid(self.deployment_id, "deployment_id")
        )
        if type(self.profile_revision) is not int or self.profile_revision < 1:
            raise ValueError("profile_revision должен быть целым не меньше 1")
        if type(self.max_file_bytes) is not int or self.max_file_bytes < MIN_FILE_BYTES:
            raise ValueError(f"max_file_bytes должен быть целым не меньше {MIN_FILE_BYTES}")
        if type(self.max_total_bytes) is not int or self.max_total_bytes < self.max_file_bytes:
            raise ValueError("max_total_bytes должен быть целым не меньше max_file_bytes")


class RotatingJournalSink:
    """Пишет заголовок, события и концовку; сама решает, когда начать новое поколение.

    Ошибка записи не скрывается: исключение уходит вызывающей стороне (LowService в этом
    случае закрывает соединение без ответа), а файл с неподтверждённой записью закрывается
    без концовки. Следующая запись открывает новое поколение.
    """

    def __init__(
        self,
        settings: JournalSettings,
        clock: Callable[[], datetime] = _utc_now,
        new_journal_id: Callable[[], str] = lambda: str(uuid4()),
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._new_journal_id = new_journal_id
        self._file: BinaryIO | None = None
        self._current_name: str | None = None
        self._journal_id: str | None = None
        self._previous_id: str | None = None
        self._journal_seq = 0
        self._size = 0
        self._total = 0
        self._first_seq = 1
        self._last_seq = 0

    @property
    def current_journal_id(self) -> str | None:
        """Идентификатор открытого поколения либо None, если файл сейчас закрыт."""
        return self._journal_id if self._file is not None else None

    def open(self) -> None:
        """Создаёт первый файл сразу, чтобы недоступный каталог обнаруживался при запуске."""
        if self._file is None:
            self._open_next(self._last_seq + 1)

    def emit(self, event: Mapping[str, object]) -> None:
        """Записывает событие; при необходимости выполняет ротацию и проверяет квоту."""
        seq = event.get("seq")
        if type(seq) is not int or seq < 1:
            raise ValueError("событие журнала требует целое поле seq не меньше 1")
        data = self._encode({"record": "event", **event})
        if len(data) + _FOOTER_RESERVE > self._settings.max_file_bytes // 2:
            raise ValueError("запись журнала слишком велика")
        if self._file is None:
            self._open_next(seq)
        elif self._size + len(data) + _FOOTER_RESERVE > self._settings.max_file_bytes:
            self._close_current("rotation")
            self._open_next(seq)
        self._ensure_quota(len(data))
        self._write(data)
        self._last_seq = seq

    def close(self, reason: str = "shutdown") -> None:
        """Закрывает файл концовкой; повторный вызов безопасен."""
        self._close_current(reason)

    # --- внутренние операции -------------------------------------------------------

    def _encode(self, record: Mapping[str, object]) -> bytes:
        text = json.dumps(record, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
        return (text + "\n").encode("ascii")

    def _timestamp(self) -> str:
        return self._clock().astimezone(UTC).isoformat().replace("+00:00", "Z")

    def _write(self, data: bytes) -> None:
        file = self._file
        assert file is not None
        try:
            file.write(data)
            file.flush()
            if self._settings.fsync_each_event:
                os.fsync(file.fileno())
        except OSError:
            self._abandon()
            raise
        self._size += len(data)
        self._total += len(data)

    def _abandon(self) -> None:
        """Закрывает файл без концовки: его хвост мог быть записан не полностью."""
        file, self._file = self._file, None
        self._current_name = None
        self._previous_id = self._journal_id
        if file is not None:
            try:
                file.close()
            except OSError:
                pass

    def _scan(self, exclude: str | None = None) -> tuple[int, int]:
        """Возвращает размер файлов журнала в каталоге и последний номер своего экземпляра.

        Размер читается свежим os.stat, а не из записи каталога: на Windows запись
        каталога для открытого на запись файла отстаёт от фактического размера. Файл,
        который пишется сейчас, исключается по имени: его размер известен точно.
        """
        total = 0
        newest = 0
        with os.scandir(self._settings.directory) as entries:
            for entry in entries:
                match = _NAME.fullmatch(entry.name)
                if match is None or not entry.is_file(follow_symlinks=False):
                    continue
                if match.group(1) == self._settings.instance_id:
                    newest = max(newest, int(match.group(2)))
                if entry.name != exclude:
                    total += os.stat(entry.path, follow_symlinks=False).st_size
        return total, newest

    def _ensure_quota(self, extra: int) -> None:
        if self._total + extra <= self._settings.max_total_bytes:
            return
        # Сборщик мог удалить обработанные файлы: уточняем размер по каталогу.
        others, _ = self._scan(exclude=self._current_name)
        self._total = others + (self._size if self._file is not None else 0)
        if self._total + extra > self._settings.max_total_bytes:
            raise OSError(errno.ENOSPC, "квота журнала исчерпана")

    def _open_next(self, first_seq: int) -> None:
        total, newest = self._scan()
        self._journal_seq = max(self._journal_seq, newest) + 1
        journal_id = self._new_journal_id()
        header = self._encode(
            {
                "record": "header",
                "schema": SCHEMA,
                "journal_id": journal_id,
                "journal_seq": self._journal_seq,
                "previous_journal_id": self._previous_id,
                "instance_id": self._settings.instance_id,
                "deployment_id": self._settings.deployment_id,
                "profile_revision": self._settings.profile_revision,
                "opened_at": self._timestamp(),
                "first_seq": first_seq,
            }
        )
        if total + len(header) > self._settings.max_total_bytes:
            raise OSError(errno.ENOSPC, "квота журнала исчерпана")
        name = f"low-{self._settings.instance_id}-{self._journal_seq:06d}.jsonl"
        path = self._settings.directory / name
        # O_EXCL: существующий файл никогда не перезаписывается.
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        descriptor = os.open(path, flags, 0o640)
        file = os.fdopen(descriptor, "wb")
        try:
            file.write(header)
            file.flush()
        except OSError:
            file.close()
            try:
                path.unlink()
            except OSError:
                pass
            raise
        self._file = file
        self._current_name = name
        self._journal_id = journal_id
        self._size = len(header)
        self._total = total + len(header)
        self._first_seq = first_seq
        self._last_seq = first_seq - 1

    def _close_current(self, reason: str) -> None:
        file = self._file
        if file is None:
            return
        footer = self._encode(
            {
                "record": "footer",
                "journal_id": self._journal_id,
                "last_seq": self._last_seq,
                "closed_at": self._timestamp(),
                "reason": reason,
            }
        )
        try:
            file.write(footer)
            file.flush()
            os.fsync(file.fileno())
        except OSError:
            # Без концовки файл считается оборванным; события в нём уже записаны.
            pass
        finally:
            self._file = None
            self._current_name = None
            self._previous_id = self._journal_id
            try:
                file.close()
            except OSError:
                pass
