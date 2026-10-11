"""Внутренние типы для уже подготовленных событий; сетевой валидации здесь нет."""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

BIGINT_MAX = 2**63 - 1


@dataclass(frozen=True)
class PreparedEvent:
    """Байты — единственный оригинал; проекция извлекается, а не дублируется."""

    index: int
    agent_id: UUID
    event_id: UUID
    deployment_id: UUID
    instance_id: UUID
    profile_revision: int
    canonical_bytes: bytes

    def __post_init__(self) -> None:
        if type(self.index) is not int or self.index < 0:
            raise ValueError("Некорректный исходный index")
        object.__setattr__(self, "canonical_bytes", bytes(self.canonical_bytes))
        if type(self.profile_revision) is not int or self.profile_revision < 1:
            raise ValueError("Некорректная revision")
        original = self.original()
        for name in ("agent_id", "event_id", "deployment_id", "instance_id"):
            if str(getattr(self, name)) != original[name]:
                raise ValueError("Идентичность не соответствует оригиналу")
        if type(original["profile_revision"]) is not int or (
            original["profile_revision"] != self.profile_revision
        ):
            raise ValueError("Revision не соответствует оригиналу")
        if self.occurred_at.tzinfo is None:
            raise ValueError("Время оригинала должно содержать timezone")

    def original(self) -> dict[str, Any]:
        """Каждый вызов выдаёт отдельный объект, не изменяя готовые байты."""
        value: dict[str, Any] = json.loads(self.canonical_bytes)
        return value

    @property
    def occurred_at(self) -> datetime:
        return datetime.fromisoformat(self.original()["occurred_at"])


@dataclass(frozen=True)
class WriteResult:
    """Принятие возвращается только после commit, конфликт не меняет оригинал."""

    index: int
    agent_id: UUID
    event_id: UUID
    status: Literal["accepted", "EVENT_ID_CONFLICT"]


@dataclass(frozen=True)
class HistoryKey:
    """Исключительная нижняя граница сортировки DESC."""

    received_at: datetime
    event_id: UUID
    agent_id: UUID


@dataclass(frozen=True)
class EventFilters:
    """Нормализованные фильтры внутреннего источника, без HTTP-парсинга."""

    deployment_id: UUID | None = None
    from_time: datetime | None = None
    to_time: datetime | None = None
    source_ip: str | None = None
    event_type: str | None = None
    session_id: UUID | None = None
    timeline: bool = False


@dataclass(frozen=True)
class StoredEvent:
    """Внутренняя запись; не сериализовать автоматически как публичный DTO."""

    event_pk: int
    agent_id: UUID
    event_id: UUID
    received_at: datetime
    stream_seq: int
    canonical_bytes: bytes

    def original(self) -> dict[str, Any]:
        value: dict[str, Any] = json.loads(self.canonical_bytes)
        return value

    @property
    def history_key(self) -> HistoryKey:
        return HistoryKey(self.received_at, self.event_id, self.agent_id)
