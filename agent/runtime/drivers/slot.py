"""Общая машинерия слота: один контейнер на назначение центра, независимо от уровня.

Слот — это одно назначение (`deployment_id`) и ровно один контейнер с меткой уровня.
Жизненный цикл, защита чужих контейнеров, адресация мутаций по неизменяемому Docker ID,
липкий `FAILED`, подхват работающего экземпляра и операции для исполнителя команд
(`observe`, `recover`, `wait_until_released`) одинаковы для Low и Medium. Уровень задаёт
только перевод профиля, описание контейнера, подготовку хоста и протокольные пробы.

Живой прогон с настоящим Docker Engine не выполнялся: поведение проверено юнит- и
loopback-тестами с подменой контейнерного backend (см. specs/runtime-drivers.md).
"""

from __future__ import annotations

import contextlib
import re
from abc import abstractmethod
from dataclasses import dataclass
from typing import ClassVar, Protocol
from uuid import UUID

from agent.runtime.contract import DeploymentSpec, Level
from agent.runtime.drivers.base import (
    Driver,
    DriverError,
    DriverErrorCode,
    DriverState,
    DriverStatus,
)
from agent.runtime.drivers.containers import (
    LABEL_DEPLOYMENT,
    LABEL_INSTANCE,
    LABEL_LEVEL,
    LABEL_MANAGED,
    LABEL_REVISION,
    BackendError,
    ContainerBackend,
    ContainerObservation,
    ContainerSpec,
)
from agent.runtime.drivers.health import HealthCheckFailed

_FOREIGN = "имя слота занято контейнером, не принадлежащим HoneyForge"
_OTHER_INSTANCE = "имя слота занято другим экземпляром HoneyForge"
_OTHER_SLOT = "имя слота занято контейнером другого назначения или уровня"
_CONTAINER_ID = re.compile(r"[0-9a-f]{64}")
_REVISION = re.compile(r"[1-9][0-9]{0,18}")


class SlotBlueprint(Protocol):
    """Производное от профиля описание ловушки: идентичность назначения и ревизия."""

    @property
    def deployment_id(self) -> str: ...

    @property
    def revision(self) -> int: ...


@dataclass(frozen=True)
class PreparedLaunch[BlueprintT: SlotBlueprint]:
    """Новый экземпляр, проверенный до побочных эффектов над работающим слотом."""

    blueprint: BlueprintT
    instance_id: str
    spec: ContainerSpec


class ContainerSlotDriver[BlueprintT: SlotBlueprint](Driver):
    """Один слот: одно назначение, один контейнер, новый instance_id на каждый запуск."""

    level: ClassVar[Level]

    def __init__(
        self, deployment_id: str, backend: ContainerBackend, stop_timeout: int = 10
    ) -> None:
        super().__init__()
        self.deployment_id = str(UUID(deployment_id))
        self._backend = backend
        self._stop_timeout = stop_timeout
        self._name = self._container_name()
        self._state = DriverState.STOPPED
        self._healthy = False
        self._instance_id: str | None = None
        self._revision: int | None = None
        self._blueprint: BlueprintT | None = None
        self._detail = ""

    # --- Обязанности уровня -------------------------------------------------

    @abstractmethod
    def _container_name(self) -> str:
        """Имя контейнера слота; префикс honeyforge- защищает чужие контейнеры."""

    @abstractmethod
    def _translate_profile(self, deployment: DeploymentSpec) -> BlueprintT:
        """Проверяет профиль на реализуемость; нарушение — UNSUPPORTED_PROFILE."""

    @abstractmethod
    def _container_spec(self, blueprint: BlueprintT, instance_id: str) -> ContainerSpec:
        """Описание запуска нового экземпляра: порты, метки, тома и меры ограничения."""

    @abstractmethod
    def _prepare_host(self, blueprint: BlueprintT, instance_id: str, spec: ContainerSpec) -> None:
        """Готовит состояние на хосте до удаления прежнего экземпляра; отказ — START_FAILED."""

    @abstractmethod
    async def _check_healthy(self, blueprint: BlueprintT) -> None:
        """Протокольная проба опубликованных портов; отказ — HealthCheckFailed."""

    @abstractmethod
    async def _check_released(self, deployment: DeploymentSpec) -> bool:
        """True только при явном отказе TCP-подключения ко всем портам профиля."""

    # --- Наблюдение и восстановление для исполнителя ------------------------

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
            return await self._check_released(deployment)

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
            await self._check_healthy(blueprint)
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

    # --- Общий контракт Driver ---------------------------------------------

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

    # --- Шаги изменяющих операций ------------------------------------------

    def _translate(self, deployment: DeploymentSpec) -> BlueprintT:
        if deployment.deployment_id != self.deployment_id:
            raise DriverError(DriverErrorCode.INVALID_STATE, "назначение относится к другому слоту")
        return self._translate_profile(deployment)

    def _prepare(self, blueprint: BlueprintT) -> PreparedLaunch[BlueprintT]:
        """Готовит новый экземпляр до удаления прежнего: ошибки параметров — до удаления.

        Побочные эффекты ограничены подготовкой состояния этого экземпляра на хосте:
        недоступный каталог обнаруживается, пока исправный экземпляр ещё работает.
        Любой отказ подготовки становится `START_FAILED`, в том числе исключение, которое
        уровень не предусмотрел: иначе оно вышло бы из драйвера и уронило цикл
        исполнителя без ACK центру (правило 3 в specs/runtime-drivers.md, раздел 3).
        """
        instance_id = self._new_instance_id()
        try:
            spec = self._container_spec(blueprint, instance_id)
        except ValueError as error:
            raise DriverError(
                DriverErrorCode.START_FAILED, f"недопустимые параметры запуска: {error}"
            ) from error
        try:
            self._prepare_host(blueprint, instance_id, spec)
        except (OSError, ValueError, OverflowError) as error:
            # Сообщение не содержит пути и ввода: его формирует уровень, а здесь
            # остаётся только безопасная причина отказа.
            raise DriverError(
                DriverErrorCode.START_FAILED, "состояние экземпляра на хосте не подготовлено"
            ) from error
        return PreparedLaunch(blueprint, instance_id, spec)

    @abstractmethod
    def _new_instance_id(self) -> str:
        """Новый UUID экземпляра; исполнитель подставляет сохранённый UUID попытки."""

    async def _launch(self, prepared: PreparedLaunch[BlueprintT]) -> DriverStatus:
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
                await self._check_healthy(prepared.blueprint)
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
            await self._backend.stop(observed, self._stop_timeout)
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
