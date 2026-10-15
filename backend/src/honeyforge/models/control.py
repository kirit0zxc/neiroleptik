"""Персистентное состояние профилей, агентов и запланированных ловушек."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from honeyforge.common import utcnow
from honeyforge.db import Base


class Profile(Base):
    """Контейнер неизменяемых версий; блокировка строки сериализует обновления."""

    __tablename__ = "profiles"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(80))
    name_key: Mapped[str] = mapped_column(String(240), unique=True)
    description: Mapped[str] = mapped_column(String(500), default="")
    current_revision: Mapped[int] = mapped_column(default=1)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProfileRevision(Base):
    """Публичная конфигурация отделена от зашифрованных приманочных паролей."""

    __tablename__ = "profile_revisions"
    __table_args__ = (UniqueConstraint("profile_id", "revision", name="uq_profile_revision"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    profile_id: Mapped[UUID] = mapped_column(ForeignKey("profiles.id"), index=True)
    revision: Mapped[int]
    config: Mapped[dict[str, Any]] = mapped_column(JSON)
    credentials_ciphertext: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Enrollment(Base):
    """Одноразовая регистрация с блокировкой строки при обмене token."""

    __tablename__ = "enrollments"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Host(Base):
    """Узел связывает отдельный agent token с его возможностями и heartbeat."""

    __tablename__ = "hosts"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(80))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    capabilities: Mapped[list[str]] = mapped_column(JSON)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Deployment(Base):
    """Назначение конкретной версии профиля; planned не означает запуск процесса."""

    __tablename__ = "deployments"
    __table_args__ = (
        CheckConstraint(
            "state IN ('planned','deploying','running','stopping','stopped','failed','unknown')",
            name="deployment_lifecycle_state",
        ),
        CheckConstraint("generation >= 0", name="deployment_generation"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    host_id: Mapped[UUID] = mapped_column(ForeignKey("hosts.id"), index=True)
    profile_revision_id: Mapped[UUID] = mapped_column(ForeignKey("profile_revisions.id"))
    state: Mapped[str] = mapped_column(String(20), default="planned")
    generation: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    applied_revision_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("profile_revisions.id", name="fk_deployment_applied_revision")
    )
    instance_id: Mapped[UUID | None]
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PortReservation(Base):
    """Уникальный TCP-порт на узле защищён ограничением БД от гонки запросов."""

    __tablename__ = "port_reservations"
    host_id: Mapped[UUID] = mapped_column(ForeignKey("hosts.id"), primary_key=True)
    port: Mapped[int] = mapped_column(primary_key=True)
    deployment_id: Mapped[UUID] = mapped_column(ForeignKey("deployments.id"), index=True)
