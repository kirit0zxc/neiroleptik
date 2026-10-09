"""Историческая идентичность экземпляра сохраняется после reset и stop."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from honeyforge.common import utcnow
from honeyforge.db import Base


class RuntimeInstance(Base):
    """Глобальный UUID никогда не назначается другому запуску или deployment."""

    __tablename__ = "runtime_instances"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    deployment_id: Mapped[UUID] = mapped_column(ForeignKey("deployments.id"), index=True)
    profile_revision_id: Mapped[UUID] = mapped_column(ForeignKey("profile_revisions.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
