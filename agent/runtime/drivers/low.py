"""Драйвер Low: контейнер с ограниченной TCP/HTTP-ловушкой по профилю центра.

Драйвер не собирает и не скачивает образы и не знает о центре: он получает разобранное
назначение, переводит его в ContainerSpec и управляет одним контейнером слота. Работа с
настоящим Docker Engine в этом модуле не проверялась (см. specs/runtime-drivers.md).
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar
from uuid import UUID, uuid4

from agent.runtime.contract import DeploymentSpec, Level, ServiceSpec
from agent.runtime.drivers.base import (
    Driver,
    DriverError,
    DriverErrorCode,
    DriverState,
    DriverStatus,
)
from agent.runtime.drivers.containers import (
    LABEL_MANAGED,
    BackendError,
    BindMount,
    ContainerBackend,
    ContainerObservation,
    ContainerSpec,
    PortPublish,
)

# Порты внутри контейнера фиксированы; публичный порт из профиля публикуется на хост.
LOW_TCP_CONTAINER_PORT = 2222
LOW_HTTP_CONTAINER_PORT = 8080
LABEL_DEPLOYMENT = "honeyforge.deployment_id"
LABEL_INSTANCE = "honeyforge.instance_id"
LABEL_REVISION = "honeyforge.revision"
LABEL_LEVEL = "honeyforge.level"
# Каталог журнала внутри контейнера: единственное место, куда ловушка может писать.
JOURNAL_CONTAINER_DIR = "/journal"
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

    def __post_init__(self) -> None:
        ipaddress.ip_address(self.bind_address)
        if self.journal_root is not None:
            object.__setattr__(self, "journal_root", Path(self.journal_root))
            if not Path(self.journal_root).is_absolute():
                raise ValueError("journal_root должен быть абсолютным путём")
        for name in ("journal_max_bytes", "journal_quota_bytes"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 1):
                raise ValueError(f"{name} должен быть положительным целым")
        for name in ("start_timeout", "probe_interval", "probe_timeout"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"{name} должен быть положительным числом")


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
    """Что проверять после запуска: опубликованные порты и ожидаемый баннер."""

    host: str
    tcp_port: int | None
    banner: bytes | None
    http_port: int | None


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

    def health_targets(self, blueprint: LowBlueprint) -> HealthTargets:
        """Проба идёт с хоста на опубликованные порты; wildcard-адрес заменяется loopback."""
        address = ipaddress.ip_address(self._settings.bind_address)
        host = self._settings.bind_address
        if address.is_unspecified:
            host = "127.0.0.1" if address.version == 4 else "::1"
        banner = None
        if blueprint.tcp is not None:
            banner = (blueprint.tcp.banner + "\r\n").encode("ascii")
        return HealthTargets(
            host=host,
            tcp_port=None if blueprint.tcp is None else blueprint.tcp.port,
            banner=banner,
            http_port=None if blueprint.http is None else blueprint.http.port,
        )

    @staticmethod
    def _unsupported(reason: str) -> DriverError:
        return DriverError(DriverErrorCode.UNSUPPORTED_PROFILE, reason)


class HealthCheckFailed(Exception):
    """Ловушка не ответила ожидаемо за отведённое время; причина безопасна для показа."""


class LowHealthCheck:
    """Проверяет, что опубликованные порты отвечают так, как задано профилем.

    Каждая проба порождает события в журнале Low, поэтому проверка выполняется при
    запуске, а не при периодическом inspect (см. открытые вопросы в спецификации).
    """

    def __init__(self, timeout: float, interval: float, probe_timeout: float) -> None:
        self._timeout = timeout
        self._interval = interval
        self._probe_timeout = probe_timeout

    async def wait_until_healthy(self, targets: HealthTargets) -> None:
        """Повторяет пробу до успеха или до срока; последняя причина попадает в ошибку."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._timeout
        last = "проверка не выполнялась"
        while True:
            # Проба не должна выходить за общий срок: ограничиваем её оставшимся временем.
            probe_timeout = min(self._probe_timeout, max(deadline - loop.time(), 0.05))
            try:
                await self._probe_once(targets, probe_timeout)
                return
            except (OSError, EOFError, TimeoutError, HealthCheckFailed) as error:
                last = self._describe(error)
            if loop.time() >= deadline:
                raise HealthCheckFailed(last)
            await asyncio.sleep(self._interval)

    @staticmethod
    def _describe(error: Exception) -> str:
        """Краткая безопасная причина; тексты системных ошибок зависят от ОС и языка."""
        if isinstance(error, HealthCheckFailed):
            return str(error)
        if isinstance(error, TimeoutError):
            return "нет ответа в срок"
        if isinstance(error, ConnectionRefusedError):
            return "порт не принимает соединения"
        if isinstance(error, EOFError):
            return "соединение закрыто до ответа"
        return f"ошибка сети ({type(error).__name__})"

    async def _probe_once(self, targets: HealthTargets, timeout: float) -> None:
        if targets.tcp_port is not None and targets.banner is not None:
            await self._probe_tcp(targets.host, targets.tcp_port, targets.banner, timeout)
        if targets.http_port is not None:
            await self._probe_http(targets.host, targets.http_port, timeout)

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

    @staticmethod
    async def _close(writer: asyncio.StreamWriter) -> None:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()


class LowDriver(Driver):
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
        super().__init__()
        self.deployment_id = str(UUID(deployment_id))
        self._backend = backend
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
        self._new_instance_id = new_instance_id
        self._name = self._translator.container_name(self.deployment_id)
        self._state = DriverState.STOPPED
        self._healthy = False
        self._instance_id: str | None = None
        self._revision: int | None = None
        self._blueprint: LowBlueprint | None = None
        self._detail = ""

    def _status(self) -> DriverStatus:
        return DriverStatus(
            self._state, self._healthy, self._instance_id, self._revision, self._detail
        )

    async def _start(self, deployment: DeploymentSpec) -> DriverStatus:
        if self._state is not DriverState.STOPPED:
            raise DriverError(DriverErrorCode.INVALID_STATE, "start допустим только из STOPPED")
        return await self._launch(self._translate(deployment))

    async def _apply_config(self, deployment: DeploymentSpec) -> DriverStatus:
        if self._state is DriverState.STOPPED:
            raise DriverError(DriverErrorCode.INVALID_STATE, "для остановленного слота нужен start")
        # Профиль проверяется до удаления работающего экземпляра: негодный профиль
        # не должен убивать исправную ловушку.
        blueprint = self._translate(deployment)
        await self._discard()
        return await self._launch(blueprint)

    async def _reset(self) -> DriverStatus:
        if self._state is DriverState.STOPPED or self._blueprint is None:
            raise DriverError(DriverErrorCode.INVALID_STATE, "нет запущенного экземпляра и профиля")
        blueprint = self._blueprint
        await self._discard()
        return await self._launch(blueprint)

    async def _stop(self) -> DriverStatus:
        await self._discard()
        self._state = DriverState.STOPPED
        self._healthy = False
        self._revision = None
        self._detail = ""
        return self._status()

    async def _inspect(self) -> DriverStatus:
        try:
            observed = await self._backend.inspect(self._name)
        except BackendError as error:
            raise self._driver_error(DriverErrorCode.BACKEND_UNAVAILABLE, error) from error
        if observed is None:
            if self._state is DriverState.RUNNING:
                self._mark_failed("контейнер исчез")
            return self._status()
        if observed.labels.get(LABEL_MANAGED) != "true":
            self._mark_failed("имя слота занято контейнером, не принадлежащим HoneyForge")
            return self._status()
        if not observed.running:
            self._mark_failed(f"контейнер остановился (код {observed.exit_code})")
            return self._status()
        if self._state is DriverState.FAILED:
            # FAILED «липкий»: пока драйвер не выполнил успешную операцию, живой контейнер
            # (например, не удалившийся при stop) не превращается обратно в RUNNING.
            return self._status()
        if self._state is DriverState.RUNNING and observed.labels.get(LABEL_INSTANCE) == (
            self._instance_id
        ):
            return self._status()
        return self._adopt(observed)

    def _adopt(self, observed: ContainerObservation) -> DriverStatus:
        """Принимает работающий контейнер, которого этот драйвер не запускал.

        Так бывает после перезапуска агента. Внутреннее состояние становится RUNNING, но
        здоровье не заявляется: этим драйвером оно не проверялось. Профиль неизвестен,
        поэтому reset недоступен, а apply_config и stop работают.
        """
        instance_id = observed.labels.get(LABEL_INSTANCE)
        revision = observed.labels.get(LABEL_REVISION)
        try:
            instance = str(UUID(instance_id)) if instance_id else None
        except ValueError:
            instance = None
        self._state = DriverState.RUNNING
        self._healthy = False
        self._instance_id = instance
        self._revision = int(revision) if revision and revision.isdecimal() else None
        self._blueprint = None
        self._detail = "экземпляр обнаружен, здоровье этим драйвером не проверялось"
        return self._status()

    def _translate(self, deployment: DeploymentSpec) -> LowBlueprint:
        if deployment.deployment_id != self.deployment_id:
            raise DriverError(DriverErrorCode.INVALID_STATE, "назначение относится к другому слоту")
        return self._translator.blueprint(deployment)

    async def _launch(self, blueprint: LowBlueprint) -> DriverStatus:
        """Создаёт новый экземпляр; при любой неудаче ресурсы удалены, состояние FAILED."""
        self._blueprint = blueprint
        instance_id = self._new_instance_id()
        spec = self._translator.container_spec(blueprint, instance_id)
        try:
            await self._free_slot()
            self._prepare_journal_directory()
            try:
                await self._backend.run(spec)
            except BackendError as error:
                await self._discard()
                raise self._driver_error(DriverErrorCode.START_FAILED, error) from error
            try:
                await self._health.wait_until_healthy(self._translator.health_targets(blueprint))
            except HealthCheckFailed as error:
                await self._discard()
                raise DriverError(DriverErrorCode.HEALTHCHECK_FAILED, str(error)) from error
        except DriverError as error:
            self._mark_failed(error.message or error.code.value)
            raise
        self._state = DriverState.RUNNING
        self._healthy = True
        self._instance_id = instance_id
        self._revision = blueprint.revision
        self._detail = ""
        return self._status()

    def _prepare_journal_directory(self) -> None:
        """Создаёт каталог журнала слота. Владельца и права задаёт подготовка хоста."""
        directory = self._translator.journal_directory(self.deployment_id)
        if directory is None:
            return
        try:
            directory.mkdir(parents=True, exist_ok=True, mode=0o750)
        except OSError as error:
            raise DriverError(
                DriverErrorCode.START_FAILED, "каталог журнала недоступен для создания"
            ) from error

    async def _free_slot(self) -> None:
        """Перед запуском убирает остаток прежнего запуска; чужой контейнер не трогает."""
        try:
            existing = await self._backend.inspect(self._name)
        except BackendError as error:
            raise self._driver_error(DriverErrorCode.BACKEND_UNAVAILABLE, error) from error
        if existing is None:
            return
        if existing.labels.get(LABEL_MANAGED) != "true":
            raise DriverError(
                DriverErrorCode.START_FAILED,
                "имя слота занято контейнером, не принадлежащим HoneyForge",
            )
        await self._discard()

    async def _discard(self) -> None:
        """Удаляет контейнер слота. Неудача — CLEANUP_FAILED и состояние FAILED."""
        try:
            await self._backend.remove(self._name)
        except BackendError as error:
            self._mark_failed("не удалось удалить контейнер")
            raise DriverError(DriverErrorCode.CLEANUP_FAILED, error.message) from error

    def _mark_failed(self, detail: str) -> None:
        self._state = DriverState.FAILED
        self._healthy = False
        self._revision = None
        self._detail = detail

    @staticmethod
    def _driver_error(code: DriverErrorCode, error: BackendError) -> DriverError:
        if error.unavailable:
            return DriverError(DriverErrorCode.BACKEND_UNAVAILABLE, error.message)
        return DriverError(code, error.message)
