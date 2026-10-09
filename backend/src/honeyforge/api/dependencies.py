"""Авторизация HTTP-запросов отделена от бизнес-операций."""

from typing import Annotated

from fastapi import Depends, Header, Request, Security
from fastapi.security import APIKeyCookie, HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from honeyforge.auth.service import AuthService
from honeyforge.common import DomainError
from honeyforge.config import Settings, get_settings
from honeyforge.control.registry import RegistryService
from honeyforge.db import get_session
from honeyforge.models import Host, User

Database = Annotated[Session, Depends(get_session)]
Configuration = Annotated[Settings, Depends(get_settings)]
session_cookie = APIKeyCookie(name="hf_session", auto_error=False)
agent_bearer = HTTPBearer(auto_error=False)


def current_user(
    request: Request,
    db: Database,
    settings: Configuration,
    token: Annotated[str | None, Security(session_cookie)],
    x_csrf_token: Annotated[str | None, Header()] = None,
    origin: Annotated[str | None, Header()] = None,
) -> User:
    """Все изменения пользовательской сессии требуют Origin и CSRF."""
    service = AuthService(db, settings)
    user = service.authenticate(token)
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        service.verify_csrf(token or "", x_csrf_token, origin)
    return user


Reader = Annotated[User, Depends(current_user)]


def operator(user: Reader) -> User:
    if user.role not in {"admin", "operator"}:
        raise DomainError(403, "Требуется роль operator или admin")
    return user


def administrator(user: Reader) -> User:
    if user.role != "admin":
        raise DomainError(403, "Требуется роль admin")
    return user


Operator = Annotated[User, Depends(operator)]
Administrator = Annotated[User, Depends(administrator)]


def current_agent(
    db: Database,
    settings: Configuration,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(agent_bearer)],
) -> Host:
    token = credentials.credentials if credentials is not None else None
    return RegistryService(db, settings).authenticate(token)


Agent = Annotated[Host, Depends(current_agent)]
