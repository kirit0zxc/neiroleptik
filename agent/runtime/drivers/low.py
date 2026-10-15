"""Драйвер Low: контейнер с ограниченной TCP/HTTP-ловушкой по профилю центра.

Драйвер не собирает и не скачивает образы и не знает о центре: он получает разобранное
назначение, переводит его в ContainerSpec и управляет одним контейнером слота. Работа с
настоящим Docker Engine в этом модуле не проверялась (см. specs/runtime-drivers.md).
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import math
import os
import re
import stat
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
# Значения повторяют контракт Low; тест сверяет их с настройками самого журнала.
# Агент не импортирует ловушку: они собираются и запускаются отдельно.
MIN_JOURNAL_FILE_BYTES = 256 * 1024
DEFAULT_JOURNAL_FILE_BYTES = 8 * 1024 * 1024
DEFAULT_JOURNAL_QUOTA_BYTES = 256 * 1024 * 1024
_MAX_BANNER_WITH_CRLF = 255
_FOREIGN = "имя слота занято контейнером, не принадлежащим HoneyForge"
_OTHER_INSTANCE = "имя слота занято другим экземпляром HoneyForge"
_OTHER_SLOT = "имя слота занято контейнером другого назначения или уровня"
_CONTAINER_ID = re.compile(r"[0-9a-f]{64}")
_REVISION = re.compile(r"[1-9][0-9]{0,18}")


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
        for name in ("start_timeout", "probe_interval", "probe_timeout"):
            value = getattr(self, name)
            message = f"{name} должен быть конечным положительным числом"
            try:
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or value <= 0
                ):
                    raise ValueError(message)
            except OverflowError as error:
                # Таймеры asyncio используют float; огромный int непригоден даже при
                # математической конечности. Отказываем до изменения работающего слота.
                raise ValueError(message) from error


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
        address = ipaddress.ip_address(self._settings.bind_address)
        if address.is_unspecified:
            return "127.0.0.1" if address.version == 4 else "::1"
        return self._settings.bind_address

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
            try:
                await self._probe_once(targets, deadline)
                return
            except (OSError, EOFError, TimeoutError, HealthCheckFailed) as error:
                last = self._describe(error)
            if loop.time() >= deadline:
                raise HealthCheckFailed(last)
            await asyncio.sleep(min(self._interval, max(deadline - loop.time(), 0.0)))

    async def wait_until_released(self, targets: HealthTargets) -> bool:
        """Ждёт явного отказа TCP-подключения ко всем портам за один общий срок.

        Таймаут и другие ошибки сети не доказывают освобождение. Порты проверяются
        параллельно; probe_timeout не прерывает медленный отказ Windows loopback.
        Никакие данные или команды остановки чужому процессу не отправляются.
        """
        ports = {port for port in (targets.tcp_port, targets.http_port) if port is not None}
        ports |= targets.release_ports
        if not ports:
            return True
        address = ipaddress.ip_address(targets.host)
        host = targets.host
        if address.is_unspecified:
            host = "127.0.0.1" if address.version == 4 else "::1"
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._timeout
        while loop.time() < deadline:
            try:
                async with asyncio.timeout_at(deadline):
                    released = await asyncio.gather(
                        *(self._connection_refused(host, port) for port in ports)
                    )
            except TimeoutError:
                return False
            if all(released):
                return loop.time() <= deadline
            await asyncio.sleep(min(self._interval, max(deadline - loop.time(), 0.0)))
        return False

    async def _connection_refused(self, host: str, port: int) -> bool:
        """Отказ connect подтверждает отсутствие слушателя; успешный connect — нет."""
        try:
            _, writer = await asyncio.open_connection(host, port)
        except ConnectionRefusedError:
            return True
        except OSError:
            return False
        await self._close(writer)
        return False

    def _budget(self, deadline: float) -> float:
        """Время на одну пробу: не больше probe_timeout и не дальше общего срока."""
        remaining = deadline - asyncio.get_running_loop().time()
        return min(self._probe_timeout, max(remaining, 0.05))

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
        *,
        translator: LowProfileTranslator | None = None,
    ) -> None:
        super().__init__()
        self.deployment_id = str(UUID(deployment_id))
        self._backend = backend
        self._settings = settings if settings is not None else LowSettings()
        self._translator = translator or LowProfileTranslator(self._settings)
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

    async def observe(self) -> ContainerObservation | None:
        """Читает реальный контейнер независимо от липкого FAILED, без мутаций.

        Исполнитель использует наблюдение для ACK после неудачной остановки. Прежде
        чем доверять меткам, проверяем принадлежность слоту и полноту идентичности.
        """
        return await self._serialized_observation()

    async def _serialized_observation(self) -> ContainerObservation | None:
        # Базовая сериализация типизирована результатом DriverStatus; сохраняем тот же
        # замок для raw-наблюдения и не создаём второй независимый замок слота.
        async with self._operation_lock:
            return await self._observe()

    async def _observe(self) -> ContainerObservation | None:
        try:
            observed = await self._backend.inspect(self._name)
        except BackendError as error:
            raise self._driver_error(DriverErrorCode.BACKEND_UNAVAILABLE, error) from error
        if observed is not None:
            self._require_owned(observed, DriverErrorCode.INVALID_STATE)
            self._identity(observed, DriverErrorCode.INVALID_STATE)
        return observed

    async def wait_until_released(self, deployment: DeploymentSpec) -> bool:
        """Проверяет порты профиля под замком слота, не меняя состояние или Docker.

        Исполнитель сначала отдельно подтверждает отсутствие контейнера через
        observe(); сетевой таймаут этого метода не разрешает успешный stopped ACK.
        """
        async with self._operation_lock:
            if deployment.deployment_id != self.deployment_id:
                raise DriverError(
                    DriverErrorCode.INVALID_STATE, "назначение относится к другому слоту"
                )
            return await self._health.wait_until_released(
                self._translator.release_targets(deployment)
            )

    async def recover(
        self, deployment: DeploymentSpec, expected_instance_id: str | None
    ) -> DriverStatus:
        """Восстанавливает заранее сохранённый экземпляр, не запуская его повторно.

        Метки и неизменяемый ID проверяются до и после сетевой пробы. Неудачная проба
        не запускает очистку: право на следующую мутацию отдельно определяет lease.
        """
        return await self._serialized(self._recover(deployment, expected_instance_id))

    async def _recover(
        self, deployment: DeploymentSpec, expected_instance_id: str | None
    ) -> DriverStatus:
        blueprint = self._translate(deployment)
        try:
            expected = None if expected_instance_id is None else str(UUID(expected_instance_id))
        except (ValueError, TypeError, AttributeError) as error:
            raise DriverError(
                DriverErrorCode.INVALID_STATE, "некорректный UUID восстановления"
            ) from error
        observed = await self._observe()
        self._healthy = False
        if observed is None:
            self._state = DriverState.STOPPED
            self._instance_id = expected
            self._revision = None
            self._blueprint = blueprint
            self._detail = ""
            return self._status()
        instance_id, revision = self._identity(observed, DriverErrorCode.INVALID_STATE)
        if instance_id != expected or revision != deployment.revision:
            self._mark_failed("контейнер не совпадает с сохраняемой попыткой запуска")
            raise DriverError(DriverErrorCode.INVALID_STATE, self._detail)
        self._instance_id = instance_id
        self._blueprint = blueprint
        if not observed.running:
            self._state = DriverState.STOPPED
            self._revision = None
            self._detail = "сохранённый экземпляр остановлен"
            return self._status()
        try:
            await self._health.wait_until_healthy(self._translator.health_targets(blueprint))
        except HealthCheckFailed as error:
            self._mark_failed(str(error))
            raise DriverError(DriverErrorCode.HEALTHCHECK_FAILED, str(error)) from error
        confirmed = await self._observe()
        if (
            confirmed is None
            or not confirmed.running
            or confirmed.container_id != observed.container_id
            or self._identity(confirmed, DriverErrorCode.INVALID_STATE) != (instance_id, revision)
        ):
            self._mark_failed("контейнер изменился во время проверки восстановления")
            raise DriverError(DriverErrorCode.INVALID_STATE, self._detail)
        self._state = DriverState.RUNNING
        self._healthy = True
        self._revision = revision
        self._detail = ""
        return self._status()

    async def _start(self, deployment: DeploymentSpec) -> DriverStatus:
        if self._state is not DriverState.STOPPED:
            raise DriverError(DriverErrorCode.INVALID_STATE, "start допустим только из STOPPED")
        prepared = self._prepare(self._translate(deployment))
        await self._clear_slot(DriverErrorCode.START_FAILED)
        return await self._launch(prepared)

    async def _apply_config(self, deployment: DeploymentSpec) -> DriverStatus:
        if self._state is DriverState.STOPPED:
            raise DriverError(DriverErrorCode.INVALID_STATE, "для остановленного слота нужен start")
        # Профиль и параметры запуска проверяются до удаления работающего экземпляра:
        # негодная ревизия или настройка не должна убивать исправную ловушку.
        prepared = self._prepare(self._translate(deployment))
        await self._clear_slot(DriverErrorCode.CLEANUP_FAILED)
        return await self._launch(prepared)

    async def _reset(self) -> DriverStatus:
        if self._state is DriverState.STOPPED or self._blueprint is None:
            raise DriverError(DriverErrorCode.INVALID_STATE, "нет запущенного экземпляра и профиля")
        prepared = self._prepare(self._blueprint)
        await self._clear_slot(DriverErrorCode.CLEANUP_FAILED)
        return await self._launch(prepared)

    async def _stop(self) -> DriverStatus:
        await self._clear_slot(DriverErrorCode.CLEANUP_FAILED)
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
        ownership_problem = self._ownership_problem(observed)
        if ownership_problem is not None:
            # Повторная проверка не должна стирать исходную причину неудачного запуска,
            # если аварийная очистка уже отказалась трогать чужой контейнер.
            if self._state is DriverState.FAILED and self._detail:
                if ownership_problem not in self._detail:
                    self._detail = f"{ownership_problem}; {self._detail}"
            else:
                self._mark_failed(ownership_problem)
            return self._status()
        try:
            self._identity(observed, DriverErrorCode.INVALID_STATE)
        except DriverError:
            return self._status()
        if not observed.running:
            if self._state is DriverState.RUNNING:
                self._mark_failed(f"контейнер остановился (код {observed.exit_code})")
            elif self._state is DriverState.STOPPED:
                # Например, после перезагрузки хоста (restart=no): слот остаётся STOPPED,
                # start заменит остаток прежнего запуска.
                self._detail = (
                    f"найден остановленный контейнер прежнего запуска (код {observed.exit_code})"
                )
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
        здоровье не заявляется: этим драйвером оно не проверялось. instance_id и revision
        берутся из меток контейнера. Профиль неизвестен, поэтому reset недоступен, а
        apply_config и stop работают.
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

    def _prepare(self, blueprint: LowBlueprint) -> _PreparedLaunch:
        """Готовит новый экземпляр до удаления прежнего: ошибки параметров — до удаления.

        Единственный побочный эффект — идемпотентное создание каталога журнала слота:
        недоступный каталог обнаруживается, пока исправный экземпляр ещё работает.
        """
        instance_id = self._new_instance_id()
        try:
            spec = self._translator.container_spec(blueprint, instance_id)
        except ValueError as error:
            raise DriverError(
                DriverErrorCode.START_FAILED, f"недопустимые параметры запуска: {error}"
            ) from error
        self._prepare_journal_directory(spec.user)
        return _PreparedLaunch(blueprint, instance_id, spec)

    async def _launch(self, prepared: _PreparedLaunch) -> DriverStatus:
        """Создаёт экземпляр; при неудаче пытается очистить свои ресурсы, состояние FAILED."""
        self._blueprint = prepared.blueprint
        try:
            try:
                await self._backend.run(prepared.spec)
            except BackendError as error:
                # docker run мог создать контейнер до сбоя: убираем только свой ресурс.
                await self._cleanup_after(error.message, prepared.instance_id)
                raise self._driver_error(DriverErrorCode.START_FAILED, error) from error
            try:
                await self._health.wait_until_healthy(
                    self._translator.health_targets(prepared.blueprint)
                )
            except HealthCheckFailed as error:
                await self._cleanup_after(str(error), prepared.instance_id)
                raise DriverError(DriverErrorCode.HEALTHCHECK_FAILED, str(error)) from error
        except DriverError as error:
            self._mark_failed(error.message or error.code.value)
            raise
        self._state = DriverState.RUNNING
        self._healthy = True
        self._instance_id = prepared.instance_id
        self._revision = prepared.blueprint.revision
        self._detail = ""
        return self._status()

    def _prepare_journal_directory(self, user: str) -> None:
        """Готовит каталог для пользователя контейнера, не переходя по ссылке слота.

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
            uid, gid = map(int, user.split(":"))
            chown = getattr(os, "chown", None)
            if chown is not None and (observed.st_uid, observed.st_gid) != (uid, gid):
                chown(directory, uid, gid, follow_symlinks=False)
        except OSError as error:
            raise DriverError(
                DriverErrorCode.START_FAILED, "каталог журнала недоступен для подготовки"
            ) from error

    async def _clear_slot(
        self, foreign_code: DriverErrorCode, expected_instance_id: str | None = None
    ) -> None:
        """Убирает свой контейнер слота; аварийная очистка ограничена конкретной попыткой."""
        try:
            existing = await self._backend.inspect(self._name)
        except BackendError as error:
            raise self._driver_error(DriverErrorCode.BACKEND_UNAVAILABLE, error) from error
        if existing is None:
            return
        self._require_owned(existing, foreign_code)
        self._identity(existing, foreign_code)
        if (
            expected_instance_id is not None
            and existing.labels.get(LABEL_INSTANCE) != expected_instance_id
        ):
            self._mark_failed(_OTHER_INSTANCE)
            raise DriverError(foreign_code, _OTHER_INSTANCE)
        await self._remove(existing)

    def _ownership_problem(self, observed: ContainerObservation) -> str | None:
        """Имя и managed не заменяют метки конкретного назначения и уровня."""
        if observed.labels.get(LABEL_MANAGED) != "true":
            return _FOREIGN
        if (
            observed.name != self._name
            or observed.labels.get(LABEL_DEPLOYMENT) != self.deployment_id
            or observed.labels.get(LABEL_LEVEL) != self.level
        ):
            return _OTHER_SLOT
        return None

    def _require_owned(self, observed: ContainerObservation, code: DriverErrorCode) -> None:
        problem = self._ownership_problem(observed)
        if problem is not None:
            self._mark_failed(problem)
            raise DriverError(code, problem)

    def _identity(self, observed: ContainerObservation, code: DriverErrorCode) -> tuple[str, int]:
        """Отказывает до доверия меткам и удаления ресурса с неполной идентичностью."""
        try:
            instance_id = str(UUID(observed.labels[LABEL_INSTANCE]))
            revision_text = observed.labels[LABEL_REVISION]
            if _REVISION.fullmatch(revision_text) is None or int(revision_text) > 2**63 - 1:
                raise ValueError("revision")
            revision = int(revision_text)
        except (KeyError, ValueError, TypeError, AttributeError) as error:
            self._mark_failed("метки контейнера не содержат допустимые instance_id и revision")
            raise DriverError(code, self._detail) from error
        if (
            not isinstance(observed.container_id, str)
            or _CONTAINER_ID.fullmatch(observed.container_id) is None
        ):
            self._mark_failed("не получен допустимый неизменяемый идентификатор контейнера")
            raise DriverError(code, self._detail)
        return instance_id, revision

    async def _remove(self, observed: ContainerObservation) -> None:
        """Мягкая остановка, затем удаление. Неудача удаления — CLEANUP_FAILED и FAILED.

        SIGTERM даёт ловушке закрыть соединения и журнал; если мягкая остановка не удалась,
        принудительное удаление всё равно завершает процесс. Вызывается только после
        проверки принадлежности контейнера в _clear_slot. Обе операции используют
        неизменяемый ID наблюдавшегося контейнера: новое назначение имени не затрагивается.
        """
        if (
            not isinstance(observed.container_id, str)
            or _CONTAINER_ID.fullmatch(observed.container_id) is None
        ):
            message = "не получен допустимый неизменяемый идентификатор контейнера"
            self._mark_failed(message)
            raise DriverError(DriverErrorCode.CLEANUP_FAILED, message)
        with contextlib.suppress(BackendError):
            await self._backend.stop(observed, self._settings.stop_timeout)
        try:
            await self._backend.remove(observed)
        except BackendError as error:
            self._mark_failed("не удалось удалить контейнер")
            raise DriverError(DriverErrorCode.CLEANUP_FAILED, error.message) from error

    async def _cleanup_after(self, cause: str, instance_id: str) -> None:
        """Удаляет следы неудачного запуска; при отказе сохраняет исходную причину."""
        try:
            # После отказа run имя мог занять чужой контейнер или другой экземпляр
            # HoneyForge; очистка касается только UUID нашей попытки запуска.
            await self._clear_slot(DriverErrorCode.CLEANUP_FAILED, instance_id)
        except DriverError as error:
            raise DriverError(
                DriverErrorCode.CLEANUP_FAILED, f"{error.message}; исходная причина: {cause}"
            ) from error

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


@dataclass(frozen=True)
class _PreparedLaunch:
    """Новый экземпляр, проверенный до побочных эффектов."""

    blueprint: LowBlueprint
    instance_id: str
    spec: ContainerSpec
