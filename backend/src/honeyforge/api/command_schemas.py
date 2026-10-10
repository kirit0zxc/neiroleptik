"""Общий контракт заданий пользователя и отдельного Bearer-протокола агента."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, SecretStr, model_validator

from honeyforge.api.schemas import AgentProfileConfig, StrictModel

CommandAction = Literal["start", "stop", "reset"]
JobState = Literal["requested", "leased", "running", "succeeded", "failed", "expired"]


class CommandCreate(StrictModel):
    """Повтор нажатия сохраняет ключ; optimistic concurrency защищает новое действие."""

    command: CommandAction
    idempotency_key: UUID
    expected_generation: int = Field(ge=0, le=2**63 - 2, strict=True)


class CommandAccepted(StrictModel):
    job_id: UUID


class JobRead(StrictModel):
    """Публичное задание не содержит lease token и конфигурацию с паролями."""

    job_id: UUID
    deployment_id: UUID
    command: CommandAction
    state: JobState
    error: str | None
    error_code: str | None
    generation: int
    created_at: datetime
    completed_at: datetime | None


class CommandClaim(StrictModel):
    command_id: UUID | None = None


class AgentCommand(StrictModel):
    """Полная команда доступна только агенту закреплённого узла."""

    command_id: UUID
    deployment_id: UUID
    action: CommandAction
    profile_id: UUID
    revision: int
    expected_generation: int
    generation: int
    expected_instance_id: UUID | None
    reconcile: bool
    config: AgentProfileConfig
    lease_token: str
    lease_expires_at: datetime
    execution_expires_at: datetime
    delivery_expires_at: datetime


class CommandClaimRead(StrictModel):
    command: AgentCommand | None


class LeaseInput(StrictModel):
    lease_token: SecretStr = Field(min_length=73, max_length=73)


class LeaseRead(StrictModel):
    command_id: UUID
    lease_expires_at: datetime
    execution_expires_at: datetime


class RuntimeErrorRead(StrictModel):
    code: str = Field(min_length=1, max_length=64, pattern=r"^[A-Z][A-Z0-9_]*$")
    message: str = Field(min_length=1, max_length=500)


class CommandAck(LeaseInput):
    """Финальный результат операции отличается от наблюдаемого состояния процесса."""

    deployment_id: UUID
    generation: int = Field(ge=1, le=2**63 - 1, strict=True)
    applied_revision: int | None = Field(ge=1, strict=True)
    instance_id: UUID | None
    outcome: Literal["succeeded", "failed"]
    runtime_state: Literal["running", "stopped", "failed"]
    error: RuntimeErrorRead | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> "CommandAck":
        if (self.outcome == "failed") != (self.error is not None):
            raise ValueError("failed требует ошибку; succeeded не допускает ошибку")
        if self.runtime_state == "running" and (
            self.instance_id is None or self.applied_revision is None
        ):
            raise ValueError("running требует экземпляр и применённую ревизию")
        if self.applied_revision is not None and self.instance_id is None:
            raise ValueError("Применённая ревизия требует идентичность экземпляра")
        return self
