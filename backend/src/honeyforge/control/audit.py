"""Добавление аудита в ту же транзакцию, что и бизнес-операция."""

from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from honeyforge.models import AuditEvent


class AuditService:
    """Вызывающий сервис передаёт только разрешённые несекретные поля."""

    @staticmethod
    def add(
        session: Session,
        action: str,
        actor: UUID | None = None,
        target: UUID | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        session.add(
            AuditEvent(action=action, actor_id=actor, target_id=target, details=details or {})
        )
