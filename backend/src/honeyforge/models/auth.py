"""Модели пользователей, сессий и долговечного ограничения входа."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from honeyforge.common import utcnow
from honeyforge.db import Base


class User(Base):
    """Роль проверяется по актуальной записи, а не по содержимому cookie."""

    __tablename__ = "users"
    __table_args__ = (CheckConstraint("role IN ('admin','operator','viewer')", name="user_role"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UserSession(Base):
    """В БД сохраняется только digest случайного session token."""

    __tablename__ = "user_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class LoginBucket(Base):
    """Счётчик попыток по хешированному IP общий для всех процессов приложения."""

    __tablename__ = "login_buckets"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int]
