"""Импорт моделей регистрирует полную схему для Alembic."""

from honeyforge.models.audit import AuditEvent
from honeyforge.models.auth import LoginBucket, User, UserSession
from honeyforge.models.control import (
    Deployment,
    Enrollment,
    Host,
    PortReservation,
    Profile,
    ProfileRevision,
)

__all__ = [
    "AuditEvent",
    "Deployment",
    "Enrollment",
    "Host",
    "LoginBucket",
    "PortReservation",
    "Profile",
    "ProfileRevision",
    "User",
    "UserSession",
]
