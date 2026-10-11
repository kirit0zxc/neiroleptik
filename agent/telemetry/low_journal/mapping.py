"""Граница будущего согласованного преобразования; production адаптера пока нет."""

from typing import Protocol

from agent.telemetry.schemas import Event

from .models import JournalBinding, JournalRecord


class RawEventMapper(Protocol):
    """Результат требует отдельного атомарного ledger конверсии, независимо от ACK."""

    def convert(self, binding: JournalBinding, record: JournalRecord) -> Event | None:
        """Служебная запись не создаёт Event; mapping определяется владельцами."""
        ...
