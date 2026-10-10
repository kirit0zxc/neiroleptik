"""Проверка сетевых команд без зависимости от пакета центра и раскрытия профиля."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal, Protocol, cast
from uuid import UUID

from agent.runtime.contract import (
    AgentConfig,
    ConfigParser,
    ContractError,
    DeploymentSpec,
    ProfileSpec,
)

Action = Literal["start", "stop", "reset"]
MAX_GENERATION = 2**63 - 1
_LEASE = re.compile(r"hf_lease_[0-9a-f]{64}")


def _object(value: object, path: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ContractError(path)
    return cast(dict[str, object], value)


def _fields(value: dict[str, object], expected: set[str], path: str) -> None:
    """Обязательные nullable-поля также должны присутствовать, как в OpenAPI."""
    if set(value) != expected:
        raise ContractError(path)


def _integer(value: object, path: str, minimum: int = 1) -> int:
    if type(value) is not int or not minimum <= value <= MAX_GENERATION:
        raise ContractError(path)
    return value


def _uuid(value: object, path: str) -> str:
    if not isinstance(value, str):
        raise ContractError(path)
    try:
        return str(UUID(value))
    except (ValueError, AttributeError):
        raise ContractError(path) from None


def _time(value: object, path: str) -> datetime:
    if not isinstance(value, str):
        raise ContractError(path)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(UTC)
    except (ValueError, OverflowError):
        raise ContractError(path) from None


def _action(value: object, path: str) -> Action:
    if value not in ("start", "stop", "reset"):
        raise ContractError(path)
    return value


@dataclass(frozen=True)
class ClaimedCommand:
    """Профиль используется в памяти; журнал сохраняет только его SHA-256."""

    command_id: str
    deployment_id: str
    action: Action
    profile_id: str
    revision: int
    expected_generation: int
    generation: int
    expected_instance_id: str | None
    reconcile: bool
    config: ProfileSpec = field(repr=False)
    lease_token: str = field(repr=False)
    lease_expires_at: datetime
    execution_expires_at: datetime
    delivery_expires_at: datetime
    config_sha256: str

    @classmethod
    def parse(cls, value: object) -> ClaimedCommand:
        obj = _object(value, "command")
        _fields(
            obj,
            {
                "command_id",
                "deployment_id",
                "action",
                "profile_id",
                "revision",
                "expected_generation",
                "generation",
                "expected_instance_id",
                "reconcile",
                "config",
                "lease_token",
                "lease_expires_at",
                "execution_expires_at",
                "delivery_expires_at",
            },
            "command",
        )
        command_id = _uuid(obj["command_id"], "command.command_id")
        deployment_id = _uuid(obj["deployment_id"], "command.deployment_id")
        profile_id = _uuid(obj["profile_id"], "command.profile_id")
        revision = _integer(obj["revision"], "command.revision")
        expected = _integer(obj["expected_generation"], "command.expected_generation", 0)
        generation = _integer(obj["generation"], "command.generation")
        if generation != expected + 1:
            raise ContractError("command.generation")
        instance = obj["expected_instance_id"]
        if instance is not None:
            instance = _uuid(instance, "command.expected_instance_id")
        reconcile = obj["reconcile"]
        if type(reconcile) is not bool:
            raise ContractError("command.reconcile")
        token = obj["lease_token"]
        if not isinstance(token, str) or _LEASE.fullmatch(token) is None:
            raise ContractError("command.lease_token")
        lease = _time(obj["lease_expires_at"], "command.lease_expires_at")
        execution = _time(obj["execution_expires_at"], "command.execution_expires_at")
        delivery = _time(obj["delivery_expires_at"], "command.delivery_expires_at")
        if lease > execution:
            raise ContractError("command.lease_expires_at")
        # Уже имеющийся разбор профиля сохраняет общие ограничения Low/Medium/High.
        deployment = ConfigParser.deployment(
            {
                "deployment_id": deployment_id,
                "profile_id": profile_id,
                "revision": revision,
                "state": "deploying",
                "generation": generation,
                "applied_revision": None,
                "instance_id": instance,
                "config": obj["config"],
            },
            "command",
        )
        try:
            canonical = json.dumps(
                obj["config"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (ValueError, TypeError, UnicodeError):
            raise ContractError("command.config") from None
        return cls(
            command_id,
            deployment_id,
            _action(obj["action"], "command.action"),
            profile_id,
            revision,
            expected,
            generation,
            instance,
            reconcile,
            deployment.config,
            token,
            lease,
            execution,
            delivery,
            hashlib.sha256(canonical).hexdigest(),
        )

    def identity(self) -> dict[str, object]:
        """Неизменяемые метаданные пригодны для диска и безопасного сравнения повторов."""
        return {
            "command_id": self.command_id,
            "deployment_id": self.deployment_id,
            "action": self.action,
            "profile_id": self.profile_id,
            "revision": self.revision,
            "expected_generation": self.expected_generation,
            "generation": self.generation,
            "expected_instance_id": self.expected_instance_id,
            "reconcile": self.reconcile,
            "config_sha256": self.config_sha256,
            "execution_expires_at": self.execution_expires_at.isoformat(),
            "delivery_expires_at": self.delivery_expires_at.isoformat(),
        }

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(self.identity(), sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def deployment(self) -> DeploymentSpec:
        """Преобразование команды не утверждает применение ревизии или запуск процесса."""
        return DeploymentSpec(
            self.deployment_id,
            self.profile_id,
            self.revision,
            "stopping" if self.action == "stop" else "deploying",
            self.config,
            self.generation,
            None,
            self.expected_instance_id,
        )


@dataclass(frozen=True)
class LeaseReply:
    """Продление сохраняет общий срок исполнения; токен не меняется."""

    command_id: str
    lease_expires_at: datetime
    execution_expires_at: datetime

    @classmethod
    def parse(cls, value: object) -> LeaseReply:
        obj = _object(value, "lease")
        _fields(obj, {"command_id", "lease_expires_at", "execution_expires_at"}, "lease")
        lease = _time(obj["lease_expires_at"], "lease.lease_expires_at")
        execution = _time(obj["execution_expires_at"], "lease.execution_expires_at")
        if lease > execution:
            raise ContractError("lease.lease_expires_at")
        return cls(_uuid(obj["command_id"], "lease.command_id"), lease, execution)


def validate_job(
    value: object,
    command_id: str,
    acknowledgement: dict[str, object],
) -> dict[str, object]:
    """Успешный HTTP ACK ещё обязан вернуть итог именно запрошенной команды."""
    obj = _object(value, "job")
    _fields(
        obj,
        {
            "job_id",
            "deployment_id",
            "command",
            "state",
            "error",
            "error_code",
            "generation",
            "created_at",
            "completed_at",
        },
        "job",
    )
    if _uuid(obj["job_id"], "job.job_id") != command_id:
        raise ContractError("job.job_id")
    _uuid(obj["deployment_id"], "job.deployment_id")
    _action(obj["command"], "job.command")
    _integer(obj["generation"], "job.generation")
    _time(obj["created_at"], "job.created_at")
    if obj["completed_at"] is not None:
        _time(obj["completed_at"], "job.completed_at")
    if obj["state"] not in ("requested", "leased", "running", "succeeded", "failed", "expired"):
        raise ContractError("job.state")
    for name in ("error", "error_code"):
        item = obj[name]
        if item is not None and (
            not isinstance(item, str)
            or "\x00" in item
            or any(0xD800 <= ord(c) <= 0xDFFF for c in item)
        ):
            raise ContractError("job." + name)
    outcome = acknowledgement.get("outcome")
    if outcome not in ("succeeded", "failed") or obj["state"] != outcome:
        raise ContractError("job.state")
    if (
        _uuid(obj["deployment_id"], "job.deployment_id")
        != _uuid(acknowledgement.get("deployment_id"), "ack.deployment_id")
        or obj["generation"] != _integer(acknowledgement.get("generation"), "ack.generation")
        or obj["completed_at"] is None
    ):
        raise ContractError("job")
    if outcome == "succeeded":
        if obj["error"] is not None or obj["error_code"] is not None:
            raise ContractError("job.error")
    else:
        error = _object(acknowledgement.get("error"), "ack.error")
        if (
            not isinstance(error.get("code"), str)
            or not error["code"]
            or not isinstance(error.get("message"), str)
            or not error["message"]
            or obj["error_code"] != error["code"]
            or obj["error"] != error["message"]
        ):
            raise ContractError("job.error")
    return obj


class CommandClient(Protocol):
    """Сетевой клиент и тестовая подмена предоставляют один интерфейс исполнителю."""

    async def claim(self, command_id: str | None = None) -> ClaimedCommand | None: ...
    async def renew(self, command_id: str, lease_token: str) -> LeaseReply: ...
    async def ack(self, command_id: str, payload: dict[str, object]) -> dict[str, object]: ...
    async def config(self) -> AgentConfig: ...
