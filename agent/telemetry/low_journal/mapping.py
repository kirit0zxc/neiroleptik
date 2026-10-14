"""Протокол конверсии: рабочая реализация находится в production.LowProductionMapper."""

from typing import Protocol

from agent.telemetry.schemas import Event

from .models import JournalBinding, JournalRecord


class RawEventMapper(Protocol):
    """Результат требует отдельного атомарного ledger конверсии, независимо от ACK."""

    def convert(self, binding: JournalBinding, record: JournalRecord) -> Event | None:
        """Служебная запись не создаёт Event; атомарность обеспечивает внешний ledger."""
        ...
