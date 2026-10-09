"""Долговечные задания, поколения lease и отпечатки финальных подтверждений."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from honeyforge.common import utcnow
from honeyforge.db import Base


class Command(Base):
    """Ограничения БД дополняют блокировки: одно активное задание на deployment."""

    __tablename__ = "commands"
    __table_args__ = (
        CheckConstraint("action IN ('start','stop','reset')", name="command_action"),
        CheckConstraint(
            "state IN ('requested','leased','running','succeeded','failed','expired')",
            name="command_state",
        ),
        CheckConstraint(
            "expected_generation >= 0 AND generation = expected_generation + 1",
            name="command_generation",
        ),
        CheckConstraint("lease_version >= 0", name="command_lease_version"),
        Index("uq_command_idempotency", "deployment_id", "idempotency_key", unique=True),
        Index(
            "uq_command_active",
            "deployment_id",
            unique=True,
            postgresql_where=text("state IN ('requested','leased','running')"),
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    deployment_id: Mapped[UUID] = mapped_column(ForeignKey("deployments.id"), index=True)
    host_id: Mapped[UUID] = mapped_column(ForeignKey("hosts.id"), index=True)
    profile_revision_id: Mapped[UUID] = mapped_column(ForeignKey("profile_revisions.id"))
    idempotency_key: Mapped[UUID]
    action: Mapped[str] = mapped_column(String(16))
    state: Mapped[str] = mapped_column(String(16), default="requested")
    expected_generation: Mapped[int] = mapped_column(BigInteger)
    generation: Mapped[int] = mapped_column(BigInteger)
    expected_instance_id: Mapped[UUID | None]
    reconcile: Mapped[bool] = mapped_column(Boolean, default=False)
    lease_version: Mapped[int] = mapped_column(default=0)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    execution_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivery_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ack_digest: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
