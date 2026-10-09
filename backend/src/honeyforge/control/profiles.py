"""Создание immutable revisions и безопасное представление конфигураций."""

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from honeyforge.api.schemas import (
    ProfileConfigInput,
    ProfileConfigPublic,
    ProfileCreate,
    ProfileRead,
    ProfileUpdate,
    RevisionRead,
)
from honeyforge.auth.security import Security
from honeyforge.common import DomainError
from honeyforge.config import Settings
from honeyforge.control.audit import AuditService
from honeyforge.models import Profile, ProfileRevision, User


class ProfileService:
    """Обновление блокирует профиль, но не изменяет ранее выданные версии."""

    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.security = Security(settings)

    def get(self, profile_id: UUID, lock: bool = False) -> Profile:
        statement = select(Profile).where(Profile.id == profile_id)
        if lock:
            statement = statement.with_for_update()
        profile = self.session.scalar(statement)
        if profile is None:
            raise DomainError(404, "Профиль не найден")
        return profile

    def revision(self, profile: Profile, revision: int | None = None) -> ProfileRevision:
        item = self.session.scalar(
            select(ProfileRevision).where(
                ProfileRevision.profile_id == profile.id,
                ProfileRevision.revision == (revision or profile.current_revision),
            )
        )
        if item is None:
            raise DomainError(404, "Версия профиля не найдена")
        return item

    def public_config(self, item: ProfileRevision) -> ProfileConfigPublic:
        credentials = self.security.decrypt_credentials(item.credentials_ciphertext)
        return ProfileConfigPublic.model_validate(
            {
                **item.config,
                "credential_usernames": [value["username"] for value in credentials],
            }
        )

    def read(self, profile: Profile) -> ProfileRead:
        return ProfileRead(
            id=profile.id,
            name=profile.name,
            description=profile.description,
            archived=profile.archived,
            revision=profile.current_revision,
            config=self.public_config(self.revision(profile)),
            created_at=profile.created_at,
        )

    def config_and_credentials(
        self,
        config: ProfileConfigInput,
        previous: ProfileRevision | None = None,
    ) -> tuple[dict[str, Any], str]:
        """null сохраняет пароль; пустой список означает явное удаление."""
        if config.credentials is None:
            credentials = (
                self.security.decrypt_credentials(previous.credentials_ciphertext)
                if previous
                else []
            )
        else:
            credentials = [
                {"username": item.username, "password": item.password.get_secret_value()}
                for item in config.credentials
            ]
        if config.interaction_level != "low" and not credentials:
            raise DomainError(422, "Medium и High требуют приманочные credentials")
        return config.model_dump(
            mode="json", exclude={"credentials"}
        ), self.security.encrypt_credentials(credentials)

    def create(self, payload: ProfileCreate, actor: User) -> ProfileRead:
        config, ciphertext = self.config_and_credentials(payload.config)
        profile = Profile(
            name=payload.name, name_key=payload.name.casefold(), description=payload.description
        )
        try:
            self.session.add(profile)
            self.session.flush()
            self.session.add(
                ProfileRevision(
                    profile_id=profile.id,
                    revision=1,
                    config=config,
                    credentials_ciphertext=ciphertext,
                )
            )
            AuditService.add(self.session, "profile.created", actor.id, profile.id, {"revision": 1})
            self.session.commit()
        except IntegrityError as error:
            self.session.rollback()
            raise DomainError(409, "Профиль с таким названием уже существует") from error
        return self.read(profile)

    def update(self, profile_id: UUID, payload: ProfileUpdate, actor: User) -> ProfileRead:
        profile = self.get(profile_id, lock=True)
        if profile.archived:
            raise DomainError(409, "Архивный профиль нельзя изменять")
        if profile.current_revision != payload.expected_revision:
            raise DomainError(409, "Профиль уже изменён: перечитайте текущую версию")
        previous = self.revision(profile)
        config, ciphertext = (
            self.config_and_credentials(payload.config, previous)
            if payload.config
            else (previous.config, previous.credentials_ciphertext)
        )
        if payload.name is not None:
            profile.name, profile.name_key = payload.name, payload.name.casefold()
        if payload.description is not None:
            profile.description = payload.description
        profile.current_revision += 1
        self.session.add(
            ProfileRevision(
                profile_id=profile.id,
                revision=profile.current_revision,
                config=config,
                credentials_ciphertext=ciphertext,
            )
        )
        AuditService.add(
            self.session,
            "profile.updated",
            actor.id,
            profile.id,
            {"revision": profile.current_revision},
        )
        try:
            self.session.commit()
        except IntegrityError as error:
            self.session.rollback()
            raise DomainError(409, "Название профиля уже используется") from error
        return self.read(profile)

    def archive(self, profile_id: UUID, actor: User) -> None:
        profile = self.get(profile_id, lock=True)
        profile.archived = True
        AuditService.add(self.session, "profile.archived", actor.id, profile.id)
        self.session.commit()

    def list_profiles(self, limit: int, offset: int, archived: bool) -> list[ProfileRead]:
        profiles = self.session.scalars(
            select(Profile)
            .where(Profile.archived == archived)
            .order_by(Profile.created_at, Profile.id)
            .limit(limit)
            .offset(offset)
        )
        return [self.read(profile) for profile in profiles]

    def revisions(self, profile_id: UUID, limit: int, offset: int) -> list[RevisionRead]:
        self.get(profile_id)
        items = self.session.scalars(
            select(ProfileRevision)
            .where(
                ProfileRevision.profile_id == profile_id,
            )
            .order_by(ProfileRevision.revision.desc())
            .limit(limit)
            .offset(offset)
        )
        return [
            RevisionRead(
                revision=item.revision, config=self.public_config(item), created_at=item.created_at
            )
            for item in items
        ]
