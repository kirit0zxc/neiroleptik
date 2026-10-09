"""Аудит фиксирует происхождение операций без их секретных параметров."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from honeyforge.common import utcnow
from honeyforge.db import Base


class AuditEvent(Base):
    """Идентификатор инициатора сохраняется даже после изменения его состояния."""

    __tablename__ = "audit_events"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    actor_id: Mapped[UUID | None]
    action: Mapped[str] = mapped_column(String(80))
    target_id: Mapped[UUID | None]
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
