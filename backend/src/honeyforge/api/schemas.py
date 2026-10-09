"""Единые HTTP-схемы; OpenAPI генерируется из этих типов."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

Role = Literal["admin", "operator", "viewer"]
Level = Literal["low", "medium", "high"]


class StrictModel(BaseModel):
    """Опечатки в поле не должны превращаться в молча проигнорированные данные."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    @field_validator("*")
    @classmethod
    def validate_text(cls, value: Any) -> Any:
        """PostgreSQL text не принимает NUL и некорректные Unicode-последовательности."""
        text = value.get_secret_value() if isinstance(value, SecretStr) else value
        if isinstance(text, str):
            if "\x00" in text:
                raise ValueError("Текст не должен содержать NUL")
            try:
                text.encode("utf-8")
            except UnicodeEncodeError as error:
                raise ValueError("Текст должен быть корректным Unicode") from error
        return value


class ValidationIssue(StrictModel):
    """Безопасная ошибка поля без исходного значения и секретов."""

    loc: list[str | int]
    type: str
    msg: str


class ErrorResponse(StrictModel):
    """Общий контракт бизнес-ошибок и ошибок валидации."""

    detail: str | list[ValidationIssue]


class LoginInput(StrictModel):
    username: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    password: SecretStr = Field(min_length=1, max_length=256)


class UserRead(StrictModel):
    id: UUID
    username: str
    role: Role


class AuthRead(StrictModel):
    user: UserRead
    csrf_token: str


class ServiceConfig(StrictModel):
    protocol: Literal["tcp", "http", "ssh"]
    port: int = Field(ge=1, le=65535)
    banner: str = Field(default="", max_length=200)

    @field_validator("banner")
    @classmethod
    def validate_banner(cls, value: str) -> str:
        if any(char in value for char in ["\r", "\n", "\x00"]):
            raise ValueError("Баннер должен быть одной строкой")
        return value


class CredentialInput(StrictModel):
    username: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    password: SecretStr = Field(min_length=1, max_length=128)


class LoggingConfig(StrictModel):
    max_payload_bytes: int = Field(default=8192, ge=256, le=8192)


class ChannelConfig(StrictModel):
    flush_interval_ms: int = Field(default=1500, ge=100, le=5000)
    batch_size: int = Field(default=100, ge=1, le=100)


class HighConfig(StrictModel):
    template: Literal["ubuntu-ssh-v1"] = "ubuntu-ssh-v1"
    ttl_seconds: int = Field(default=900, ge=60, le=3600)
    cpu: int = Field(default=2, ge=1, le=2)
    memory_mb: int = Field(default=2048, ge=1024, le=4096)


class ConfigBase(StrictModel):
    interaction_level: Level
    hostname: str = Field(default="service-node", pattern=r"^[a-zA-Z0-9][a-zA-Z0-9-]{0,62}$")
    services: list[ServiceConfig] = Field(min_length=1, max_length=8)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    channel: ChannelConfig = Field(default_factory=ChannelConfig)
    high: HighConfig | None = None

    @model_validator(mode="after")
    def validate_services(self) -> "ConfigBase":
        """Профиль ограничен реализуемыми драйверами, а не произвольными программами."""
        allowed = {"low": {"tcp", "http"}, "medium": {"ssh"}, "high": {"ssh"}}
        if any(
            service.protocol not in allowed[self.interaction_level] for service in self.services
        ):
            raise ValueError("Протокол не поддерживается выбранным уровнем")
        if len({service.port for service in self.services}) != len(self.services):
            raise ValueError("Порты профиля должны быть уникальными")
        if self.interaction_level == "high":
            if self.high is None or len(self.services) != 1:
                raise ValueError("High требует параметры VM и один SSH-сервис")
        elif self.high is not None:
            raise ValueError("Параметры VM разрешены только для High")
        return self


class ProfileConfigInput(ConfigBase):
    credentials: list[CredentialInput] | None = Field(default=None, max_length=8)

    @field_validator("credentials")
    @classmethod
    def validate_usernames(
        cls, value: list[CredentialInput] | None
    ) -> list[CredentialInput] | None:
        if value and len({item.username for item in value}) != len(value):
            raise ValueError("Имена приманочных пользователей должны быть уникальными")
        return value


class ProfileConfigPublic(ConfigBase):
    credential_usernames: list[str]


class AgentCredential(StrictModel):
    """Пароль выдаётся только авторизованному агенту назначенного узла."""

    username: str
    password: str


class AgentProfileConfig(ConfigBase):
    credentials: list[AgentCredential]


class ProfileCreate(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    config: ProfileConfigInput

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Название не должно быть пустым")
        return value


class ProfileUpdate(StrictModel):
    expected_revision: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=500)
    config: ProfileConfigInput | None = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("Название не должно быть пустым")
        return value.strip() if value is not None else None


class ProfileRead(StrictModel):
    id: UUID
    name: str
    description: str
    archived: bool
    revision: int
    config: ProfileConfigPublic
    created_at: datetime


class RevisionRead(StrictModel):
    revision: int
    config: ProfileConfigPublic
    created_at: datetime


class EnrollmentRead(StrictModel):
    id: UUID
    enrollment_token: str
    expires_at: datetime


class EnrollInput(StrictModel):
    enrollment_token: SecretStr = Field(min_length=20, max_length=128)
    name: str = Field(min_length=1, max_length=80)
    capabilities: list[Level] = Field(min_length=1, max_length=3)

    @field_validator("capabilities")
    @classmethod
    def unique_capabilities(cls, value: list[Level]) -> list[Level]:
        if len(set(value)) != len(value):
            raise ValueError("Capabilities не должны повторяться")
        return value


class EnrollRead(StrictModel):
    host_id: UUID
    agent_token: str


class HostRead(StrictModel):
    id: UUID
    name: str
    capabilities: list[Level]
    last_seen: datetime | None
    status: Literal["online", "offline", "revoked"]
    created_at: datetime


class DeploymentCreate(StrictModel):
    host_id: UUID
    profile_id: UUID
    revision: int = Field(ge=1)


class DeploymentRead(StrictModel):
    id: UUID
    host_id: UUID
    profile_id: UUID
    revision: int
    state: Literal["planned"]
    created_at: datetime


class AgentDeployment(StrictModel):
    deployment_id: UUID
    profile_id: UUID
    revision: int
    state: Literal["planned"]
    config: AgentProfileConfig


class AgentConfigRead(StrictModel):
    host_id: UUID
    deployments: list[AgentDeployment]


class AuditRead(StrictModel):
    id: UUID
    actor_id: UUID | None
    action: str
    target_id: UUID | None
    details: dict[str, str | int | bool]
    created_at: datetime
