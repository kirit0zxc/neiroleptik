"""Runtime-половина доверенной границы процесса Low: anchor и proof (§3, §8).

Producer выполняет согласованную внутреннюю операцию runtime: контролируемо удаляет
старый контейнер писателя и создаёт **другой** контейнер с прежними
agent/deployment/instance/revision и новым writer_id. Команда центра start или
recovery разрешением такого перезапуска не является и здесь не используется.

Порядок шагов §3 жёсткий: durable intent до запуска, подтверждённое ограждение,
снимки **до** выдачи нового RW, привязка фактического заголовка и только затем одна
фиксация и выдача неизменяемого документа. При сбое между шагами готового документа
нет: состояние остаётся в registry, и продолжение определяет reconciliation.

Этот модуль не интегрирован в исполнитель команд центра: операция вызывается отдельно
(см. `infra/runtime/boundary_stand.py` и спецификацию). Доверенная база — администратор
runtime-хоста и Docker Engine; ошибка, тайм-аут или другой daemon означают неизвестный
исход, а не отказ.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from agent.runtime.boundary.documents import (
    FirstJournal,
    ProcessBoundary,
    SnapshotEntry,
    WriterBinding,
    utc_timestamp,
)
from agent.runtime.boundary.errors import BoundaryError, BoundaryErrorCode
from agent.runtime.boundary.journals import JournalFile, journal_name
from agent.runtime.boundary.launcher import (
    BoundWriter,
    WriterAllocation,
    WriterBackend,
    WriterLauncher,
)
from agent.runtime.boundary.publication import (
    LinuxWriteOnceRename,
    ProtectedTree,
    PublicationRoots,
)
from agent.runtime.boundary.registry import (
    BindingState,
    BoundaryRegistry,
    IntentState,
    StoredDocument,
    StoredIntent,
    WriterChainHead,
)
from agent.runtime.boundary.snapshots import SnapshotLimits, SnapshotStore
from agent.runtime.contract import DeploymentSpec
from agent.runtime.drivers.low import LowSettings

CONTROL_PREFIX = "low-boundaries"
ANCHOR_NAME = "anchor.json"


def boundary_name(boundary_seq: int, boundary_id: str) -> str:
    """Имя опубликованного proof: номер без ведущих нулей и UUID перехода (§8.2)."""
    return f"{boundary_seq}-{boundary_id}.json"


@dataclass(frozen=True)
class ProducerResult:
    """Итог операции producer: достигнутое состояние и выданный документ, если он есть."""

    state: IntentState
    document: StoredDocument | None = None
    writer: BoundWriter | None = None


class BoundaryProducer:
    """Выполняет и восстанавливает переход писателя в прежнем instance.

    Экземпляр класса — один запуск exporter: его `publisher_epoch` попадает только
    в документы, зафиксированные после этого запуска. Повторная публикация уже
    зафиксированного документа epoch не меняет.
    """

    def __init__(
        self,
        registry: BoundaryRegistry,
        launcher: WriterLauncher,
        snapshots: SnapshotStore,
        control: ProtectedTree,
        *,
        publisher_epoch: str | None = None,
        new_uuid: Callable[[], str] = lambda: str(uuid4()),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._registry = registry
        self._launcher = launcher
        self._snapshots = snapshots
        self._control = control
        self._publisher_epoch = publisher_epoch if publisher_epoch is not None else new_uuid()
        self._new_uuid = new_uuid
        self._clock = clock

    @property
    def publisher_epoch(self) -> str:
        """UUID этого запуска exporter; смена epoch не доказывает смерть Low."""
        return self._publisher_epoch

    # --- операции ---------------------------------------------------------------------

    async def launch_first_writer(
        self, binding: WriterBinding, deployment: DeploymentSpec
    ) -> ProducerResult:
        """Первый запуск нового instance с выдачей anchor (§8.1).

        Bootstrap уже работающего или накопленного instance запрещён: anchor выдаётся
        только instance, запущенному после включения producer.
        """
        self._control.require_available()
        self._require_deployment(binding, deployment)
        await self.reconcile(binding, deployment)
        state = self._registry.binding_state(binding)
        if state is not None:
            if not state.supported:
                raise BoundaryError(
                    BoundaryErrorCode.BOUNDARY_SEQUENCE_CONFLICT, state.blocked_reason
                )
            if state.anchor_id is not None or state.head is not None:
                raise BoundaryError(
                    BoundaryErrorCode.BOUNDARY_SEQUENCE_CONFLICT,
                    "instance уже имеет начальную привязку писателя",
                )
        self._require_known_history(binding, state)
        if self._launcher.slot(binding).entries():
            raise BoundaryError(
                BoundaryErrorCode.PRODUCER_DISABLED,
                "в каталоге слота уже есть журналы этого instance",
            )
        allocation = WriterAllocation(self._new_uuid(), self._new_uuid(), 1)
        intent = self._registry.begin_intent(
            binding,
            kind="anchor",
            intent_id=self._new_uuid(),
            document_id=self._new_uuid(),
            writer_id=allocation.writer_id,
            first_journal_id=allocation.journal_id,
            first_journal_seq=allocation.journal_seq,
            journal_max_bytes=self._launcher.journal_max_bytes,
            journal_quota_bytes=self._launcher.journal_quota_bytes,
            engine_identity=await self._launcher.engine_identity(),
            now=self._clock(),
        )
        await self._launch(binding, deployment, intent)
        return await self.finish(binding)

    async def restart_writer(
        self,
        binding: WriterBinding,
        deployment: DeploymentSpec,
        *,
        hold_export: bool = False,
    ) -> ProducerResult:
        """Переход писателя: ограждение, снимки, запуск и привязка нового писателя.

        `hold_export` оставляет готовый переход незафиксированным: это управляемая
        точка совместного теста, а не разрешение выдавать RW до снимков. Документ
        выдаёт отдельный вызов `finish`.
        """
        self._control.require_available()
        self._require_deployment(binding, deployment)
        pending = await self.reconcile(binding, deployment)
        if pending is not None and pending.document is not None:
            # Незавершённый переход доведён до конца: новый писатель только что
            # привязан, и вторая ротация подряд уничтожила бы его без просьбы.
            return pending
        state = self._registry.binding_state(binding)
        self._require_known_history(binding, state)
        if state is None or state.head is None or state.anchor_id is None:
            raise BoundaryError(
                BoundaryErrorCode.BOUNDARY_SEQUENCE_CONFLICT,
                "у instance нет начальной привязки писателя",
            )
        if not state.supported:
            raise BoundaryError(BoundaryErrorCode.BOUNDARY_SEQUENCE_CONFLICT, state.blocked_reason)
        engine = await self._launcher.engine_identity()
        if state.engine_identity is not None and state.engine_identity != engine:
            # Отсутствие контейнера на другом daemon ограждения не доказывает (§2).
            raise BoundaryError(
                BoundaryErrorCode.RUNTIME_OBSERVATION_UNKNOWN,
                "текущая цепочка привязки создана другим Docker Engine",
            )
        # Место под копии проверяется до ограждения и до durable intent: нехватка
        # места не должна расходовать идентичности и оставлять незавершённый переход.
        self._check_capacity_before_fence(binding, state.head)
        intent = self._registry.begin_intent(
            binding,
            kind="boundary",
            intent_id=self._new_uuid(),
            document_id=self._new_uuid(),
            writer_id=self._new_uuid(),
            first_journal_id=self._new_uuid(),
            journal_max_bytes=self._launcher.journal_max_bytes,
            journal_quota_bytes=self._launcher.journal_quota_bytes,
            engine_identity=engine,
            previous_writer_id=state.head.writer_id,
            previous_container_id=state.head.container_id,
            now=self._clock(),
        )
        intent = await self._fence(binding, intent)
        intent = await self._stage(binding, intent)
        intent = await self._launch(binding, deployment, intent)
        if hold_export:
            return ProducerResult(intent.state, None, self._bound_writer(intent))
        return await self.finish(binding)

    async def finish(self, binding: WriterBinding) -> ProducerResult:
        """Выполняет только фиксацию и публикацию готового перехода (§3 шаг 5)."""
        intent = self._registry.outstanding_intent(binding)
        if intent is None:
            raise BoundaryError(
                BoundaryErrorCode.BOUNDARY_SEQUENCE_CONFLICT,
                "у привязки нет незавершённого перехода",
            )
        if intent.state == "next_bound":
            intent = self._commit(intent)
        if intent.state != "committed":
            raise BoundaryError(
                BoundaryErrorCode.NEXT_WRITER_UNBOUND,
                "переход не готов к фиксации",
            )
        document = self._registry.document(intent.document_id)
        assert document is not None
        self._publish(binding, document)
        published = self._registry.mark_published(intent.intent_id)
        return ProducerResult("exported", published, self._bound_writer(intent))

    async def reconcile(
        self, binding: WriterBinding, deployment: DeploymentSpec
    ) -> ProducerResult | None:
        """Продолжает или закрывает незавершённый intent привязки (§8.4).

        Выполняется под межпроцессным замком registry до любых новых операций слота.
        Неизвестный исход не продвигает состояние и не порождает нового перехода.
        """
        intent = self._registry.outstanding_intent(binding)
        if intent is None:
            # Незавершённого перехода нет: остаётся сверить опубликованные документы.
            return self.republish(binding)
        self._control.require_available()
        await self._require_same_engine(intent)
        if intent.state == "intent":
            intent = await self._resume_intent(binding, intent)
        if intent.state == "fenced":
            # Ограждение подтверждается заново: запись fenced сделана до сбоя, а
            # появившийся с тех пор писатель с RW доступом сделал бы снимок
            # доказательством чужих байт (§2, §3 шаг 2).
            await self._require_fenced(binding, intent)
            intent = await self._stage(binding, intent)
        if intent.state == "snapshots_published":
            intent = await self._resume_snapshots_published(binding, intent)
        if intent.state == "next_created":
            intent = await self._resume_next_created(binding, deployment, intent)
        elif intent.state == "next_started":
            intent = await self._resume_next_started(binding, intent)
        elif intent.state in ("intent", "snapshots_published"):
            # Контейнер ещё не создавался: запуск продолжается с шага 4 §3.
            intent = await self._launch(binding, deployment, intent)
        if intent.state in ("next_bound", "committed"):
            return await self.finish(binding)
        return ProducerResult(intent.state, None, None)

    # --- шаги перехода ----------------------------------------------------------------

    async def _fence(self, binding: WriterBinding, intent: StoredIntent) -> StoredIntent:
        """Подтверждает прекращение старого писателя и отсутствие другого RW доступа.

        Удаление идёт по полному сохранённому ID. stopped=false/true, код выхода или
        исчезновение имени этого барьера не заменяют: после удаления наблюдение по ID
        обязано вернуть отсутствие контейнера, а список контейнеров с записью в каталог
        слота обязан быть пустым.
        """
        await self._require_same_engine(intent)
        writer = intent.previous_writer_id
        container = intent.previous_container_id
        if writer is None or container is None:
            raise BoundaryError(
                BoundaryErrorCode.PREVIOUS_WRITER_NOT_FENCED,
                "в intent нет идентичности закрываемого писателя",
            )
        observed = await self._launcher.observe(container)
        if observed is not None:
            self._launcher.require_owned(binding, writer, observed)
            await self._launcher.discard(binding, writer, container)
        await self._require_fenced(binding, intent)
        return self._registry.advance(
            intent.intent_id, "fenced", fenced_at=utc_timestamp(self._clock())
        )

    async def _require_fenced(self, binding: WriterBinding, intent: StoredIntent) -> None:
        """Наблюдением подтверждает, что ни один писатель не может менять каталог слота.

        Проверка выполняется и сразу после удаления, и при каждом возобновлении из
        состояния `fenced`: durable запись об ограждении сделана в прошлом, а §2
        требует именно отсутствия других ресурсов с RW доступом на момент снимков.
        """
        container = intent.previous_container_id
        if container is None:
            raise BoundaryError(
                BoundaryErrorCode.PREVIOUS_WRITER_NOT_FENCED,
                "в intent нет идентичности закрываемого писателя",
            )
        if await self._launcher.observe(container) is not None:
            raise BoundaryError(
                BoundaryErrorCode.PREVIOUS_WRITER_NOT_FENCED,
                "контейнер закрываемого писателя всё ещё существует",
            )
        if await self._launcher.writable_mount_holders(binding):
            raise BoundaryError(
                BoundaryErrorCode.PREVIOUS_WRITER_NOT_FENCED,
                "к каталогу слота остаётся доступ на запись",
            )

    async def _stage(self, binding: WriterBinding, intent: StoredIntent) -> StoredIntent:
        """Закрепляет все журналы закрываемого писателя до выдачи нового RW (§3 шаг 3)."""
        state = self._registry.binding_state(binding)
        if state is None or state.head is None:
            raise BoundaryError(
                BoundaryErrorCode.BOUNDARY_SEQUENCE_CONFLICT,
                "у привязки нет текущей цепочки писателя",
            )
        if await self._launcher.containers_of_writer(intent.writer_id):
            # Контейнер нового писателя до снимков — противоречие порядка §3.
            raise BoundaryError(
                BoundaryErrorCode.RUNTIME_OBSERVATION_UNKNOWN,
                "контейнер нового писателя существует до закрепления снимков",
            )
        slot = self._launcher.slot(binding)
        try:
            chain = slot.collect_writer_chain(
                state.head.first_journal_id, state.head.first_journal_seq
            )
            entries = self._registry.snapshots(intent.intent_id)
            if entries:
                self._snapshots.verify_published(binding, intent.document_id, entries)
            else:
                entries = self._stage_copies(binding, intent, chain, state.head)
                self._registry.record_snapshots(intent.intent_id, entries)
        except BoundaryError as error:
            if error.code is BoundaryErrorCode.SOURCE_CHANGED:
                # Изменение исходника после ограждения означает, что ограждение
                # неполно (§8.3): переход неизвестен, а неизвестный исход запрещает
                # новые переходы instance до решения оператора (§2).
                self._registry.block_binding(binding, error.detail or error.code.value)
            raise
        next_seq = chain[-1].header.journal_seq + 1
        usage = slot.directory_usage()
        if usage + self._launcher.journal_max_bytes > self._launcher.journal_quota_bytes:
            raise BoundaryError(
                BoundaryErrorCode.LIMIT_EXCEEDED,
                "в квоте журнала Low нет места под новое поколение",
            )
        return self._registry.advance(
            intent.intent_id, "snapshots_published", first_journal_seq=next_seq
        )

    def _stage_copies(
        self,
        binding: WriterBinding,
        intent: StoredIntent,
        chain: Sequence[JournalFile],
        head: WriterChainHead,
    ) -> tuple[SnapshotEntry, ...]:
        """Создаёт копии либо перенимает уже опубликованный каталог перехода.

        Публикация каталога снимков и запись перечня в registry — две транзакции.
        Сбой между ними оставлял бы переход без выхода: копировать заново нельзя
        (имена write-once), а перечня нет. Поэтому опубликованный каталог
        перенимается, сверяясь с байтами цепочки; иначе переход останавливается.
        """
        limits = SnapshotLimits(head.journal_max_bytes, head.journal_quota_bytes)
        try:
            return self._snapshots.stage(binding, intent.document_id, chain, limits)
        except BoundaryError as error:
            if error.code is not BoundaryErrorCode.BOUNDARY_PUBLICATION_CONFLICT:
                raise
            return self._snapshots.adopt_published(binding, intent.document_id, chain)

    def _check_capacity_before_fence(self, binding: WriterBinding, head: WriterChainHead) -> None:
        """Предварительная проверка места в snapshot_root по текущим размерам (§8.3).

        Цепочка собирается до ограждения, пока писатель ещё жив, поэтому измерения
        приблизительны: если набор пока не образует целой цепочки, проверка
        пропускается, а решение принимает повторная проверка после ограждения.
        """
        try:
            chain = self._launcher.slot(binding).collect_writer_chain(
                head.first_journal_id, head.first_journal_seq
            )
        except BoundaryError as error:
            # Живой писатель может ротировать и дописывать журнал прямо сейчас:
            # неполный набор и изменившийся размер здесь нормальны.
            if error.code in (
                BoundaryErrorCode.BOUNDARY_SNAPSHOT_UNAVAILABLE,
                BoundaryErrorCode.SOURCE_CHANGED,
            ):
                return
            raise
        self._snapshots.check_capacity(binding, chain)

    async def _launch(
        self, binding: WriterBinding, deployment: DeploymentSpec, intent: StoredIntent
    ) -> StoredIntent:
        """Создаёт, запускает и привязывает нового писателя (§3 шаг 4)."""
        await self._require_same_engine(intent)
        allocation = self._allocation(intent)
        container_id = intent.container_id
        if container_id is None:
            container_id = await self._launcher.create(binding, deployment, allocation)
            intent = self._registry.advance(
                intent.intent_id, "next_created", container_id=container_id
            )
        if intent.state == "next_created":
            await self._launcher.start(container_id)
            intent = self._registry.advance(intent.intent_id, "next_started")
        return await self._bind(binding, intent, allocation)

    async def _bind(
        self, binding: WriterBinding, intent: StoredIntent, allocation: WriterAllocation
    ) -> StoredIntent:
        """Привязывает фактический заголовок первого поколения нового писателя."""
        assert intent.container_id is not None
        try:
            first = await self._launcher.bind_first_journal(
                binding, allocation, intent.container_id
            )
        except BoundaryError as error:
            if error.code is BoundaryErrorCode.RUNTIME_OBSERVATION_UNKNOWN:
                # Неизвестный исход не продвигает состояние: продолжит reconciliation.
                raise
            await self._abandon_after_launch(binding, intent, error)
            raise
        return self._registry.advance(
            intent.intent_id, "next_bound", header_sha256=first.header_sha256
        )

    async def _abandon_after_launch(
        self, binding: WriterBinding, intent: StoredIntent, error: BoundaryError
    ) -> None:
        """Снимает непривязанного писателя и закрывает поддержку границы для instance.

        Для anchor контейнер не удаляется: его судьбу решают обычные правила start,
        и producer не останавливает работающую ловушку ради выдачи anchor (§8.1).
        """
        if intent.kind == "boundary" and intent.container_id is not None:
            await self._launcher.discard(binding, intent.writer_id, intent.container_id)
        self._registry.fail_after_launch(intent.intent_id, error.detail or error.code.value)

    def _commit(self, intent: StoredIntent) -> StoredIntent:
        """Одна durable фиксация: счётчик, байты документа и новая привязка (§3 шаг 5)."""
        if intent.kind == "anchor":
            self._registry.commit_anchor(intent.intent_id, self._publisher_epoch, self._clock())
        else:
            self._registry.commit_boundary(intent.intent_id, self._publisher_epoch, self._clock())
        committed = self._registry.intent(intent.intent_id)
        assert committed is not None
        return committed

    def _publish(self, binding: WriterBinding, document: StoredDocument) -> None:
        """Публикует сохранённые байты документа write-once (§8.2).

        Имя выводится из самого документа, поэтому повторная публикация не зависит от
        незавершённого перехода. Повтор пишет те же байты: `publisher_epoch` и
        `committed_at` в них остаются от исходной фиксации.
        """
        name = self.document_name(document)
        directory = self._control.open_directory(self._control_parts(binding), create=True)
        try:
            self._control.write_once(directory, name, document.canonical)
        finally:
            os.close(directory)

    @staticmethod
    def document_name(document: StoredDocument) -> str:
        """Имя опубликованного файла документа по его каноническим байтам (§8.2)."""
        if document.kind == "anchor":
            return ANCHOR_NAME
        boundary = ProcessBoundary.parse(document.canonical)
        return boundary_name(boundary.boundary_seq, boundary.boundary_id)

    def republish(self, binding: WriterBinding) -> ProducerResult | None:
        """Сверяет опубликованные документы привязки с registry и восстанавливает их.

        Строка `exported` таблицы §8.4: пропавший файл публикуется теми же байтами,
        отличающийся — BOUNDARY_PUBLICATION_CONFLICT без перезаписи. Документы не
        собираются заново: берутся сохранённые при фиксации байты.
        """
        state = self._registry.binding_state(binding)
        if state is None or state.anchor_id is None:
            return None
        self._control.require_available()
        restored: StoredDocument | None = None
        identifiers = [state.anchor_id]
        if state.last_boundary_id is not None:
            identifiers.append(state.last_boundary_id)
        for identifier in identifiers:
            document = self._registry.document(identifier)
            if document is None:
                raise BoundaryError(
                    BoundaryErrorCode.REGISTRY_UNAVAILABLE,
                    "сохранённые байты выданного документа утрачены",
                )
            name = self.document_name(document)
            directory = self._control.open_directory(self._control_parts(binding), create=True)
            try:
                existing = self._control.read_published(directory, name)
                if existing is None:
                    self._control.write_once(directory, name, document.canonical)
                    restored = document
                elif existing != document.canonical:
                    # Опубликованный документ обязан побайтно совпадать с сохранённым
                    # при фиксации: иначе выданное доказательство подменено, и
                    # перезаписывать его нельзя (§8.2, §8.4).
                    raise BoundaryError(
                        BoundaryErrorCode.BOUNDARY_PUBLICATION_CONFLICT,
                        "опубликованный документ не совпадает с сохранёнными байтами",
                    )
            finally:
                os.close(directory)
        if restored is None:
            return None
        return ProducerResult("exported", restored, None)

    def published_path(self, binding: WriterBinding, name: str) -> Path:
        """Абсолютный путь опубликованного документа для диагностики и стенда."""
        return self._control.root.joinpath(*self._control_parts(binding)) / name

    def _require_deployment(self, binding: WriterBinding, deployment: DeploymentSpec) -> None:
        """Сверяет назначение центра с привязкой перехода до любых изменений (§5).

        Раньше несовпадение обнаруживалось только при создании контейнера, то есть
        после ограждения и снимков: ошибка вызывающей стороны уничтожала работающего
        писателя. Проверка вынесена в начало операции.
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

    def _require_known_history(self, binding: WriterBinding, state: BindingState | None) -> None:
        """Опубликованные документы при пустой registry означают утрату истории (§8.4).

        Историю по каталогу публикации восстанавливать запрещено, но её наличие обязано
        остановить выдачу новых документов: иначе после утраты registry instance получил
        бы второй anchor, объявляющий нового писателя первым писателем instance.
        """
        if state is not None and state.anchor_id is not None:
            return
        try:
            directory = self._control.open_directory(self._control_parts(binding), create=False)
        except BoundaryError as error:
            if error.code is BoundaryErrorCode.PRODUCER_DISABLED:
                # Каталога привязки нет: выданных документов тоже нет.
                return
            raise
        try:
            published = self._control.list_published(directory)
        finally:
            os.close(directory)
        if published:
            raise BoundaryError(
                BoundaryErrorCode.RUNTIME_OBSERVATION_UNKNOWN,
                "у привязки есть опубликованные документы, которых нет в registry",
            )

    @staticmethod
    def _control_parts(binding: WriterBinding) -> tuple[str, ...]:
        """Раскладка control-root из §8.2 без имени документа."""
        return (
            CONTROL_PREFIX,
            binding.agent_id,
            binding.deployment_id,
            binding.instance_id,
        )

    # --- восстановление ---------------------------------------------------------------

    async def _resume_intent(self, binding: WriterBinding, intent: StoredIntent) -> StoredIntent:
        """Состояние intent: для перехода продолжается ограждение, для anchor — поиск create."""
        if intent.kind == "boundary":
            return await self._fence(binding, intent)
        found = await self._launcher.containers_of_writer(intent.writer_id)
        if len(found) > 1:
            raise BoundaryError(
                BoundaryErrorCode.RUNTIME_OBSERVATION_UNKNOWN,
                "метке нового писателя соответствует несколько контейнеров",
            )
        if found:
            return self._registry.advance(intent.intent_id, "next_created", container_id=found[0])
        return intent

    async def _resume_snapshots_published(
        self, binding: WriterBinding, intent: StoredIntent
    ) -> StoredIntent:
        """Сверяет опубликованный набор снимков и привязывает потерянный create."""
        entries = self._registry.snapshots(intent.intent_id)
        if not entries:
            raise BoundaryError(
                BoundaryErrorCode.BOUNDARY_SNAPSHOT_UNAVAILABLE,
                "состояние снимков записано без перечня",
            )
        self._snapshots.verify_published(binding, intent.document_id, entries)
        if intent.container_id is not None:
            return intent
        found = await self._launcher.containers_of_writer(intent.writer_id)
        if len(found) > 1:
            raise BoundaryError(
                BoundaryErrorCode.RUNTIME_OBSERVATION_UNKNOWN,
                "метке нового писателя соответствует несколько контейнеров",
            )
        if found:
            return self._registry.advance(intent.intent_id, "next_created", container_id=found[0])
        return intent

    async def _resume_next_created(
        self, binding: WriterBinding, deployment: DeploymentSpec, intent: StoredIntent
    ) -> StoredIntent:
        """Контейнер создан: либо он ни разу не запускался, либо запуск продолжается.

        Если Engine сообщает, что контейнер не запускался, и файла первого поколения нет,
        попытка снимается целиком: контейнер удаляется по полному ID, а writer_id и
        journal_id остаются сожжёнными — launch повторяется с новыми (§8.4).
        """
        assert intent.container_id is not None
        observed = await self._launcher.observe(intent.container_id)
        allocation = self._allocation(intent)
        path = self._launcher.slot_directory(binding) / journal_name(
            binding.instance_id, allocation.journal_seq
        )
        # docker сообщает состояние created, пока контейнер ни разу не запускался.
        never_started = (
            observed is not None and not observed.running and observed.status == "created"
        )
        if (observed is None or never_started) and not path.exists():
            if observed is not None:
                self._launcher.require_owned(binding, intent.writer_id, observed)
                await self._launcher.discard(binding, intent.writer_id, intent.container_id)
            intent = self._registry.reallocate(intent.intent_id, self._new_uuid(), self._new_uuid())
            return await self._launch(binding, deployment, intent)
        if observed is not None and not observed.running and not never_started:
            # Контейнер уже работал, значит RW новому писателю выдавался: вторая жизнь
            # того же writer_id запрещена (§2), а цепочка осталась непривязанной (§8.4).
            self._launcher.require_owned(binding, intent.writer_id, observed)
            failure = BoundaryError(
                BoundaryErrorCode.NEXT_WRITER_UNBOUND,
                "писатель завершился до привязки первого поколения",
            )
            await self._abandon_after_launch(binding, intent, failure)
            raise failure
        return await self._launch(binding, deployment, intent)

    async def _resume_next_started(
        self, binding: WriterBinding, intent: StoredIntent
    ) -> StoredIntent:
        """Запуск состоялся: ожидается заголовок, иначе писатель снимается."""
        return await self._bind(binding, intent, self._allocation(intent))

    async def _require_same_engine(self, intent: StoredIntent) -> None:
        """Другой Engine или daemon делает исход операции неизвестным (§2)."""
        if await self._launcher.engine_identity() != intent.engine_identity:
            raise BoundaryError(
                BoundaryErrorCode.RUNTIME_OBSERVATION_UNKNOWN,
                "операция относится к другому Docker Engine",
            )

    @staticmethod
    def _allocation(intent: StoredIntent) -> WriterAllocation:
        """Выделение писателя из intent; без номера первого поколения запуск невозможен."""
        if intent.first_journal_seq is None:
            raise BoundaryError(
                BoundaryErrorCode.NEXT_WRITER_UNBOUND,
                "в intent нет выделенного номера первого поколения",
            )
        return WriterAllocation(
            writer_id=intent.writer_id,
            journal_id=intent.first_journal_id,
            journal_seq=intent.first_journal_seq,
        )

    def _bound_writer(self, intent: StoredIntent) -> BoundWriter | None:
        """Привязанный писатель для диагностики вызывающей стороны."""
        if (
            intent.container_id is None
            or intent.first_journal_seq is None
            or intent.header_sha256 is None
        ):
            return None
        return BoundWriter(
            writer_id=intent.writer_id,
            container_id=intent.container_id,
            first_journal=FirstJournal(
                journal_id=intent.first_journal_id,
                journal_seq=intent.first_journal_seq,
                header_sha256=intent.header_sha256,
            ),
        )


def build_producer(
    registry: BoundaryRegistry,
    backend: WriterBackend,
    roots: PublicationRoots,
    low: LowSettings,
    *,
    publisher_epoch: str | None = None,
) -> BoundaryProducer:
    """Собирает producer с защищёнными корнями публикации из настроек оператора.

    Корни не вложены в journal_root и не содержат его: иначе ловушка получила бы путь
    к собственным доказательствам.
    """
    if low.journal_root is None:
        raise BoundaryError(
            BoundaryErrorCode.PRODUCER_DISABLED,
            "граница процесса требует файлового журнала Low",
        )
    roots.check_outside(low.journal_root)
    rename = LinuxWriteOnceRename()
    control = ProtectedTree(roots.control_root, roots.group_gid, rename)
    snapshot_tree = ProtectedTree(roots.snapshot_root, roots.group_gid, rename)
    return BoundaryProducer(
        registry,
        WriterLauncher(low, backend, roots),
        SnapshotStore(snapshot_tree, roots.snapshot_quota_bytes),
        control,
        publisher_epoch=publisher_epoch,
    )
