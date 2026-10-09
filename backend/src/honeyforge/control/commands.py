"""Долговечные команды: выдача lease, повторная доставка и атомарный итоговый ACK."""

import hashlib
import hmac
import json
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from honeyforge.api.command_schemas import (
    AgentCommand,
    CommandAccepted,
    CommandAck,
    CommandClaimRead,
    CommandCreate,
    JobRead,
    LeaseInput,
    LeaseRead,
)
from honeyforge.api.schemas import AgentProfileConfig
from honeyforge.common import DomainError, utcnow
from honeyforge.config import Settings
from honeyforge.control.audit import AuditService
from honeyforge.control.command_policy import CommandPolicy
from honeyforge.control.instances import InstanceRegistry
from honeyforge.control.profiles import ProfileService
from honeyforge.control.registry import RegistryService
from honeyforge.models import Command, Deployment, Host, Profile, ProfileRevision, User

ACTIVE = {"requested", "leased", "running"}
LEASE_SECONDS = 60
DELIVERY_SECONDS = 300


class CommandService:
    """Блокировки идут host → deployment; это также сериализует операции с revoke."""

    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.registry = RegistryService(session, settings)
        self.profiles = ProfileService(session, settings)

    def lock_host(self, host_id: UUID) -> Host:
        host = self.registry.get(host_id, lock=True)
        if host.revoked_at is not None:
            raise DomainError(401, "Agent token отозван", "AGENT_REVOKED")
        return host

    def lock_deployment(self, deployment_id: UUID) -> Deployment:
        item = self.session.scalar(
            select(Deployment)
            .where(Deployment.id == deployment_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if item is None:
            raise DomainError(404, "Назначение не найдено")
        return item

    def context(self, command_id: UUID, agent: Host | None = None) -> tuple[Command, Deployment]:
        command = self.session.get(Command, command_id)
        if command is None or (agent is not None and command.host_id != agent.id):
            raise DomainError(404, "Команда не найдена")
        self.lock_host(command.host_id)
        deployment = self.lock_deployment(command.deployment_id)
        self.session.refresh(command)
        return command, deployment

    def revision(self, command: Command) -> ProfileRevision:
        revision = self.session.get(ProfileRevision, command.profile_revision_id)
        if revision is None:
            raise RuntimeError("Нарушена связь команды с ревизией")
        return revision

    @staticmethod
    def read(command: Command) -> JobRead:
        return JobRead.model_validate(
            {
                "job_id": command.id,
                "deployment_id": command.deployment_id,
                "command": command.action,
                "state": command.state,
                "error": command.error,
                "error_code": command.error_code,
                "generation": command.generation,
                "created_at": command.created_at,
                "completed_at": command.completed_at,
            }
        )

    def expire(self, command: Command, deployment: Deployment, now: datetime) -> None:
        """Неподтверждённое выполнение не превращается в выдуманный stopped/failed."""
        if command.state not in ACTIVE:
            return
        deadline = command.execution_expires_at or command.delivery_expires_at
        if deadline <= now:
            command.state = "expired"
            command.completed_at = now
            command.error_code = (
                "EXECUTION_EXPIRED" if command.lease_version else "DELIVERY_EXPIRED"
            )
            command.error = (
                "Истёк срок выполнения" if command.lease_version else "Агент не взял команду"
            )
            if command.lease_version:
                deployment.state = "unknown"
            AuditService.add(
                self.session,
                "command.expired",
                target=command.id,
                details={"code": command.error_code},
            )
        elif command.lease_expires_at is not None and command.lease_expires_at <= now:
            deployment.state = "unknown"

    def active(self, deployment_id: UUID) -> Command | None:
        return self.session.scalar(
            select(Command).where(Command.deployment_id == deployment_id, Command.state.in_(ACTIVE))
        )

    def refresh(self, deployment_id: UUID) -> None:
        """Чтение состояний актуализирует сроки без отдельного фонового процесса."""
        command = self.active(deployment_id)
        if command is not None:
            # Читатель может видеть отозванный узел; выполнение на нём запрещено отдельно.
            self.registry.get(command.host_id, lock=True)
            deployment = self.lock_deployment(deployment_id)
            self.session.refresh(command)
            self.expire(command, deployment, utcnow())
            self.session.commit()

    def create(self, deployment_id: UUID, payload: CommandCreate, actor: User) -> CommandAccepted:
        item = self.session.get(Deployment, deployment_id)
        if item is None:
            raise DomainError(404, "Назначение не найдено")
        host = self.registry.get(item.host_id, lock=True)
        item = self.lock_deployment(deployment_id)
        previous = self.session.scalar(
            select(Command).where(
                Command.deployment_id == item.id, Command.idempotency_key == payload.idempotency_key
            )
        )
        if previous is not None:
            if (
                previous.action != payload.command
                or previous.expected_generation != payload.expected_generation
            ):
                raise DomainError(409, "Ключ использован с другим запросом", "IDEMPOTENCY_CONFLICT")
            return CommandAccepted(job_id=previous.id)
        if host.revoked_at is not None:
            raise DomainError(409, "Узел отозван", "AGENT_REVOKED")
        active = self.active(item.id)
        now = utcnow()
        if active is not None:
            self.expire(active, item, now)
            self.session.flush()
            if active.state in ACTIVE:
                raise DomainError(409, "У ловушки уже есть незавершённая команда", "COMMAND_BUSY")
        if payload.expected_generation != item.generation:
            raise DomainError(409, "Перечитайте поколение ловушки", "STALE_GENERATION")
        revision = self.session.get(ProfileRevision, item.profile_revision_id)
        if revision is None:
            raise RuntimeError("Нарушена связь назначения с ревизией")
        CommandPolicy.validate_action(payload.command, item, revision)
        if payload.command != "stop":
            profile = self.session.get(Profile, revision.profile_id)
            if profile is None or profile.archived:
                raise DomainError(409, "Архивный профиль нельзя запускать", "PROFILE_ARCHIVED")
            if revision.config["interaction_level"] not in host.capabilities:
                raise DomainError(
                    409, "Узел не поддерживает уровень профиля", "UNSUPPORTED_PROFILE"
                )
        item.generation += 1
        command = Command(
            deployment_id=item.id,
            host_id=item.host_id,
            profile_revision_id=item.profile_revision_id,
            idempotency_key=payload.idempotency_key,
            action=payload.command,
            expected_generation=payload.expected_generation,
            generation=item.generation,
            expected_instance_id=item.instance_id,
            reconcile=item.state == "unknown",
            delivery_expires_at=now + timedelta(seconds=DELIVERY_SECONDS),
        )
        self.session.add(command)
        self.session.flush()
        AuditService.add(
            self.session,
            "command.requested",
            actor.id,
            command.id,
            {"generation": command.generation, "command": command.action},
        )
        self.session.commit()
        return CommandAccepted(job_id=command.id)

    def get(self, command_id: UUID) -> JobRead:
        # Пользовательский read не требует действующий agent token узла.
        command = self.session.get(Command, command_id)
        if command is None:
            raise DomainError(404, "Задание не найдено")
        self.refresh(command.deployment_id)
        self.session.refresh(command)
        return self.read(command)

    def token(self, command: Command) -> str:
        """HMAC воспроизводим после потери ответа и рестарта, raw token в БД не хранится."""
        value = f"{command.host_id}:{command.id}:{command.lease_version}".encode()
        digest = hmac.new(
            self.settings.session_secret.get_secret_value().encode(), value, hashlib.sha256
        ).hexdigest()
        return "hf_lease_" + digest

    def claim(self, agent: Host, command_id: UUID | None) -> CommandClaimRead:
        self.lock_host(agent.id)
        commands = list(
            self.session.scalars(
                select(Command)
                .where(Command.host_id == agent.id, Command.state.in_(ACTIVE))
                .order_by(Command.created_at, Command.id)
            )
        )
        now = utcnow()
        for candidate in commands:
            self.expire(candidate, self.lock_deployment(candidate.deployment_id), now)
        self.session.flush()
        active = [item for item in commands if item.state in ACTIVE]
        leased = next((item for item in active if item.lease_version > 0), None)
        if command_id is not None:
            command = self.session.get(Command, command_id)
            if command is None or command.host_id != agent.id:
                raise DomainError(404, "Команда не найдена")
            if command.state not in ACTIVE:
                raise DomainError(409, "Команда уже завершена", "COMMAND_FINISHED")
            if leased is not None and leased.id != command.id:
                raise DomainError(409, "Узел выполняет другую команду", "HOST_BUSY")
        else:
            command = leased or next(iter(active), None)
        if command is None:
            self.session.commit()
            return CommandClaimRead(command=None)
        deployment = self.lock_deployment(command.deployment_id)
        if command.lease_expires_at is None or command.lease_expires_at <= now:
            command.lease_version += 1
            if command.execution_expires_at is None:
                duration = (
                    900 if self.revision(command).config["interaction_level"] == "high" else 300
                )
                command.execution_expires_at = now + timedelta(seconds=duration)
            command.lease_expires_at = min(
                now + timedelta(seconds=LEASE_SECONDS), command.execution_expires_at
            )
            command.state = "leased"
            # При повторной выдаче факт работы всё ещё неизвестен до финального ACK.
            if deployment.state != "unknown":
                deployment.state = "stopping" if command.action == "stop" else "deploying"
        revision = self.revision(command)
        config = AgentProfileConfig.model_validate(
            {
                **revision.config,
                "credentials": self.profiles.security.decrypt_credentials(
                    revision.credentials_ciphertext
                ),
            }
        )
        result = AgentCommand.model_validate(
            {
                "command_id": command.id,
                "deployment_id": deployment.id,
                "action": command.action,
                "profile_id": revision.profile_id,
                "revision": revision.revision,
                "expected_generation": command.expected_generation,
                "generation": command.generation,
                "expected_instance_id": command.expected_instance_id,
                "reconcile": command.reconcile,
                "config": config,
                "lease_token": self.token(command),
                "lease_expires_at": command.lease_expires_at,
                "execution_expires_at": command.execution_expires_at,
                "delivery_expires_at": command.delivery_expires_at,
            }
        )
        self.session.commit()
        return CommandClaimRead(command=result)

    def verify_lease(self, command: Command, token: str, now: datetime) -> None:
        if command.lease_version == 0 or not hmac.compare_digest(token, self.token(command)):
            raise DomainError(409, "Lease token не соответствует текущей выдаче", "LEASE_MISMATCH")
        if (
            command.state not in ACTIVE
            or command.lease_expires_at is None
            or command.lease_expires_at <= now
        ):
            raise DomainError(409, "Lease истёк: повторно получите команду", "LEASE_EXPIRED")

    def renew(self, command_id: UUID, payload: LeaseInput, agent: Host) -> LeaseRead:
        command, deployment = self.context(command_id, agent)
        now = utcnow()
        self.expire(command, deployment, now)
        self.verify_lease(command, payload.lease_token.get_secret_value(), now)
        if command.execution_expires_at is None:
            raise RuntimeError("Lease выдан без срока выполнения")
        command.lease_expires_at = min(
            now + timedelta(seconds=LEASE_SECONDS), command.execution_expires_at
        )
        command.state = "running"
        self.session.commit()
        return LeaseRead(
            command_id=command.id,
            lease_expires_at=command.lease_expires_at,
            execution_expires_at=command.execution_expires_at,
        )

    def ack(self, command_id: UUID, payload: CommandAck, agent: Host) -> JobRead:
        command, deployment = self.context(command_id, agent)
        token = payload.lease_token.get_secret_value()
        digest = hashlib.sha256(
            json.dumps(
                payload.model_dump(mode="json", exclude={"lease_token"}),
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        if command.ack_digest is not None:
            if not hmac.compare_digest(token, self.token(command)):
                raise DomainError(409, "Lease token не соответствует результату", "LEASE_MISMATCH")
            if not hmac.compare_digest(command.ack_digest, digest):
                raise DomainError(
                    409, "Команда уже подтверждена другим результатом", "ACK_CONFLICT"
                )
            return self.read(command)
        now = utcnow()
        self.expire(command, deployment, now)
        self.verify_lease(command, token, now)
        CommandPolicy.validate_ack(command, deployment, self.revision(command), payload)
        InstanceRegistry(self.session).record(command, payload)
        command.state = payload.outcome
        command.ack_digest = digest
        command.completed_at = now
        command.error_code = payload.error.code if payload.error else None
        command.error = payload.error.message if payload.error else None
        deployment.state = payload.runtime_state
        deployment.instance_id = payload.instance_id
        deployment.applied_revision_id = (
            command.profile_revision_id if payload.applied_revision else None
        )
        AuditService.add(
            self.session,
            "command.acknowledged",
            agent.id,
            command.id,
            {"outcome": payload.outcome, "generation": payload.generation},
        )
        self.session.commit()
        return self.read(command)
