"""Импорт моделей регистрирует полную схему для Alembic."""

from honeyforge.models.audit import AuditEvent
from honeyforge.models.auth import LoginBucket, User, UserSession
from honeyforge.models.commands import Command
from honeyforge.models.control import (
    Deployment,
    Enrollment,
    Host,
    PortReservation,
    Profile,
    ProfileRevision,
)
from honeyforge.models.instances import RuntimeInstance

__all__ = [
    "AuditEvent",
    "Command",
    "Deployment",
    "Enrollment",
    "Host",
    "LoginBucket",
    "PortReservation",
    "Profile",
    "ProfileRevision",
    "RuntimeInstance",
    "User",
    "UserSession",
]
