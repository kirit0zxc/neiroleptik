"""Разбор конфигурации центра в неизменяемые объекты без раскрытия паролей."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal, cast
from uuid import UUID

Level = Literal["low", "medium", "high"]
Protocol = Literal["tcp", "http", "ssh"]
DeploymentState = Literal[
    "planned", "deploying", "running", "stopping", "stopped", "failed", "unknown"
]


class ContractError(ValueError):
    """Причина содержит только путь поля, никогда его входное значение."""

    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__(f"Некорректное поле: {path}")


@dataclass(frozen=True, repr=False)
class Secret:
    """Явное чтение секрета; обычное форматирование всегда скрывает значение."""

    _value: str = field(repr=False)

    def reveal(self) -> str:
        """Вызывается только при подготовке защищённой конфигурации ловушки."""
        return self._value

    def __repr__(self) -> str:
        return "Secret(***)"

    def __str__(self) -> str:
        return "***"


@dataclass(frozen=True)
class ServiceSpec:
    """Сервис профиля: внешний порт и необязательная легенда."""

    protocol: Protocol
    port: int
    banner: str = ""


@dataclass(frozen=True)
class LoggingSpec:
    """Предел исходных байтов одного события."""

    max_payload_bytes: int = 8192


@dataclass(frozen=True)
class ChannelSpec:
    """Размер и период batch телеметрии."""

    flush_interval_ms: int = 1500
    batch_size: int = 100


@dataclass(frozen=True)
class HighSpec:
    """Настройки будущей VM; наличие конфигурации не означает реализацию High."""

    template: str = "ubuntu-ssh-v1"
    ttl_seconds: int = 900
    cpu: int = 2
    memory_mb: int = 2048


@dataclass(frozen=True)
class CredentialSpec:
    """Учётные данные ловушки с безопасным repr пароля."""

    username: str
    password: Secret


@dataclass(frozen=True)
class ProfileSpec:
    """Полная конфигурация назначенного профиля."""

    interaction_level: Level
    hostname: str
    services: tuple[ServiceSpec, ...]
    logging: LoggingSpec
    channel: ChannelSpec
    high: HighSpec | None
    credentials: tuple[CredentialSpec, ...]


@dataclass(frozen=True)
class DeploymentSpec:
    """Назначение из AgentDeployment; поколения не являются instance_id."""

    deployment_id: str
    profile_id: str
    revision: int
    state: DeploymentState
    config: ProfileSpec
    generation: int = 0
    applied_revision: int | None = None
    instance_id: str | None = None


@dataclass(frozen=True)
class RejectedDeployment:
    """Безопасное описание отдельного отклонённого назначения."""

    deployment_id: str | None
    reason: str


@dataclass(frozen=True)
class AgentConfig:
    """Корректные назначения и локальные ошибки одного ответа центра."""

    host_id: str
    deployments: tuple[DeploymentSpec, ...]
    rejected: tuple[RejectedDeployment, ...] = ()


class ConfigParser:
    """Проверяет обязательные поля; дополнительные поля допускает для эволюции API."""

    @staticmethod
    def _object(value: object, path: str) -> dict[str, object]:
        if not isinstance(value, dict) or any(not isinstance(k, str) for k in value):
            raise ContractError(path)
        return cast(dict[str, object], value)

    @staticmethod
    def _string(value: object, path: str) -> str:
        if not isinstance(value, str) or "\x00" in value:
            raise ContractError(path)
        try:
            value.encode("utf8")
        except UnicodeError:
            raise ContractError(path) from None
        return value

    @classmethod
    def _uuid(cls, value: object, path: str) -> str:
        text = cls._string(value, path)
        try:
            return str(UUID(text))
        except ValueError:
            raise ContractError(path) from None

    @staticmethod
    def _integer(value: object, path: str, minimum: int, maximum: int = 2**63 - 1) -> int:
        if type(value) is not int or not minimum <= value <= maximum:
            raise ContractError(path)
        return value

    @staticmethod
    def _list(value: object, path: str) -> list[object]:
        if not isinstance(value, list):
            raise ContractError(path)
        return value

    @classmethod
    def _profile(cls, value: object, path: str) -> ProfileSpec:
        obj = cls._object(value, path)
        level = cls._string(obj.get("interaction_level"), path + ".interaction_level")
        if level not in ("low", "medium", "high"):
            raise ContractError(path + ".interaction_level")
        hostname = cls._string(obj.get("hostname", "service-node"), path + ".hostname")
        if re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9-]{0,62}", hostname) is None:
            raise ContractError(path + ".hostname")
        services: list[ServiceSpec] = []
        for index, raw in enumerate(cls._list(obj.get("services"), path + ".services")):
            item_path = f"{path}.services[{index}]"
            item = cls._object(raw, item_path)
            protocol = cls._string(item.get("protocol"), item_path + ".protocol")
            allowed = (
                ("ssh",) if level == "high" else ("ssh",) if level == "medium" else ("tcp", "http")
            )
            if protocol not in allowed:
                raise ContractError(item_path + ".protocol")
            port = cls._integer(item.get("port"), item_path + ".port", 1, 65535)
            banner = cls._string(item.get("banner", ""), item_path + ".banner")
            if len(banner) > 200 or "\r" in banner or "\n" in banner:
                raise ContractError(item_path + ".banner")
            services.append(ServiceSpec(cast(Protocol, protocol), port, banner))
        if (
            not 1 <= len(services) <= 8
            or len({s.port for s in services}) != len(services)
            or (level == "high" and len(services) != 1)
        ):
            raise ContractError(path + ".services")
        logging = cls._object(obj.get("logging", {}), path + ".logging")
        channel = cls._object(obj.get("channel", {}), path + ".channel")
        credentials: list[CredentialSpec] = []
        for index, raw in enumerate(cls._list(obj.get("credentials"), path + ".credentials")):
            item_path = f"{path}.credentials[{index}]"
            item = cls._object(raw, item_path)
            username = cls._string(item.get("username"), item_path + ".username")
            password = cls._string(item.get("password"), item_path + ".password")
            if re.fullmatch(r"[a-zA-Z0-9_.-]{1,64}", username) is None:
                raise ContractError(item_path + ".username")
            if not 1 <= len(password) <= 128 or "\r" in password or "\n" in password:
                raise ContractError(item_path + ".password")
            credentials.append(CredentialSpec(username, Secret(password)))
        if len(credentials) > 8 or len({c.username for c in credentials}) != len(credentials):
            raise ContractError(path + ".credentials")
        high: HighSpec | None = None
        if obj.get("high") is not None:
            h = cls._object(obj["high"], path + ".high")
            if h.get("template", "ubuntu-ssh-v1") != "ubuntu-ssh-v1":
                raise ContractError(path + ".high.template")
            high = HighSpec(
                ttl_seconds=cls._integer(
                    h.get("ttl_seconds", 900), path + ".high.ttl_seconds", 60, 3600
                ),
                cpu=cls._integer(h.get("cpu", 2), path + ".high.cpu", 1, 2),
                memory_mb=cls._integer(
                    h.get("memory_mb", 2048), path + ".high.memory_mb", 1024, 4096
                ),
            )
        if (level == "high") != (high is not None):
            raise ContractError(path + ".high")
        return ProfileSpec(
            cast(Level, level),
            hostname,
            tuple(services),
            LoggingSpec(
                cls._integer(
                    logging.get("max_payload_bytes", 8192),
                    path + ".logging.max_payload_bytes",
                    256,
                    8192,
                )
            ),
            ChannelSpec(
                cls._integer(
                    channel.get("flush_interval_ms", 1500),
                    path + ".channel.flush_interval_ms",
                    100,
                    5000,
                ),
                cls._integer(channel.get("batch_size", 100), path + ".channel.batch_size", 1, 100),
            ),
            high,
            tuple(credentials),
        )

    @classmethod
    def deployment(cls, value: object, path: str = "deployment") -> DeploymentSpec:
        """Разбирает назначение согласно текущему AgentDeployment."""
        obj = cls._object(value, path)
        state = cls._string(obj.get("state"), path + ".state")
        if state not in (
            "planned",
            "deploying",
            "running",
            "stopping",
            "stopped",
            "failed",
            "unknown",
        ):
            raise ContractError(path + ".state")
        if "applied_revision" not in obj or "instance_id" not in obj:
            raise ContractError(path)
        applied = obj["applied_revision"]
        instance = obj["instance_id"]
        return DeploymentSpec(
            cls._uuid(obj.get("deployment_id"), path + ".deployment_id"),
            cls._uuid(obj.get("profile_id"), path + ".profile_id"),
            cls._integer(obj.get("revision"), path + ".revision", 1),
            cast(DeploymentState, state),
            cls._profile(obj.get("config"), path + ".config"),
            cls._integer(obj.get("generation"), path + ".generation", 0),
            None if applied is None else cls._integer(applied, path + ".applied_revision", 1),
            None if instance is None else cls._uuid(instance, path + ".instance_id"),
        )

    @classmethod
    def parse(cls, value: object) -> AgentConfig:
        """Повреждённое назначение не мешает остальным; дубли ID отклоняются все."""
        obj = cls._object(value, "config")
        host = cls._uuid(obj.get("host_id"), "host_id")
        raw = cls._list(obj.get("deployments"), "deployments")
        counts: dict[str, int] = {}
        ids: list[str | None] = []
        for item in raw:
            try:
                ident = cls._uuid(
                    cls._object(item, "deployment").get("deployment_id"), "deployment_id"
                )
            except ContractError:
                ident = None
            ids.append(ident)
            if ident is not None:
                counts[ident] = counts.get(ident, 0) + 1
        accepted = []
        rejected = []
        for index, (item, ident) in enumerate(zip(raw, ids, strict=True)):
            try:
                if ident is not None and counts[ident] > 1:
                    raise ContractError(f"deployments[{index}].deployment_id")
                accepted.append(cls.deployment(item, f"deployments[{index}]"))
            except ContractError as error:
                rejected.append(RejectedDeployment(ident, str(error)))
        return AgentConfig(host, tuple(accepted), tuple(rejected))
