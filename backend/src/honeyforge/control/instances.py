"""Проверка повторного использования UUID защищает идентичность телеметрии."""

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from honeyforge.api.command_schemas import CommandAck
from honeyforge.common import DomainError
from honeyforge.models import Command, RuntimeInstance


class InstanceRegistry:
    """Уникальность защищена PostgreSQL, включая ACK с разных runtime-узлов."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def record(self, command: Command, payload: CommandAck) -> None:
        if payload.instance_id is None:
            return
        created = self.session.scalar(
            insert(RuntimeInstance)
            .values(
                id=payload.instance_id,
                deployment_id=command.deployment_id,
                profile_revision_id=command.profile_revision_id,
            )
            .on_conflict_do_nothing(index_elements=[RuntimeInstance.id])
            .returning(RuntimeInstance.id)
        )
        if created is not None:
            return
        existing = self.session.get(RuntimeInstance, payload.instance_id, populate_existing=True)
        if existing is None:
            raise RuntimeError("Конфликт UUID без исторической записи")
        if existing.deployment_id != command.deployment_id:
            raise DomainError(409, "Экземпляр принадлежит другой ловушке", "INSTANCE_CONFLICT")
        if command.action in {"start", "reset"} and (
            payload.outcome == "succeeded" or payload.instance_id != command.expected_instance_id
        ):
            raise DomainError(409, "UUID экземпляра уже использован ранее", "INSTANCE_REUSED")
