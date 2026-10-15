"""Драйвер Low: контейнер с ограниченной TCP/HTTP-ловушкой по профилю центра.

Драйвер не собирает и не скачивает образы и не знает о центре: он получает разобранное
назначение, переводит его в ContainerSpec и управляет одним контейнером слота. Общий
жизненный цикл слота реализован в `slot.py`; здесь остаются перевод профиля Low,
каталог журнала и пробы баннера TCP и ответа HTTP. Работа с настоящим Docker Engine в
этом модуле не проверялась (см. specs/runtime-drivers.md).
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar
from uuid import uuid4

from agent.runtime.contract import DeploymentSpec, Level, ServiceSpec
from agent.runtime.drivers.base import DriverError, DriverErrorCode
from agent.runtime.drivers.containers import (
    LABEL_DEPLOYMENT,
    LABEL_INSTANCE,
    LABEL_LEVEL,
    LABEL_MANAGED,
    LABEL_REVISION,
    BindMount,
    ContainerBackend,
    ContainerSpec,
    PortPublish,
)
from agent.runtime.drivers.health import (
    HealthCheckFailed,
    PortProbe,
    ReleaseTargets,
    check_probe_timings,
    probe_host,
)
from agent.runtime.drivers.slot import ContainerSlotDriver

__all__ = [
    "DEFAULT_JOURNAL_FILE_BYTES",
    "DEFAULT_JOURNAL_QUOTA_BYTES",
    "JOURNAL_CONTAINER_DIR",
    "LABEL_DEPLOYMENT",
    "LABEL_INSTANCE",
    "LABEL_LEVEL",
    "LABEL_MANAGED",
    "LABEL_REVISION",
    "LOW_HTTP_CONTAINER_PORT",
    "LOW_TCP_CONTAINER_PORT",
    "MIN_JOURNAL_FILE_BYTES",
    "HealthCheckFailed",
    "HealthTargets",
    "LowBlueprint",
    "LowDriver",
    "LowHealthCheck",
    "LowProfileTranslator",
    "LowSettings",
]

# Порты внутри контейнера фиксированы; публичный порт из профиля публикуется на хост.
LOW_TCP_CONTAINER_PORT = 2222
LOW_HTTP_CONTAINER_PORT = 8080
# Каталог журнала внутри контейнера: единственное место, куда ловушка может писать.
JOURNAL_CONTAINER_DIR = "/journal"
# Значения повторяют контракт Low; тест сверяет их с настройками самого журнала.
# Агент не импортирует ловушку: они собираются и запускаются отдельно.
MIN_JOURNAL_FILE_BYTES = 256 * 1024
DEFAULT_JOURNAL_FILE_BYTES = 8 * 1024 * 1024
DEFAULT_JOURNAL_QUOTA_BYTES = 256 * 1024 * 1024
_MAX_BANNER_WITH_CRLF = 255


@dataclass(frozen=True)
class LowSettings:
    """Настройки оператора хоста; профиль центра их не меняет."""

    image: str = "honeyforge-low:local"
    bind_address: str = "127.0.0.1"
    network: str | None = None
    # Корень журналов на хосте: каталог слота <journal_root>/<deployment_id> монтируется в
    # контейнер. None — файловый журнал выключен, ловушка пишет диагностику в stdout.
    journal_root: Path | None = None
    journal_max_bytes: int | None = None
    journal_quota_bytes: int | None = None
    start_timeout: float = 15.0
    probe_interval: float = 0.2
    probe_timeout: float = 1.0
    # Секунды на мягкую остановку (SIGTERM) перед принудительной (SIGKILL).
    stop_timeout: int = 10

    def __post_init__(self) -> None:
        ipaddress.ip_address(self.bind_address)
        # Негодные образ или сеть обнаруживаются при настройке, а не после удаления
        # работающего экземпляра.
        ContainerSpec.check_image(self.image)
        if self.network is not None:
            ContainerSpec.check_network(self.network)
        if type(self.stop_timeout) is not int or not 0 <= self.stop_timeout <= 25:
            raise ValueError("stop_timeout должен быть целым от 0 до 25 секунд")
        if self.journal_root is not None:
            object.__setattr__(self, "journal_root", Path(self.journal_root))
            if not Path(self.journal_root).is_absolute():
                raise ValueError("journal_root должен быть абсолютным путём текущей ОС")
            BindMount.check_source(str(self.journal_root))
        for name in ("journal_max_bytes", "journal_quota_bytes"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 1):
                raise ValueError(f"{name} должен быть положительным целым")
        file_bytes = (
            self.journal_max_bytes
            if self.journal_max_bytes is not None
            else DEFAULT_JOURNAL_FILE_BYTES
        )
        quota_bytes = (
            self.journal_quota_bytes
            if self.journal_quota_bytes is not None
            else DEFAULT_JOURNAL_QUOTA_BYTES
        )
        if file_bytes < MIN_JOURNAL_FILE_BYTES:
            raise ValueError(f"journal_max_bytes должен быть не меньше {MIN_JOURNAL_FILE_BYTES}")
        if quota_bytes < file_bytes:
            raise ValueError("journal_quota_bytes должен быть не меньше journal_max_bytes")
        check_probe_timings(self, ("start_timeout", "probe_interval", "probe_timeout"))


@dataclass(frozen=True)
class LowBlueprint:
    """Производное от профиля описание ловушки: без секретов и без номера экземпляра."""

    deployment_id: str
    revision: int
    tcp: ServiceSpec | None
    http: ServiceSpec | None
    command: tuple[str, ...]


@dataclass(frozen=True)
class HealthTargets:
    """Опубликованные порты для запуска/остановки и ожидаемый баннер запуска.

    `release_ports` — все порты профиля, освобождение которых подтверждает stop; при
    проверке они объединяются с `tcp_port` и `http_port`. Профиль, который драйвер
    отверг при start, может содержать больше одного сервиса каждого вида, и проверить
    нужно все его порты.
    """

    host: str
    tcp_port: int | None
    banner: bytes | None
    http_port: int | None
    release_ports: frozenset[int] = frozenset()


class LowProfileTranslator:
    """Переводит профиль центра в запуск контейнера; отказывает, а не искажает профиль."""

    def __init__(self, settings: LowSettings) -> None:
        self._settings = settings

    @staticmethod
    def container_name(deployment_id: str) -> str:
        """Имя контейнера слота; префикс honeyforge- защищает чужие контейнеры."""
        return f"honeyforge-low-{deployment_id}"

    def blueprint(self, deployment: DeploymentSpec) -> LowBlueprint:
        """Проверяет профиль на реализуемость; нарушение — UNSUPPORTED_PROFILE."""
        profile = deployment.config
        if profile.interaction_level != "low":
            raise self._unsupported("профиль не уровня low")
        tcp = [service for service in profile.services if service.protocol == "tcp"]
        http = [service for service in profile.services if service.protocol == "http"]
        if len(tcp) + len(http) != len(profile.services):
            raise self._unsupported("Low поддерживает только tcp и http")
        if len(tcp) > 1 or len(http) > 1:
            raise self._unsupported("Low открывает не более одного tcp и одного http сервиса")

        command = [
            "--host=0.0.0.0",
            f"--tcp-port={LOW_TCP_CONTAINER_PORT}",
            f"--http-port={LOW_HTTP_CONTAINER_PORT}",
            f"--max-input-bytes={profile.logging.max_payload_bytes}",
        ]
        if tcp:
            command.append(f"--tcp-public-port={tcp[0].port}")
            banner = tcp[0].banner
            if not banner:
                raise self._unsupported("tcp-сервису Low нужен непустой баннер")
            if len(banner) + 2 > _MAX_BANNER_WITH_CRLF or any(
                not 32 <= ord(char) <= 126 for char in banner
            ):
                raise self._unsupported("баннер tcp должен быть печатным ASCII")
            command.append(f"--banner={banner}")
        if http:
            command.append(f"--http-public-port={http[0].port}")
        if http and http[0].banner:
            body = http[0].banner
            if any(ord(char) < 32 or ord(char) == 127 for char in body):
                raise self._unsupported("текст http содержит управляющие символы")
            command.append(f"--http-body={body}")
        return LowBlueprint(
            deployment_id=deployment.deployment_id,
            revision=deployment.revision,
            tcp=tcp[0] if tcp else None,
            http=http[0] if http else None,
            command=tuple(command),
        )

    def journal_directory(self, deployment_id: str) -> Path | None:
        """Каталог журнала слота на хосте либо None, если файловый журнал выключен."""
        root = self._settings.journal_root
        return None if root is None else root / deployment_id

    def container_spec(self, blueprint: LowBlueprint, instance_id: str) -> ContainerSpec:
        """Описание запуска нового экземпляра: порты, метки, журнал и меры ограничения."""
        command = blueprint.command
        mounts: tuple[BindMount, ...] = ()
        journal = self.journal_directory(blueprint.deployment_id)
        if journal is not None:
            extra = [
                f"--journal-dir={JOURNAL_CONTAINER_DIR}",
                f"--instance-id={instance_id}",
                f"--deployment-id={blueprint.deployment_id}",
                f"--profile-revision={blueprint.revision}",
            ]
            if self._settings.journal_max_bytes is not None:
                extra.append(f"--journal-max-bytes={self._settings.journal_max_bytes}")
            if self._settings.journal_quota_bytes is not None:
                extra.append(f"--journal-quota-bytes={self._settings.journal_quota_bytes}")
            command = command + tuple(extra)
            mounts = (BindMount(str(journal), JOURNAL_CONTAINER_DIR),)
        publish: list[PortPublish] = []
        if blueprint.tcp is not None:
            publish.append(
                PortPublish(self._settings.bind_address, blueprint.tcp.port, LOW_TCP_CONTAINER_PORT)
            )
        if blueprint.http is not None:
            publish.append(
                PortPublish(
                    self._settings.bind_address, blueprint.http.port, LOW_HTTP_CONTAINER_PORT
                )
            )
        return ContainerSpec(
            name=self.container_name(blueprint.deployment_id),
            image=self._settings.image,
            command=command,
            publish=tuple(publish),
            mounts=mounts,
            labels=(
                (LABEL_MANAGED, "true"),
                (LABEL_LEVEL, "low"),
                (LABEL_DEPLOYMENT, blueprint.deployment_id),
                (LABEL_INSTANCE, instance_id),
                (LABEL_REVISION, str(blueprint.revision)),
            ),
            network=self._settings.network,
        )

    def _probe_host(self) -> str:
        """Пробы идут с хоста на bind-адрес; wildcard-адрес заменяется loopback."""
        return probe_host(self._settings.bind_address)

    def health_targets(self, blueprint: LowBlueprint) -> HealthTargets:
        """Проба идёт с хоста на опубликованные порты; wildcard-адрес заменяется loopback."""
        banner = None
        if blueprint.tcp is not None:
            banner = (blueprint.tcp.banner + "\r\n").encode("ascii")
        return HealthTargets(
            host=self._probe_host(),
            tcp_port=None if blueprint.tcp is None else blueprint.tcp.port,
            banner=banner,
            http_port=None if blueprint.http is None else blueprint.http.port,
        )

    def release_targets(self, deployment: DeploymentSpec) -> HealthTargets:
        """Порты для подтверждения остановки: все tcp/http-порты профиля, без баннеров.

        Остановка не должна зависеть от того, принимает ли драйвер текст и состав
        профиля. Профиль, отвергнутый при start (управляющий символ в тексте HTTP,
        недопустимый баннер или несколько сервисов одного вида, которые центр
        допускает), всё равно задаёт порты, освобождение которых нужно подтвердить;
        иначе такую ловушку нельзя было бы перевести в stopped. Проверяется только
        уровень: профиль не уровня low этот драйвер не обслуживает.
        """
        if deployment.config.interaction_level != "low":
            raise self._unsupported("профиль не уровня low")
        ports = [s.port for s in deployment.config.services if s.protocol in ("tcp", "http")]
        tcp = [s.port for s in deployment.config.services if s.protocol == "tcp"]
        http = [s.port for s in deployment.config.services if s.protocol == "http"]
        return HealthTargets(
            host=self._probe_host(),
            tcp_port=tcp[0] if tcp else None,
            banner=None,
            http_port=http[0] if http else None,
            release_ports=frozenset(ports),
        )

    @staticmethod
    def _unsupported(reason: str) -> DriverError:
        return DriverError(DriverErrorCode.UNSUPPORTED_PROFILE, reason)


class LowHealthCheck(PortProbe[HealthTargets]):
    """Проверяет, что опубликованные порты отвечают так, как задано профилем.

    Каждая проба порождает события в журнале Low, поэтому проверка выполняется при
    запуске, а не при периодическом inspect (см. открытые вопросы в спецификации).
    """

    async def wait_until_released(self, targets: HealthTargets) -> bool:
        """Ждёт отказа подключения ко всем tcp/http-портам профиля за общий срок."""
        ports = {port for port in (targets.tcp_port, targets.http_port) if port is not None}
        ports |= targets.release_ports
        return await self.wait_for_release(ReleaseTargets(targets.host, frozenset(ports)))

    async def _probe_once(self, targets: HealthTargets, deadline: float) -> None:
        if targets.tcp_port is not None and targets.banner is not None:
            await self._probe_tcp(
                targets.host, targets.tcp_port, targets.banner, self._budget(deadline)
            )
        if targets.http_port is not None:
            await self._probe_http(targets.host, targets.http_port, self._budget(deadline))

    async def _probe_tcp(self, host: str, port: int, expected: bytes, timeout: float) -> None:
        async with asyncio.timeout(timeout):
            reader, writer = await asyncio.open_connection(host, port)
            try:
                # Баннер оканчивается переводом строки, поэтому читаем строку, а не точное
                # число байт: расхождение длины обнаруживается сразу, без ожидания таймаута.
                received = await reader.readline()
            finally:
                await self._close(writer)
        if received != expected:
            raise HealthCheckFailed("tcp: баннер не совпал с профилем")

    async def _probe_http(self, host: str, port: int, timeout: float) -> None:
        async with asyncio.timeout(timeout):
            reader, writer = await asyncio.open_connection(host, port)
            try:
                writer.write(b"HEAD / HTTP/1.0\r\nUser-Agent: honeyforge-healthcheck\r\n\r\n")
                await writer.drain()
                status_line = await reader.readline()
            finally:
                await self._close(writer)
        if not status_line.startswith(b"HTTP/1.1 200"):
            raise HealthCheckFailed("http: ответ не 200")


class LowDriver(ContainerSlotDriver[LowBlueprint]):
    """Один слот Low: одно назначение, один контейнер, новый instance_id на каждый запуск."""

    level: ClassVar[Level] = "low"

    def __init__(
        self,
        deployment_id: str,
        backend: ContainerBackend,
        settings: LowSettings | None = None,
        health: LowHealthCheck | None = None,
        new_instance_id: Callable[[], str] = lambda: str(uuid4()),
    ) -> None:
        self._settings = settings if settings is not None else LowSettings()
        self._translator = LowProfileTranslator(self._settings)
        self._health = (
            health
            if health is not None
            else LowHealthCheck(
                self._settings.start_timeout,
                self._settings.probe_interval,
                self._settings.probe_timeout,
            )
        )
        self._make_instance_id = new_instance_id
        super().__init__(deployment_id, backend, self._settings.stop_timeout)

    def _container_name(self) -> str:
        return LowProfileTranslator.container_name(self.deployment_id)

    def _new_instance_id(self) -> str:
        return self._make_instance_id()

    def _translate_profile(self, deployment: DeploymentSpec) -> LowBlueprint:
        return self._translator.blueprint(deployment)

    def _container_spec(self, blueprint: LowBlueprint, instance_id: str) -> ContainerSpec:
        return self._translator.container_spec(blueprint, instance_id)

    async def _check_healthy(self, blueprint: LowBlueprint) -> None:
        await self._health.wait_until_healthy(self._translator.health_targets(blueprint))

    async def _check_released(self, deployment: DeploymentSpec) -> bool:
        return await self._health.wait_until_released(self._translator.release_targets(deployment))

    def _prepare_host(self, blueprint: LowBlueprint, instance_id: str, spec: ContainerSpec) -> None:
        """Готовит каталог журнала для пользователя контейнера, не переходя по ссылке слота.

        Ошибка подготовки не меняет работающий экземпляр. На Windows os.chown отсутствует;
        локальные тесты используют текущего пользователя вместо пользователя контейнера.
        Проверка битов доступа не доказывает доступ при ACL или read-only файловой системе.
        """
        directory = self._translator.journal_directory(self.deployment_id)
        if directory is None:
            return
        try:
            directory.mkdir(parents=True, exist_ok=True, mode=0o750)
            observed = directory.lstat()
            if stat.S_ISLNK(observed.st_mode) or not stat.S_ISDIR(observed.st_mode):
                raise OSError("каталог журнала не может быть ссылкой или обычным файлом")
            # Владелец должен читать каталог для расчёта квоты и создавать в нём файлы.
            # Уже существующие права не расширяем: отказ происходит до остановки ловушки.
            if observed.st_mode & stat.S_IRWXU != stat.S_IRWXU:
                raise OSError("владельцу каталога журнала нужны права чтения, записи и прохода")
            uid, gid = map(int, spec.user.split(":"))
            chown = getattr(os, "chown", None)
            if chown is not None and (observed.st_uid, observed.st_gid) != (uid, gid):
                chown(directory, uid, gid, follow_symlinks=False)
        except OSError as error:
            raise DriverError(
                DriverErrorCode.START_FAILED, "каталог журнала недоступен для подготовки"
            ) from error
