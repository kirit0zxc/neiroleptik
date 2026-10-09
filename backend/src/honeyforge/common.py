"""Общие ошибки и время; без зависимостей от конкретного модуля управления."""

from datetime import UTC, datetime


class DomainError(Exception):
    """Ожидаемая ошибка операции, переводимая API в согласованный HTTP-ответ."""

    def __init__(self, status: int, detail: str, code: str | None = None) -> None:
        self.status = status
        self.detail = detail
        self.code = code
        super().__init__(detail)


def utcnow() -> datetime:
    """Возвращает timezone-aware время для записей БД и сроков действия."""
    return datetime.now(UTC)
