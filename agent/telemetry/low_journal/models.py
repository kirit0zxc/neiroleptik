"""Неизменяемые результаты чтения и диагностика без содержимого источника."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal

RecordKind = Literal["header", "event", "footer"]
JournalStatus = Literal["closed", "open", "incomplete"]
ErrorCode = Literal[
    "INVALID_RECORD",
    "UNSUPPORTED_SCHEMA",
    "IDENTITY_MISMATCH",
    "LINE_TOO_LONG",
    "INVALID_SEQUENCE",
    "INVALID_STRUCTURE",
    "UNSAFE_PATH",
    "SOURCE_CHANGED",
    "IO_ERROR",
]
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def is_uuid(value: object) -> bool:
    """Принимает каноническую UUID-строку без нормализации недоверенного ввода."""
    return isinstance(value, str) and _UUID.fullmatch(value) is not None


@dataclass(frozen=True, slots=True)
class JournalBinding:
    """Явная доверенная привязка; содержимое header не определяет её значения."""

    deployment_id: str
    instance_id: str
    profile_revision: int

    def __post_init__(self) -> None:
        if (
            not is_uuid(self.deployment_id)
            or not is_uuid(self.instance_id)
            or type(self.profile_revision) is not int
            or self.profile_revision < 1
        ):
            raise ValueError("Нужны канонические UUID и целая ревизия не меньше 1")


@dataclass(frozen=True, slots=True)
class JournalRecord:
    """Точная физическая строка; изменяемый документ каждый раз создаётся заново."""

    kind: RecordKind
    journal_id: str
    offset: int
    next_offset: int
    raw: bytes

    def document(self) -> dict[str, Any]:
        """Возвращает независимую копию уже проверенного JSON, включая вложенные объекты."""
        result: dict[str, Any] = json.loads(self.raw)
        return result


@dataclass(frozen=True, slots=True)
class JournalSummary:
    """Итог исчерпанного снимка; не checkpoint, ACK или разрешение удалить файл."""

    status: JournalStatus
    next_offset: int
    journal_id: str | None


class JournalReadError(ValueError):
    """Стабильный код и позиция без путей, payload и исходных сообщений исключений."""

    def __init__(self, code: ErrorCode, offset: int | None) -> None:
        self.code = code
        self.offset = offset
        super().__init__(code, offset)
