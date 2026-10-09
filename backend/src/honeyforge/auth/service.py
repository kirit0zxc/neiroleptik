"""Операции входа, серверной сессии и защиты изменения данных."""

import hmac
from datetime import timedelta

from sqlalchemy import case, delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from honeyforge.auth.security import Security
from honeyforge.common import DomainError, utcnow
from honeyforge.config import Settings
from honeyforge.control.audit import AuditService
from honeyforge.models import LoginBucket, User, UserSession


class AuthService:
    """Сессия отзывается через БД; права читаются заново при каждом запросе."""

    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.security = Security(settings)

    def consume_login_attempt(self, ip: str) -> None:
        """Атомарный upsert предотвращает обход счётчика параллельными запросами."""
        now = utcnow()
        expired = LoginBucket.window_start <= now - timedelta(minutes=5)
        statement = (
            insert(LoginBucket)
            .values(key=self.security.ip_key(ip), window_start=now, attempts=1)
            .on_conflict_do_update(
                index_elements=[LoginBucket.key],
                set_={
                    "window_start": case((expired, now), else_=LoginBucket.window_start),
                    "attempts": case((expired, 1), else_=LoginBucket.attempts + 1),
                },
            )
            .returning(LoginBucket.attempts)
        )
        attempts = self.session.scalar(statement)
        self.session.commit()
        if attempts is not None and attempts > 20:
            raise DomainError(429, "Слишком много попыток входа; повторите через 5 минут")

    def login(self, username: str, password: str, ip: str) -> tuple[User, str]:
        self.consume_login_attempt(ip)
        user = self.session.scalar(select(User).where(User.username == username.lower()))
        if user is None:
            # Хеширование выравнивает стоимость проверки отсутствующего пользователя.
            self.security.hash_password(password)
        valid = user is not None and self.security.verify_password(user.password_hash, password)
        if not valid or user is None or not user.active:
            AuditService.add(self.session, "auth.login_failed", details={"username": username})
            self.session.commit()
            raise DomainError(401, "Неверное имя пользователя или пароль")
        token = self.security.token("hf_session_")
        self.session.add(
            UserSession(
                token_hash=self.security.digest(token),
                user_id=user.id,
                expires_at=utcnow() + timedelta(hours=self.settings.session_hours),
            )
        )
        AuditService.add(self.session, "auth.login", user.id, user.id)
        self.session.commit()
        return user, token

    def authenticate(self, token: str | None) -> User:
        if not token or len(token) > 128:
            raise DomainError(401, "Требуется вход")
        user = self.session.scalar(
            select(User)
            .join(UserSession, UserSession.user_id == User.id)
            .where(
                UserSession.token_hash == self.security.digest(token),
                UserSession.expires_at > utcnow(),
                User.active.is_(True),
            )
        )
        if user is None:
            raise DomainError(401, "Сессия недействительна или истекла")
        return user

    def verify_origin(self, origin: str | None) -> None:
        if origin not in self.settings.allowed_origins:
            raise DomainError(403, "Origin не разрешён")

    def verify_csrf(self, token: str, header: str | None, origin: str | None) -> None:
        self.verify_origin(origin)
        if not header or not hmac.compare_digest(
            self.security.csrf_token(token).encode(), header.encode()
        ):
            raise DomainError(403, "Неверный CSRF token")

    def logout(self, token: str, user: User) -> None:
        self.session.execute(
            delete(UserSession).where(UserSession.token_hash == self.security.digest(token))
        )
        AuditService.add(self.session, "auth.logout", user.id, user.id)
        self.session.commit()
