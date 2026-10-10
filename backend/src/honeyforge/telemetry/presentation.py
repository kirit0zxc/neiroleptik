"""Публичная копия отделена от неизменяемого оригинала и внутренней записи."""

from collections.abc import Callable
from copy import deepcopy
from typing import Any

from honeyforge.telemetry.read_contract import PublishedEvent

PUBLIC_FIELDS = frozenset(
    {
        "schema_version",
        "event_id",
        "agent_id",
        "deployment_id",
        "instance_id",
        "session_id",
        "source_seq",
        "occurred_at",
        "type",
        "source",
        "service",
        "data",
        "artifact_ids",
        "profile_revision",
    }
)


class PublicEventPresenter:
    """Без явно переданной политики нельзя случайно выдать оригинальные secrets."""

    def __init__(self, policy: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
        self.policy = policy

    def present(self, record: PublishedEvent) -> dict[str, Any]:
        public = deepcopy({k: v for k, v in record.event.items() if k in PUBLIC_FIELDS})
        public.setdefault("session_id", None)
        public["received_at"] = record.received_at.isoformat()
        return self.policy(public)
