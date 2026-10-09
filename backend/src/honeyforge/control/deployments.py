"""Назначение версии профиля узлу без ложного подтверждения запуска."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from honeyforge.api.schemas import (
    AgentConfigRead,
    AgentDeployment,
    AgentProfileConfig,
    DeploymentCreate,
    DeploymentRead,
)
from honeyforge.common import DomainError
from honeyforge.config import Settings
from honeyforge.control.audit import AuditService
from honeyforge.control.commands import CommandService
from honeyforge.control.profiles import ProfileService
from honeyforge.control.registry import RegistryService
from honeyforge.models import Deployment, Host, PortReservation, User


class DeploymentService:
    """PostgreSQL атомарно резервирует TCP-порты, чтобы параллельные POST не конфликтовали."""

    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.profiles = ProfileService(session, settings)
        self.registry = RegistryService(session, settings)
        self.commands = CommandService(session, settings)

    def read(self, item: Deployment) -> DeploymentRead:
        from honeyforge.models import ProfileRevision

        self.commands.refresh(item.id)
        revision = self.session.get(ProfileRevision, item.profile_revision_id)
        if revision is None:
            raise RuntimeError("Нарушена внешняя связь deployment/profile revision")
        return DeploymentRead.model_validate(
            {
                "id": item.id,
                "host_id": item.host_id,
                "profile_id": revision.profile_id,
                "revision": revision.revision,
                "state": item.state,
                "interaction_level": revision.config["interaction_level"],
                "desired_revision": revision.revision,
                "applied_revision": revision.revision if item.applied_revision_id else None,
                "generation": item.generation,
                "instance_id": item.instance_id,
                "created_at": item.created_at,
            }
        )

    def create(self, payload: DeploymentCreate, actor: User) -> DeploymentRead:
        host = self.registry.get(payload.host_id, lock=True)
        if host.revoked_at is not None:
            raise DomainError(409, "Узел отозван")
        profile = self.profiles.get(payload.profile_id, lock=True)
        if profile.archived:
            raise DomainError(409, "Архивный профиль нельзя назначить")
        revision = self.profiles.revision(profile, payload.revision)
        if revision.config["interaction_level"] not in host.capabilities:
            raise DomainError(409, "Узел не заявил поддержку выбранного уровня")
        item = Deployment(host_id=host.id, profile_revision_id=revision.id)
        try:
            self.session.add(item)
            self.session.flush()
            for service in revision.config["services"]:
                self.session.add(
                    PortReservation(host_id=host.id, port=service["port"], deployment_id=item.id)
                )
            AuditService.add(
                self.session,
                "deployment.planned",
                actor.id,
                item.id,
                {"revision": revision.revision},
            )
            self.session.commit()
        except IntegrityError as error:
            self.session.rollback()
            raise DomainError(409, "Один из портов уже зарезервирован на этом узле") from error
        return self.read(item)

    def get(self, deployment_id: UUID) -> DeploymentRead:
        item = self.session.get(Deployment, deployment_id)
        if item is None:
            raise DomainError(404, "Назначение не найдено")
        return self.read(item)

    def list(self, limit: int, offset: int) -> list[DeploymentRead]:
        items = self.session.scalars(
            select(Deployment)
            .order_by(Deployment.created_at, Deployment.id)
            .limit(limit)
            .offset(offset)
        )
        return [self.read(item) for item in items]

    def config(self, host: Host) -> AgentConfigRead:
        """Агент не выбирает host_id извне и получает только свои назначения."""
        from honeyforge.models import ProfileRevision

        items = self.session.scalars(
            select(Deployment)
            .where(Deployment.host_id == host.id)
            .order_by(Deployment.created_at, Deployment.id)
        )
        result = []
        for item in items:
            self.commands.refresh(item.id)
            revision = self.session.get(ProfileRevision, item.profile_revision_id)
            if revision is None:
                raise RuntimeError("Нарушена связь версии")
            result.append(
                AgentDeployment.model_validate(
                    {
                        "deployment_id": item.id,
                        "profile_id": revision.profile_id,
                        "revision": revision.revision,
                        "state": item.state,
                        "generation": item.generation,
                        "applied_revision": revision.revision if item.applied_revision_id else None,
                        "instance_id": item.instance_id,
                        "config": AgentProfileConfig.model_validate(
                            {
                                **revision.config,
                                "credentials": self.profiles.security.decrypt_credentials(
                                    revision.credentials_ciphertext
                                ),
                            }
                        ),
                    }
                )
            )
        return AgentConfigRead(host_id=host.id, deployments=result)
