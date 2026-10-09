"""Регистрация узла, отзыв agent token и проверяемое состояние связи."""

from datetime import timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from honeyforge.api.schemas import EnrollInput, EnrollmentRead, EnrollRead, HostRead
from honeyforge.auth.security import Security
from honeyforge.common import DomainError, utcnow
from honeyforge.config import Settings
from honeyforge.control.audit import AuditService
from honeyforge.models import Enrollment, Host, User


class RegistryService:
    """Одноразовые enrollment tokens не используются для последующих запросов агента."""

    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.security = Security(settings)

    def issue(self, actor: User) -> EnrollmentRead:
        token = self.security.token("hf_enroll_")
        item = Enrollment(
            token_hash=self.security.digest(token), expires_at=utcnow() + timedelta(minutes=15)
        )
        self.session.add(item)
        self.session.flush()
        AuditService.add(self.session, "enrollment.created", actor.id, item.id)
        self.session.commit()
        return EnrollmentRead(id=item.id, enrollment_token=token, expires_at=item.expires_at)

    def enroll(self, payload: EnrollInput) -> EnrollRead:
        item = self.session.scalar(
            select(Enrollment)
            .where(
                Enrollment.token_hash
                == self.security.digest(payload.enrollment_token.get_secret_value())
            )
            .with_for_update()
        )
        if item is None or item.consumed_at is not None or item.expires_at <= utcnow():
            raise DomainError(401, "Регистрационный token недействителен или истёк")
        token = self.security.token("hf_agent_")
        host = Host(
            name=payload.name,
            token_hash=self.security.digest(token),
            capabilities=payload.capabilities,
        )
        self.session.add(host)
        self.session.flush()
        item.consumed_at = utcnow()
        AuditService.add(self.session, "agent.enrolled", host.id, item.id)
        self.session.commit()
        return EnrollRead(host_id=host.id, agent_token=token)

    def authenticate(self, token: str | None) -> Host:
        if not token or not token.startswith("hf_agent_") or len(token) > 128:
            raise DomainError(401, "Требуется agent token")
        host = self.session.scalar(
            select(Host).where(
                Host.token_hash == self.security.digest(token),
                Host.revoked_at.is_(None),
            )
        )
        if host is None:
            raise DomainError(401, "Agent token недействителен или отозван")
        return host

    @staticmethod
    def read(host: Host) -> HostRead:
        status = (
            "revoked"
            if host.revoked_at
            else (
                "online"
                if host.last_seen and host.last_seen > utcnow() - timedelta(seconds=40)
                else "offline"
            )
        )
        return HostRead.model_validate(
            {
                "id": host.id,
                "name": host.name,
                "capabilities": host.capabilities,
                "last_seen": host.last_seen,
                "status": status,
                "created_at": host.created_at,
            }
        )

    def list(self, limit: int, offset: int) -> list[HostRead]:
        hosts = self.session.scalars(
            select(Host).order_by(Host.created_at, Host.id).limit(limit).offset(offset)
        )
        return [self.read(host) for host in hosts]

    def get(self, host_id: UUID, lock: bool = False) -> Host:
        statement = select(Host).where(Host.id == host_id)
        host = self.session.scalar(statement.with_for_update() if lock else statement)
        if host is None:
            raise DomainError(404, "Узел не найден")
        return host

    def heartbeat(self, host: Host) -> HostRead:
        host.last_seen = utcnow()
        self.session.commit()
        return self.read(host)

    def revoke(self, host_id: UUID, actor: User) -> HostRead:
        host = self.get(host_id, lock=True)
        host.revoked_at = utcnow()
        AuditService.add(self.session, "agent.revoked", actor.id, host.id)
        self.session.commit()
        return self.read(host)
