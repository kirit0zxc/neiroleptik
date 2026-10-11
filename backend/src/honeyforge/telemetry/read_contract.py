"""Контракт коротких read-only запросов к committed журналу публикаций."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True)
class HistoryFilters:
    deployment_id: str | None = None
    source_ip: str | None = None
    event_type: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    session_id: str | None = None
    sort: str = "received_at"
    order: str = "desc"

    def normalized(self) -> dict[str, str | None]:
        return {
            "deployment_id": self.deployment_id,
            "source_ip": self.source_ip,
            "type": self.event_type,
            "from": self.start.isoformat() if self.start else None,
            "to": self.end.isoformat() if self.end else None,
            "session_id": self.session_id,
            "sort": self.sort,
            "order": self.order,
        }


@dataclass(frozen=True)
class PublishedEvent:
    seq: int
    received_at: datetime
    event: dict[str, Any]

    @property
    def key(self) -> tuple[datetime, str, str]:
        return self.received_at, self.event["event_id"], self.event["agent_id"]


class PublicationReader(Protocol):
    """Каждый вызов завершает Session до возврата; pending/rollback не видны.

    history возвращает не более limit строк, key DESC, seq<=watermark;
    stream возвращает не более limit строк в (after,until], seq ASC.
    Нижняя retention граница — последний уже невосстановимый курсор,
    None означает отсутствие доказанной очистки, промежутки seq допустимы.
    """

    def watermark(self) -> int: ...
    def retention_floor(self) -> int | None: ...
    def history(
        self,
        filters: HistoryFilters,
        watermark: int,
        before: tuple[datetime, str, str] | None,
        limit: int,
    ) -> list[PublishedEvent]: ...
    def stream(self, after: int, until: int, limit: int) -> list[PublishedEvent]: ...
