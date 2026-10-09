"""Разбор ответа центра GET /agent/v1/config в неизменяемые объекты.

Модуль повторяет границы схем центра (contracts/openapi.json: AgentConfigRead и
связанные) и не содержит сетевого кода. Пароли ловушек хранятся в Secret и не
попадают ни в repr, ни в str, ни в сообщения об ошибках разбора.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast, get_args
from uuid import UUID

Level = Literal["low", "medium", "high"]
ServiceProtocol = Literal["tcp", "http", "ssh"]

# Границы совпадают со схемами центра; тест дрейфа сверяет их с contracts/openapi.json.
MIN_SERVICES = 1
MAX_SERVICES = 8
MIN_PORT = 1
MAX_PORT = 65535
MAX_BANNER_LENGTH = 200
MIN_PAYLOAD_BYTES = 256
MAX_PAYLOAD_BYTES = 8192
MIN_FLUSH_INTERVAL_MS = 100
MAX_FLUSH_INTERVAL_MS = 5000
MIN_BATCH_SIZE = 1
MAX_BATCH_SIZE = 100
MIN_TTL_SECONDS = 60
MAX_TTL_SECONDS = 3600
MIN_CPU = 1
MAX_CPU = 2
MIN_MEMORY_MB = 1024
MAX_MEMORY_MB = 4096
# Верхняя граница списка credentials берётся из ProfileConfigInput центра (max_length=8).
MAX_CREDENTIALS = 8

# Значения по умолчанию совпадают с default в схемах центра.
DEFAULT_HOSTNAME = "service-node"
DEFAULT_MAX_PAYLOAD_BYTES = 8192
DEFAULT_FLUSH_INTERVAL_MS = 1500
DEFAULT_BATCH_SIZE = 100
DEFAULT_TTL_SECONDS = 900
DEFAULT_CPU = 2
DEFAULT_MEMORY_MB = 2048
HIGH_TEMPLATE = "ubuntu-ssh-v1"

_HOSTNAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9-]{0,62}")
_USERNAME = re.compile(r"[a-zA-Z0-9_.-]{1,64}")
_PROTOCOLS_BY_LEVEL: dict[str, frozenset[str]] = {
    "low": frozenset({"tcp", "http"}),
    "medium": frozenset({"ssh"}),
    "high": frozenset({"ssh"}),
}


class ContractError(ValueError):
    """Ответ центра не соответствует контракту.

    Хранит путь к полю и причину без самого значения: значением может быть пароль.
    """

    def __init__(self, path: str, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"{path}: {reason}")


class Secret:
    """Строка, значение которой нельзя случайно вывести в журнал или исключение."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        """Единственный способ прочитать значение; вызывается осознанно и по месту."""
        return self._value

    def __repr__(self) -> str:
        return "Secret(***)"

    def __str__(self) -> str:
        return "***"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Secret) and self._value == other._value

    def __hash__(self) -> int:
        return hash(self._value)


@dataclass(frozen=True)
class ServiceSpec:
    protocol: ServiceProtocol
    port: int
    banner: str


@dataclass(frozen=True)
class CredentialSpec:
    username: str
    password: Secret


@dataclass(frozen=True)
class LoggingSpec:
    max_payload_bytes: int


@dataclass(frozen=True)
class ChannelSpec:
    flush_interval_ms: int
    batch_size: int


@dataclass(frozen=True)
class HighSpec:
    template: str
    ttl_seconds: int
    cpu: int
    memory_mb: int


@dataclass(frozen=True)
class ProfileSpec:
    """Профиль одной ревизии; имена полей совпадают с AgentProfileConfig центра."""

    interaction_level: Level
    hostname: str
    services: tuple[ServiceSpec, ...]
    logging: LoggingSpec
    channel: ChannelSpec
    high: HighSpec | None
    credentials: tuple[CredentialSpec, ...]


@dataclass(frozen=True)
class DeploymentSpec:
    """Назначение профиля узлу; имена полей совпадают с AgentDeployment центра."""

    deployment_id: str
    profile_id: str
    revision: int
    state: str
    config: ProfileSpec


@dataclass(frozen=True)
class RejectedDeployment:
    """Назначение, которое нельзя применить из-за нарушения контракта.

    deployment_id равен None, если идентификатор не удалось извлечь безопасно.
    """

    deployment_id: str | None
    reason: str


@dataclass(frozen=True)
class AgentConfig:
    """Разобранный ответ центра: корректные назначения и отклонённые по отдельности."""

    host_id: str
    deployments: tuple[DeploymentSpec, ...]
    rejected: tuple[RejectedDeployment, ...] = ()


class AgentConfigParser:
    """Строго проверяет обязательное и терпимо относится к добавленным полям центра.

    Неизвестные дополнительные поля игнорируются, чтобы новое поле в центре не ломало
    агент. Неизвестные значения перечислений (state, уровень, протокол, шаблон) —
    ошибка: применять профиль, смысл которого неизвестен, нельзя. Нарушение в одном
    назначении не блокирует остальные: оно попадает в AgentConfig.rejected.
    """

    def parse(self, payload: object) -> AgentConfig:
        """Разбирает тело ответа; нарушение верхнего уровня поднимает ContractError."""
        root = self._mapping(payload, "$")
        host_id = self._uuid(self._required(root, "host_id", "$"), "$.host_id")
        raw = self._required(root, "deployments", "$")
        if not isinstance(raw, list):
            raise ContractError("$.deployments", "ожидается массив")
        accepted: list[DeploymentSpec] = []
        rejected: list[RejectedDeployment] = []
        seen: set[str] = set()
        for index, item in enumerate(raw):
            try:
                deployment = self._deployment(item, f"$.deployments[{index}]")
                if deployment.deployment_id in seen:
                    raise ContractError(
                        f"$.deployments[{index}].deployment_id", "идентификатор повторяется"
                    )
            except ContractError as error:
                rejected.append(RejectedDeployment(self._safe_deployment_id(item), str(error)))
                continue
            seen.add(deployment.deployment_id)
            accepted.append(deployment)
        return AgentConfig(host_id, tuple(accepted), tuple(rejected))

    def _deployment(self, raw: object, path: str) -> DeploymentSpec:
        item = self._mapping(raw, path)
        state = self._string(self._required(item, "state", path), f"{path}.state")
        if state != "planned":
            raise ContractError(f"{path}.state", "неподдерживаемое состояние назначения")
        return DeploymentSpec(
            deployment_id=self._uuid(
                self._required(item, "deployment_id", path), f"{path}.deployment_id"
            ),
            profile_id=self._uuid(self._required(item, "profile_id", path), f"{path}.profile_id"),
            revision=self._integer(
                self._required(item, "revision", path), f"{path}.revision", 1, 2**63 - 1
            ),
            state=state,
            config=self._profile(self._required(item, "config", path), f"{path}.config"),
        )

    def _profile(self, raw: object, path: str) -> ProfileSpec:
        item = self._mapping(raw, path)
        level = self._string(
            self._required(item, "interaction_level", path), f"{path}.interaction_level"
        )
        if level not in get_args(Level):
            raise ContractError(f"{path}.interaction_level", "неизвестный уровень взаимодействия")
        hostname = self._string(item.get("hostname", DEFAULT_HOSTNAME), f"{path}.hostname")
        if _HOSTNAME.fullmatch(hostname) is None:
            raise ContractError(f"{path}.hostname", "недопустимое имя узла")

        raw_services = self._required(item, "services", path)
        if not isinstance(raw_services, list):
            raise ContractError(f"{path}.services", "ожидается массив")
        if not MIN_SERVICES <= len(raw_services) <= MAX_SERVICES:
            raise ContractError(f"{path}.services", "недопустимое число сервисов")
        services = tuple(
            self._service(service, f"{path}.services[{index}]")
            for index, service in enumerate(raw_services)
        )
        raw_credentials = self._required(item, "credentials", path)
        if not isinstance(raw_credentials, list):
            raise ContractError(f"{path}.credentials", "ожидается массив")
        if len(raw_credentials) > MAX_CREDENTIALS:
            raise ContractError(f"{path}.credentials", "слишком много приманочных учётных записей")
        credentials = tuple(
            self._credential(credential, f"{path}.credentials[{index}]")
            for index, credential in enumerate(raw_credentials)
        )
        if len({credential.username for credential in credentials}) != len(credentials):
            raise ContractError(f"{path}.credentials", "имена пользователей повторяются")

        raw_high = item.get("high")
        high = None if raw_high is None else self._high(raw_high, f"{path}.high")
        profile = ProfileSpec(
            interaction_level=cast(Level, level),  # значение проверено выше
            hostname=hostname,
            services=services,
            logging=self._logging(item.get("logging", {}), f"{path}.logging"),
            channel=self._channel(item.get("channel", {}), f"{path}.channel"),
            high=high,
            credentials=credentials,
        )
        self._check_consistency(profile, path)
        return profile

    def _check_consistency(self, profile: ProfileSpec, path: str) -> None:
        """Повторяет перекрёстные проверки ConfigBase центра."""
        allowed = _PROTOCOLS_BY_LEVEL[profile.interaction_level]
        if any(service.protocol not in allowed for service in profile.services):
            raise ContractError(f"{path}.services", "протокол не поддерживается уровнем")
        if len({service.port for service in profile.services}) != len(profile.services):
            raise ContractError(f"{path}.services", "порты профиля повторяются")
        if profile.interaction_level == "high":
            if profile.high is None or len(profile.services) != 1:
                raise ContractError(f"{path}.high", "High требует параметры VM и один сервис")
        elif profile.high is not None:
            raise ContractError(f"{path}.high", "параметры VM разрешены только для High")

    def _service(self, raw: object, path: str) -> ServiceSpec:
        item = self._mapping(raw, path)
        protocol = self._string(self._required(item, "protocol", path), f"{path}.protocol")
        if protocol not in get_args(ServiceProtocol):
            raise ContractError(f"{path}.protocol", "неизвестный протокол")
        banner = self._string(item.get("banner", ""), f"{path}.banner")
        if len(banner) > MAX_BANNER_LENGTH or any(char in banner for char in "\r\n"):
            raise ContractError(f"{path}.banner", "баннер должен быть короткой одной строкой")
        return ServiceSpec(
            protocol=cast(ServiceProtocol, protocol),  # значение проверено выше
            port=self._integer(
                self._required(item, "port", path), f"{path}.port", MIN_PORT, MAX_PORT
            ),
            banner=banner,
        )

    def _credential(self, raw: object, path: str) -> CredentialSpec:
        item = self._mapping(raw, path)
        username = self._string(self._required(item, "username", path), f"{path}.username")
        if _USERNAME.fullmatch(username) is None:
            raise ContractError(f"{path}.username", "недопустимое имя пользователя")
        password = self._string(self._required(item, "password", path), f"{path}.password")
        if not password or any(ord(char) < 32 or ord(char) == 127 for char in password):
            # Значение пароля в сообщение не попадает.
            raise ContractError(f"{path}.password", "пароль пуст или содержит управляющие символы")
        return CredentialSpec(username, Secret(password))

    def _logging(self, raw: object, path: str) -> LoggingSpec:
        item = self._mapping(raw, path)
        return LoggingSpec(
            self._integer(
                item.get("max_payload_bytes", DEFAULT_MAX_PAYLOAD_BYTES),
                f"{path}.max_payload_bytes",
                MIN_PAYLOAD_BYTES,
                MAX_PAYLOAD_BYTES,
            )
        )

    def _channel(self, raw: object, path: str) -> ChannelSpec:
        item = self._mapping(raw, path)
        return ChannelSpec(
            flush_interval_ms=self._integer(
                item.get("flush_interval_ms", DEFAULT_FLUSH_INTERVAL_MS),
                f"{path}.flush_interval_ms",
                MIN_FLUSH_INTERVAL_MS,
                MAX_FLUSH_INTERVAL_MS,
            ),
            batch_size=self._integer(
                item.get("batch_size", DEFAULT_BATCH_SIZE),
                f"{path}.batch_size",
                MIN_BATCH_SIZE,
                MAX_BATCH_SIZE,
            ),
        )

    def _high(self, raw: object, path: str) -> HighSpec:
        item = self._mapping(raw, path)
        template = self._string(item.get("template", HIGH_TEMPLATE), f"{path}.template")
        if template != HIGH_TEMPLATE:
            raise ContractError(f"{path}.template", "неизвестный шаблон VM")
        return HighSpec(
            template=template,
            ttl_seconds=self._integer(
                item.get("ttl_seconds", DEFAULT_TTL_SECONDS),
                f"{path}.ttl_seconds",
                MIN_TTL_SECONDS,
                MAX_TTL_SECONDS,
            ),
            cpu=self._integer(item.get("cpu", DEFAULT_CPU), f"{path}.cpu", MIN_CPU, MAX_CPU),
            memory_mb=self._integer(
                item.get("memory_mb", DEFAULT_MEMORY_MB),
                f"{path}.memory_mb",
                MIN_MEMORY_MB,
                MAX_MEMORY_MB,
            ),
        )

    @staticmethod
    def _mapping(value: object, path: str) -> Mapping[str, object]:
        if not isinstance(value, Mapping):
            raise ContractError(path, "ожидается объект")
        return value

    @staticmethod
    def _required(item: Mapping[str, object], key: str, path: str) -> object:
        if key not in item:
            raise ContractError(f"{path}.{key}", "обязательное поле отсутствует")
        return item[key]

    @staticmethod
    def _string(value: object, path: str) -> str:
        if not isinstance(value, str):
            raise ContractError(path, "ожидается строка")
        if "\x00" in value:
            raise ContractError(path, "строка содержит NUL")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ContractError(path, "строка не является корректным Unicode") from error
        return value

    @staticmethod
    def _integer(value: object, path: str, minimum: int, maximum: int) -> int:
        """Булево значение не считается числом, хотя bool наследуется от int."""
        if type(value) is not int:
            raise ContractError(path, "ожидается целое число")
        if not minimum <= value <= maximum:
            raise ContractError(path, f"значение вне границ {minimum}..{maximum}")
        return value

    @classmethod
    def _uuid(cls, value: object, path: str) -> str:
        try:
            return str(UUID(cls._string(value, path)))
        except ValueError as error:
            raise ContractError(path, "ожидается UUID") from error

    @classmethod
    def _safe_deployment_id(cls, raw: object) -> str | None:
        """Достаёт идентификатор для отчёта об отказе, не доверяя остальному содержимому."""
        if not isinstance(raw, Mapping):
            return None
        try:
            return cls._uuid(raw.get("deployment_id"), "$")
        except ContractError:
            return None
