"""Запуск одного писателя Low с привязкой первого заголовка (§3 шаг 4, §8.1).

Класс отделён от producer: он отвечает только за одну жизнь контейнера-писателя —
создание с сохранением полного Docker ID до start, сам запуск, чтение фактического
заголовка первого поколения и уборку неудачной попытки. Решения о переходе, снимках
и фиксации документов принимает producer.

Контейнер получает прежние agent/deployment/instance/revision и **новый** writer_id:
один writer_id относится к одной разрешённой жизни контейнера.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from agent.runtime.boundary.documents import FirstJournal, WriterBinding
from agent.runtime.boundary.errors import BoundaryError, BoundaryErrorCode
from agent.runtime.boundary.journals import (
    JournalHeader,
    JournalReader,
    JournalSlot,
    journal_name,
)
from agent.runtime.boundary.publication import PublicationRoots
from agent.runtime.contract import DeploymentSpec
from agent.runtime.drivers.containers import (
    LABEL_MANAGED,
    BackendError,
    ContainerObservation,
    ContainerSpec,
)
from agent.runtime.drivers.low import (
    DEFAULT_JOURNAL_FILE_BYTES,
    DEFAULT_JOURNAL_QUOTA_BYTES,
    LABEL_DEPLOYMENT,
    LABEL_INSTANCE,
    LABEL_LEVEL,
    LABEL_REVISION,
    LowProfileTranslator,
    LowSettings,
)

# Метка писателя: по ней reconciliation находит create, ID которого не успел сохраниться.
LABEL_WRITER = "honeyforge.writer_id"


class WriterBackend(Protocol):
    """Операции Engine, которые нужны доверенной границе процесса.

    Протокол реализует `DockerCli`; в тестах его заменяет подмена, записывающая вызовы.
    Все изменяющие операции адресуют контейнер только полным Docker ID.
    """

    async def create(self, spec: ContainerSpec) -> str:
        """Создаёт контейнер без запуска и возвращает полный Docker ID."""
        ...

    async def start_created(self, container_id: str) -> None:
        """Запускает уже созданный контейнер по полному ID."""
        ...

    async def inspect_by_id(self, container_id: str) -> ContainerObservation | None:
        """Наблюдает контейнер по полному ID; None — контейнера нет."""
        ...

    async def stop(self, name: str | ContainerObservation, timeout: int) -> None:
        """Мягко останавливает наблюдённый контейнер."""
        ...

    async def remove(self, name: str | ContainerObservation) -> None:
        """Удаляет наблюдённый контейнер."""
        ...

    async def engine_identity(self) -> str:
        """Идентичность Engine и демона."""
        ...

    async def writable_mount_holders(self, source: str) -> tuple[str, ...]:
        """Полные ID контейнеров с записью в каталог хоста."""
        ...

    async def containers_by_label(self, key: str, value: str) -> tuple[str, ...]:
        """Полные ID контейнеров с указанной меткой."""
        ...


@dataclass(frozen=True)
class WriterAllocation:
    """Выделенная до запуска идентичность писателя и его первого поколения."""

    writer_id: str
    journal_id: str
    journal_seq: int


@dataclass(frozen=True)
class BoundWriter:
    """Запущенный писатель с проверенным фактическим заголовком первого поколения."""

    writer_id: str
    container_id: str
    first_journal: FirstJournal


class WriterLauncher:
    """Создаёт, запускает и привязывает одного писателя слота Low."""

    def __init__(
        self,
        settings: LowSettings,
        backend: WriterBackend,
        roots: PublicationRoots,
        reader: JournalReader | None = None,
    ) -> None:
        self._settings = settings
        self._backend = backend
        self._roots = roots
        self._translator = LowProfileTranslator(settings)
        self._reader = reader if reader is not None else JournalReader()

    @property
    def journal_max_bytes(self) -> int:
        """Предел размера одного поколения, с которым запускается писатель."""
        value = self._settings.journal_max_bytes
        return DEFAULT_JOURNAL_FILE_BYTES if value is None else value

    @property
    def journal_quota_bytes(self) -> int:
        """Квота каталога слота, с которой запускается писатель."""
        value = self._settings.journal_quota_bytes
        return DEFAULT_JOURNAL_QUOTA_BYTES if value is None else value

    def slot_directory(self, binding: WriterBinding) -> Path:
        """Каталог слота на хосте; без файлового журнала граница не поддерживается."""
        directory = self._translator.journal_directory(binding.deployment_id)
        if directory is None:
            raise BoundaryError(
                BoundaryErrorCode.PRODUCER_DISABLED,
                "граница процесса требует файлового журнала Low",
            )
        return directory

    def slot(self, binding: WriterBinding) -> JournalSlot:
        """Наблюдатель каталога слота этого экземпляра."""
        return JournalSlot(self.slot_directory(binding), binding, self._reader)

    def prepare_slot_directory(self, binding: WriterBinding, spec: ContainerSpec) -> None:
        """Готовит каталог слота для пользователя контейнера до создания писателя.

        Подготовка идемпотентна и выполняется тем же переводчиком, что и при обычном
        запуске Low, поэтому владелец и права каталога не расходятся между путями.
        """
        try:
            self._translator.prepare_journal_directory(binding.deployment_id, spec.user)
        except OSError as error:
            raise BoundaryError(
                BoundaryErrorCode.PRODUCER_DISABLED,
                "каталог слота недоступен для подготовки",
            ) from error
        # Повторная сверка после подготовки: каталог слота мог оказаться ссылкой
        # внутрь корней публикации, и тогда ловушка получила бы доступ к своим же
        # доказательствам. Сверка идёт по фактическим путям, а не по строкам.
        if self._roots.overlaps_any(self.slot_directory(binding)):
            raise BoundaryError(
                BoundaryErrorCode.PRODUCER_DISABLED,
                "каталог слота пересекается с корнями публикации",
            )

    def container_spec(
        self,
        binding: WriterBinding,
        deployment: DeploymentSpec,
        allocation: WriterAllocation,
    ) -> ContainerSpec:
        """Описание запуска писателя: прежний instance, новый writer_id и выделение.

        Профиль переводится обычным переводчиком Low, поэтому меры ограничения и
        публикация портов совпадают с обычным запуском. Контейнер, источник mount
        которого пересекается с корнями публикации, не запускается (§8.2).
        """
        if deployment.deployment_id != binding.deployment_id:
            raise BoundaryError(
                BoundaryErrorCode.BOUNDARY_BINDING_MISMATCH,
                "назначение относится к другому слоту",
            )
        if deployment.revision != binding.profile_revision:
            raise BoundaryError(
                BoundaryErrorCode.BOUNDARY_BINDING_MISMATCH,
                "ревизия назначения не совпадает с привязкой перехода",
            )
        blueprint = self._translator.blueprint(deployment)
        spec = self._translator.container_spec(blueprint, binding.instance_id)
        if not spec.mounts:
            raise BoundaryError(
                BoundaryErrorCode.PRODUCER_DISABLED,
                "граница процесса требует файлового журнала Low",
            )
        for mount in spec.mounts:
            if self._roots.overlaps_any(Path(mount.source)):
                raise BoundaryError(
                    BoundaryErrorCode.PRODUCER_DISABLED,
                    "источник mount пересекается с корнями публикации",
                )
        return replace(
            spec,
            command=spec.command
            + (
                f"--journal-first-id={allocation.journal_id}",
                f"--journal-first-seq={allocation.journal_seq}",
            ),
            labels=spec.labels + ((LABEL_WRITER, allocation.writer_id),),
        )

    async def create(
        self,
        binding: WriterBinding,
        deployment: DeploymentSpec,
        allocation: WriterAllocation,
    ) -> str:
        """Создаёт контейнер писателя и возвращает полный ID до его первого запуска."""
        spec = self.container_spec(binding, deployment, allocation)
        self.prepare_slot_directory(binding, spec)
        try:
            return await self._backend.create(spec)
        except BackendError as error:
            raise self._unknown(error) from error

    async def start(self, container_id: str) -> None:
        """Запускает созданный контейнер; отказ Engine делает исход неизвестным."""
        try:
            await self._backend.start_created(container_id)
        except BackendError as error:
            raise self._unknown(error) from error

    async def bind_first_journal(
        self,
        binding: WriterBinding,
        allocation: WriterAllocation,
        container_id: str,
    ) -> FirstJournal:
        """Ждёт фактический заголовок первого поколения и сверяет его с выделением.

        Заголовок читается на хосте без перехода по ссылкам; хеш считается по строке
        вместе с LF. Тайм-аут, несовпадение или выход процесса — привязки нет (§8.1).
        """
        path = self.slot_directory(binding) / journal_name(
            binding.instance_id, allocation.journal_seq
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._settings.start_timeout
        last = "заголовок первого поколения не появился"
        while True:
            try:
                header = self._reader.read_header(path)
            except BoundaryError as error:
                last = error.detail
            else:
                self._require_allocated(binding, allocation, header)
                return FirstJournal(
                    journal_id=header.journal_id,
                    journal_seq=header.journal_seq,
                    header_sha256=header.header_sha256,
                    first_seq=header.first_seq,
                )
            observed = await self.observe(container_id)
            if observed is None or not observed.running:
                raise BoundaryError(
                    BoundaryErrorCode.NEXT_WRITER_UNBOUND,
                    "процесс писателя завершился до появления заголовка",
                )
            if loop.time() >= deadline:
                raise BoundaryError(BoundaryErrorCode.NEXT_WRITER_UNBOUND, last)
            await asyncio.sleep(min(self._settings.probe_interval, max(deadline - loop.time(), 0)))

    @staticmethod
    def _require_allocated(
        binding: WriterBinding,
        allocation: WriterAllocation,
        header: JournalHeader,
    ) -> None:
        """Все поля заголовка совпадают с выделением и привязкой перехода (§8.1)."""
        if (
            header.journal_id != allocation.journal_id
            or header.journal_seq != allocation.journal_seq
            or header.previous_journal_id is not None
            or header.first_seq != 1
        ):
            raise BoundaryError(
                BoundaryErrorCode.NEXT_WRITER_UNBOUND,
                "заголовок первого поколения не совпадает с выделением",
            )
        if (
            header.instance_id != binding.instance_id
            or header.deployment_id != binding.deployment_id
            or header.profile_revision != binding.profile_revision
        ):
            raise BoundaryError(
                BoundaryErrorCode.BOUNDARY_BINDING_MISMATCH,
                "заголовок первого поколения относится к другой привязке",
            )

    async def observe(self, container_id: str) -> ContainerObservation | None:
        """Наблюдение по полному ID; недоступный Engine делает исход неизвестным."""
        try:
            return await self._backend.inspect_by_id(container_id)
        except BackendError as error:
            raise self._unknown(error) from error

    def require_owned(
        self,
        binding: WriterBinding,
        writer_id: str,
        observed: ContainerObservation,
    ) -> None:
        """Метки контейнера обязаны совпадать с привязкой и конкретным writer_id."""
        labels = dict(observed.labels)
        if (
            labels.get(LABEL_MANAGED) != "true"
            or labels.get(LABEL_LEVEL) != "low"
            or labels.get(LABEL_DEPLOYMENT) != binding.deployment_id
            or labels.get(LABEL_INSTANCE) != binding.instance_id
            or labels.get(LABEL_REVISION) != str(binding.profile_revision)
            or labels.get(LABEL_WRITER) != writer_id
        ):
            raise BoundaryError(
                BoundaryErrorCode.BOUNDARY_BINDING_MISMATCH,
                "метки контейнера не совпадают с писателем этой привязки",
            )

    async def discard(self, binding: WriterBinding, writer_id: str, container_id: str) -> None:
        """Убирает контейнер неудачной попытки по полному ID; отсутствие — не ошибка."""
        observed = await self.observe(container_id)
        if observed is None:
            return
        self.require_owned(binding, writer_id, observed)
        try:
            await self._backend.stop(observed, self._settings.stop_timeout)
        except BackendError:
            # Мягкая остановка не обязательна: принудительное удаление завершит процесс.
            pass
        try:
            await self._backend.remove(observed)
        except BackendError as error:
            raise self._unknown(error) from error

    async def containers_of_writer(self, writer_id: str) -> tuple[str, ...]:
        """Полные ID контейнеров с меткой этого writer_id (поиск потерянного create)."""
        try:
            return await self._backend.containers_by_label(LABEL_WRITER, writer_id)
        except BackendError as error:
            raise self._unknown(error) from error

    async def engine_identity(self) -> str:
        """Идентичность Engine; сравнивается с сохранённой в intent."""
        try:
            return await self._backend.engine_identity()
        except BackendError as error:
            raise self._unknown(error) from error

    async def writable_mount_holders(self, binding: WriterBinding) -> tuple[str, ...]:
        """Контейнеры с записью в каталог слота: пустой список нужен для ограждения."""
        try:
            return await self._backend.writable_mount_holders(str(self.slot_directory(binding)))
        except BackendError as error:
            raise self._unknown(error) from error

    @staticmethod
    def _unknown(error: BackendError) -> BoundaryError:
        """Ошибка, тайм-аут или недоступность Engine означают неизвестный исход (§2)."""
        return BoundaryError(BoundaryErrorCode.RUNTIME_OBSERVATION_UNKNOWN, error.message)
