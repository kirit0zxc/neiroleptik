"""Правила операций и проверка фактического результата без доступа к транспорту."""

from honeyforge.api.command_schemas import CommandAck
from honeyforge.common import DomainError
from honeyforge.models import Command, Deployment, ProfileRevision


class CommandPolicy:
    """Успешный HTTP-ответ агента ещё должен соответствовать смыслу операции."""

    @staticmethod
    def validate_action(action: str, deployment: Deployment, revision: ProfileRevision) -> None:
        states = {
            "start": {"planned", "stopped", "failed"},
            "stop": {"running", "failed", "unknown"},
            "reset": {"running", "failed"},
        }
        if deployment.state not in states[action]:
            raise DomainError(409, "Команда недоступна в текущем состоянии", "INVALID_STATE")
        if action == "reset" and revision.config["interaction_level"] != "high":
            raise DomainError(409, "reset разрешён только для High", "UNSUPPORTED_COMMAND")

    @staticmethod
    def validate_ack(
        command: Command, deployment: Deployment, revision: ProfileRevision, payload: CommandAck
    ) -> None:
        if payload.deployment_id != deployment.id:
            raise DomainError(409, "ACK относится к другому deployment", "ACK_MISMATCH")
        if payload.generation != command.generation or deployment.generation != command.generation:
            raise DomainError(409, "Поколение команды устарело", "STALE_GENERATION")
        if payload.applied_revision not in {None, revision.revision}:
            raise DomainError(409, "Применённая ревизия не назначена этой ловушке", "ACK_MISMATCH")
        if command.action == "stop" and not command.reconcile:
            applied = revision.revision if deployment.applied_revision_id else None
            if payload.instance_id != deployment.instance_id or payload.applied_revision != applied:
                raise DomainError(409, "stop не меняет идентичность и ревизию", "ACK_MISMATCH")
        if (
            payload.outcome == "failed"
            and command.action != "stop"
            and payload.runtime_state == "running"
            and (
                payload.instance_id != command.expected_instance_id
                or command.expected_instance_id is None
            )
        ):
            raise DomainError(409, "Ошибка запуска не подтверждает новый running", "ACK_MISMATCH")
        if payload.outcome == "succeeded":
            if command.action == "stop":
                if payload.runtime_state != "stopped":
                    raise DomainError(409, "Успешный stop требует stopped", "ACK_MISMATCH")
            elif (
                payload.runtime_state != "running"
                or payload.applied_revision != revision.revision
                or payload.instance_id is None
                or payload.instance_id == command.expected_instance_id
            ):
                raise DomainError(
                    409, "Успешный start/reset требует новый работающий экземпляр", "ACK_MISMATCH"
                )
