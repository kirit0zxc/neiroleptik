"""Порядок шагов перехода писателя Low и его восстановление (§3, §8.4). Только Linux.

Подмена Engine создаёт и запускает «контейнеры» в том же процессе, но журнал пишет
**настоящий** писатель ловушки: аргументы, которые producer передаёт Low, разбираются
его же CLI, поэтому startup-выделение проверяется по фактическому заголовку. Живого
Docker Engine здесь нет: его отдельная проверка — `test_boundary_docker.py`.
"""

import asyncio
import dataclasses
import hashlib
import json
import os
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from boundary_support import collector_gid, publication_base, require_linux

from agent.runtime.boundary.documents import ProcessBoundary, WriterAnchor, WriterBinding
from agent.runtime.boundary.errors import BoundaryError, BoundaryErrorCode
from agent.runtime.boundary.journals import JournalFile, JournalSlot, journal_name
from agent.runtime.boundary.launcher import LABEL_WRITER, WriterLauncher
from agent.runtime.boundary.producer import (
    ANCHOR_NAME,
    CONTROL_PREFIX,
    BoundaryProducer,
    boundary_name,
    build_producer,
)
from agent.runtime.boundary.publication import (
    LinuxWriteOnceRename,
    ProtectedTree,
    PublicationRoots,
)
from agent.runtime.boundary.registry import BoundaryRegistry
from agent.runtime.boundary.snapshots import SnapshotStore
from agent.runtime.contract import (
    ChannelSpec,
    DeploymentSpec,
    LoggingSpec,
    ProfileSpec,
    ServiceSpec,
)
from agent.runtime.drivers.containers import (
    LABEL_MANAGED,
    BackendError,
    ContainerObservation,
    ContainerSpec,
)
from agent.runtime.drivers.low import JOURNAL_CONTAINER_DIR, LowSettings
from honeypots.low.__main__ import build_parser, journal_from_args
from honeypots.low.journal import JournalAllocationError, RotatingJournalSink

AGENT = "00000000-0000-4000-8000-000000000001"
DEPLOYMENT = "e0d692f0-fb4c-4a37-920e-0c8683471639"
PROFILE = "7c3f6fb6-2b74-4a1f-9a2a-d51d3f3a0f11"
INSTANCE = "a2df557e-785f-48f3-ba6e-348b76730f41"
ENGINE = "daemon-2b7a/29.5.2"
COLLECTOR_GID = collector_gid()

require_linux("producer")

pytestmark = pytest.mark.skipif(
    sys.platform != "linux",
    reason="публикация границы процесса поддерживается только на Linux",
)


@pytest.fixture(autouse=True)
def container_chown(monkeypatch: pytest.MonkeyPatch) -> None:
    """Смена владельца каталога слота требует прав root и подменяется, как в тестах Low."""
    monkeypatch.setattr(os, "chown", lambda *_a, **_k: None)


@pytest.fixture
def base() -> Iterator[Path]:
    """Безопасная база корней публикации и каталога журналов (§8.2)."""
    yield from publication_base("producer")


def binding(revision: int = 1) -> WriterBinding:
    return WriterBinding(AGENT, DEPLOYMENT, INSTANCE, revision)


def deployment(revision: int = 1) -> DeploymentSpec:
    """Назначение центра уровня low с одним tcp-сервисом и баннером."""
    return DeploymentSpec(
        DEPLOYMENT,
        PROFILE,
        revision,
        "running",
        ProfileSpec(
            "low",
            "service-node",
            (ServiceSpec("tcp", 2222, "SSH-2.0-HoneyForge"),),
            LoggingSpec(),
            ChannelSpec(),
            None,
            (),
        ),
        generation=1,
        applied_revision=revision,
        instance_id=INSTANCE,
    )


class FakeEngine:
    """Подмена Docker Engine: контейнеры живут в процессе, журнал пишет сам Low.

    Подмена сохраняет те свойства, на которые опирается ограждение: полный 64-символьный
    ID, различение «создан» и «запущен», метки, список источников записываемых mounts
    и возможность отказать на любой операции.
    """

    def __init__(self, engine: str = ENGINE) -> None:
        self.engine = engine
        self.specs: dict[str, ContainerSpec] = {}
        self.states: dict[str, str] = {}
        self.sinks: dict[str, RotatingJournalSink] = {}
        self.removed: set[str] = set()
        self.extra_holders: tuple[str, ...] = ()
        self.fail_create = False
        self.fail_remove = False
        self.ignore_allocation = False
        self.fail_start = False
        self.fail_writer_lookup = False
        self.survive_remove = False
        self.created: list[str] = []
        self._counter = 0

    # --- протокол WriterBackend -------------------------------------------------------

    async def create(self, spec: ContainerSpec) -> str:
        if self.fail_create:
            raise BackendError("docker create: отказ подмены")
        self._counter += 1
        identifier = f"{self._counter:064x}"
        self.specs[identifier] = spec
        self.states[identifier] = "created"
        self.created.append(identifier)
        return identifier

    async def start_created(self, container_id: str) -> None:
        if self.fail_start:
            raise BackendError("docker start: отказ подмены")
        spec = self.specs[container_id]
        self.states[container_id] = "running"
        try:
            self.sinks[container_id] = self._open_journal(spec)
        except JournalAllocationError:
            # Настоящая ловушка завершается кодом 2 до открытия слушателей.
            self.states[container_id] = "exited"

    async def inspect_by_id(self, container_id: str) -> ContainerObservation | None:
        if container_id in self.removed or container_id not in self.states:
            return None
        spec = self.specs[container_id]
        status = self.states[container_id]
        return ContainerObservation(
            name=spec.name,
            status=status,
            running=status == "running",
            exit_code=None if status == "running" else 137,
            labels=dict(spec.labels),
            container_id=container_id,
        )

    async def stop(self, name: str | ContainerObservation, timeout: int) -> None:
        assert isinstance(name, ContainerObservation) and name.container_id is not None
        self.states[name.container_id] = "exited"
        self._close(name.container_id, footer=False)

    async def remove(self, name: str | ContainerObservation) -> None:
        assert isinstance(name, ContainerObservation) and name.container_id is not None
        if self.fail_remove:
            raise BackendError("docker rm: отказ подмены")
        self._close(name.container_id, footer=False)
        if not self.survive_remove:
            self.removed.add(name.container_id)

    async def engine_identity(self) -> str:
        return self.engine

    async def writable_mount_holders(self, source: str) -> tuple[str, ...]:
        holders = [
            identifier
            for identifier, spec in self.specs.items()
            if identifier not in self.removed
            and any(mount.source == source and not mount.read_only for mount in spec.mounts)
        ]
        return (*holders, *self.extra_holders)

    async def containers_by_label(self, key: str, value: str) -> tuple[str, ...]:
        if self.fail_writer_lookup:
            # Отказ сразу после durable записи ограждения: снимки ещё не делались.
            raise BackendError("docker ps: отказ подмены на поиске по метке")
        return tuple(
            identifier
            for identifier, spec in self.specs.items()
            if identifier not in self.removed and dict(spec.labels).get(key) == value
        )

    # --- вспомогательное поведение ловушки --------------------------------------------

    def _open_journal(self, spec: ContainerSpec) -> RotatingJournalSink:
        """Разбирает аргументы producer настоящим CLI Low и открывает его журнал."""
        arguments = build_parser().parse_args(list(spec.command))
        settings = journal_from_args(arguments)
        assert settings is not None
        source = next(
            mount.source for mount in spec.mounts if mount.target == JOURNAL_CONTAINER_DIR
        )
        changes: dict[str, object] = {"directory": Path(source)}
        if self.ignore_allocation:
            # Писатель выбирает journal_id сам: привязка заголовка должна это заметить.
            changes.update(first_journal_id=None, first_journal_seq=None)
        sink = RotatingJournalSink(dataclasses.replace(settings, **changes))
        sink.open()
        return sink

    def _close(self, container_id: str, *, footer: bool) -> None:
        sink = self.sinks.pop(container_id, None)
        if sink is not None and footer:
            sink.close()

    def emit(self, container_id: str, seq: int) -> None:
        """Записывает событие в журнал работающего писателя."""
        self.sinks[container_id].emit({"seq": seq, "kind": "probe"})

    def crash(self, container_id: str) -> None:
        """Аварийное завершение: концовки нет, контейнер остаётся в состоянии exited."""
        self.sinks.pop(container_id, None)
        self.states[container_id] = "exited"

    def writer_of(self, container_id: str) -> str:
        return dict(self.specs[container_id].labels)[LABEL_WRITER]


def roots(base: Path) -> PublicationRoots:
    return PublicationRoots(base / "control", base / "snapshots", COLLECTOR_GID)


def low_settings(base: Path, **changes: object) -> LowSettings:
    defaults: dict[str, object] = {
        "journal_root": base / "journals",
        "journal_max_bytes": 256 * 1024,
        "journal_quota_bytes": 4 * 256 * 1024,
        "start_timeout": 2.0,
        "probe_interval": 0.01,
        "stop_timeout": 1,
    }
    defaults.update(changes)
    return LowSettings(**defaults)  # type: ignore[arg-type]


def producer(
    base: Path, registry: BoundaryRegistry, engine: FakeEngine, **changes: object
) -> BoundaryProducer:
    return build_producer(registry, engine, roots(base), low_settings(base, **changes))


def registry_at(base: Path) -> BoundaryRegistry:
    return BoundaryRegistry(base / "state" / "boundary.db")


def control_file(base: Path, name: str) -> Path:
    return base / "control" / CONTROL_PREFIX / AGENT / DEPLOYMENT / INSTANCE / name


def snapshot_file(base: Path, boundary_id: str, name: str) -> Path:
    return base / "snapshots" / "low-snapshots" / AGENT / DEPLOYMENT / INSTANCE / boundary_id / name


def slot_directory(base: Path) -> Path:
    return base / "journals" / DEPLOYMENT


async def anchored(
    base: Path, registry: BoundaryRegistry, engine: FakeEngine, **changes: object
) -> tuple[BoundaryProducer, WriterAnchor, str]:
    """Первый запуск instance: выдан anchor, писатель A работает и пишет события."""
    runtime = producer(base, registry, engine, **changes)
    result = await runtime.launch_first_writer(binding(), deployment())
    assert result.state == "exported" and result.document is not None
    anchor = WriterAnchor.parse(result.document.canonical)
    container = anchor.container_id
    engine.emit(container, 1)
    engine.emit(container, 2)
    return runtime, anchor, container


def test_anchor_binds_the_first_writer_of_a_new_instance(base: Path) -> None:
    """Anchor выдаётся при первом запуске instance и совпадает с фактическим заголовком."""

    async def scenario() -> None:
        engine = FakeEngine()
        with registry_at(base) as registry:
            _, anchor, container = await anchored(base, registry, engine)
            assert anchor.binding == binding()
            assert anchor.writer_id == engine.writer_of(container)
            assert anchor.first_journal.journal_seq == 1
            published = control_file(base, ANCHOR_NAME)
            assert published.read_bytes() == anchor.canonical()
            # Хеш заголовка посчитан по настоящему файлу первого поколения.
            first = slot_directory(base) / journal_name(INSTANCE, 1)
            header = first.read_bytes().split(b"\n", 1)[0] + b"\n"
            import hashlib

            assert anchor.first_journal.header_sha256 == hashlib.sha256(header).hexdigest()
            assert json.loads(header)["journal_id"] == anchor.first_journal.journal_id

    asyncio.run(scenario())


def test_crash_restart_produces_a_trusted_boundary(base: Path) -> None:
    """Основной сценарий: аварийный писатель закрыт, его байты закреплены, цепочка привязана."""

    async def scenario() -> None:
        engine = FakeEngine()
        with registry_at(base) as registry:
            runtime, anchor, container = await anchored(base, registry, engine)
            first = slot_directory(base) / journal_name(INSTANCE, 1)
            original = first.read_bytes()
            engine.crash(container)
            assert not original.endswith(b'"reason":"shutdown"}\n')

            result = await runtime.restart_writer(binding(), deployment())
            assert result.state == "exported" and result.document is not None
            proof = ProcessBoundary.parse(result.document.canonical)
            assert (proof.boundary_seq, proof.previous_boundary_id) == (1, None)
            assert proof.binding == binding()
            assert proof.previous_writer.writer_id == anchor.writer_id
            assert proof.previous_writer.container_id == container
            assert proof.previous_writer.fence == "container_removed"
            assert len(proof.previous_journals) == 1
            closed = proof.previous_journals[0]
            assert closed.journal_id == anchor.first_journal.journal_id
            assert closed.footer_present is False
            assert closed.size_bytes == len(original)
            # Новая цепочка продолжает нумерацию, но начинает process seq заново.
            assert proof.next_writer.first_journal.journal_seq == 2
            assert proof.next_writer.first_journal.first_seq == 1
            assert proof.next_writer.writer_id != anchor.writer_id
            assert proof.next_writer.container_id != container

            name = boundary_name(proof.boundary_seq, proof.boundary_id)
            assert control_file(base, name).read_bytes() == result.document.canonical
            copy = snapshot_file(base, proof.boundary_id, closed.snapshot_ref)
            assert copy.read_bytes() == original
            # Исходники остаются на месте и не переименовываются.
            assert first.read_bytes() == original
            assert sorted(path.name for path in slot_directory(base).iterdir()) == [
                journal_name(INSTANCE, 1),
                journal_name(INSTANCE, 2),
            ]

    asyncio.run(scenario())


def test_held_export_leaves_the_collector_waiting_until_commit(base: Path) -> None:
    """Шаги 1–4 без фиксации: нового документа нет, хотя писатель B уже пишет (§6)."""

    async def scenario() -> None:
        engine = FakeEngine()
        with registry_at(base) as registry:
            runtime, anchor, container = await anchored(base, registry, engine)
            engine.crash(container)
            held = await runtime.restart_writer(binding(), deployment(), hold_export=True)
            assert held.state == "next_bound" and held.document is None
            assert held.writer is not None and held.writer.first_journal.journal_seq == 2
            # Новая цепочка уже существует, но доказательства перехода ещё нет.
            assert (slot_directory(base) / journal_name(INSTANCE, 2)).exists()
            directory = control_file(base, ANCHOR_NAME).parent
            assert sorted(path.name for path in directory.iterdir()) == [ANCHOR_NAME]

            finished = await runtime.finish(binding())
            assert finished.state == "exported" and finished.document is not None
            proof = ProcessBoundary.parse(finished.document.canonical)
            assert control_file(base, boundary_name(1, proof.boundary_id)).exists()

    asyncio.run(scenario())


def test_second_writable_mount_blocks_the_transition(base: Path) -> None:
    """Другой контейнер с записью в каталог слота означает неполное ограждение (§2)."""

    async def scenario() -> None:
        engine = FakeEngine()
        with registry_at(base) as registry:
            runtime, _, container = await anchored(base, registry, engine)
            engine.crash(container)
            engine.extra_holders = ("f" * 64,)
            with pytest.raises(BoundaryError) as error:
                await runtime.restart_writer(binding(), deployment())
            assert error.value.code is BoundaryErrorCode.PREVIOUS_WRITER_NOT_FENCED
            # Новый писатель не запускался, документов не появилось.
            assert engine.created == [container]
            assert not (slot_directory(base) / journal_name(INSTANCE, 2)).exists()
            directory = control_file(base, ANCHOR_NAME).parent
            assert sorted(path.name for path in directory.iterdir()) == [ANCHOR_NAME]

    asyncio.run(scenario())


def test_surviving_container_is_not_accepted_as_fenced(base: Path) -> None:
    """Исчезновение имени или код выхода ограждения не доказывают: нужен факт удаления."""

    async def scenario() -> None:
        engine = FakeEngine()
        with registry_at(base) as registry:
            runtime, _, container = await anchored(base, registry, engine)
            engine.crash(container)
            engine.survive_remove = True
            with pytest.raises(BoundaryError) as error:
                await runtime.restart_writer(binding(), deployment())
            assert error.value.code is BoundaryErrorCode.PREVIOUS_WRITER_NOT_FENCED

    asyncio.run(scenario())


def test_other_daemon_makes_the_outcome_unknown(base: Path) -> None:
    """Другой Engine или daemon — RUNTIME_OBSERVATION_UNKNOWN без продвижения (§2)."""

    async def scenario() -> None:
        engine = FakeEngine()
        with registry_at(base) as registry:
            runtime, _, container = await anchored(base, registry, engine)
            engine.crash(container)
            engine.engine = "daemon-other/29.5.2"
            with pytest.raises(BoundaryError) as error:
                await runtime.restart_writer(binding(), deployment())
            assert error.value.code is BoundaryErrorCode.RUNTIME_OBSERVATION_UNKNOWN

    asyncio.run(scenario())


def test_other_revision_is_refused_before_any_mutation(base: Path) -> None:
    """Ревизия назначения обязана совпадать с привязкой перехода (§5)."""

    async def scenario() -> None:
        engine = FakeEngine()
        with registry_at(base) as registry:
            runtime, _, container = await anchored(base, registry, engine)
            engine.crash(container)
            with pytest.raises(BoundaryError) as error:
                await runtime.restart_writer(binding(), deployment(revision=2))
            assert error.value.code is BoundaryErrorCode.BOUNDARY_BINDING_MISMATCH

    asyncio.run(scenario())


def test_restart_without_an_anchor_is_refused(base: Path) -> None:
    """Первый переход проверяется против начальной привязки writer: без неё отказ (§8.1)."""

    async def scenario() -> None:
        engine = FakeEngine()
        with registry_at(base) as registry:
            runtime = producer(base, registry, engine)
            with pytest.raises(BoundaryError) as error:
                await runtime.restart_writer(binding(), deployment())
            assert error.value.code is BoundaryErrorCode.BOUNDARY_SEQUENCE_CONFLICT

    asyncio.run(scenario())


def test_bootstrap_of_an_accumulated_instance_is_refused(base: Path) -> None:
    """Anchor выдаётся только instance, запущенному после включения producer (§8.1)."""

    async def scenario() -> None:
        engine = FakeEngine()
        directory = slot_directory(base)
        directory.mkdir(mode=0o750, parents=True)
        (directory / journal_name(INSTANCE, 1)).write_bytes(b'{"record":"header"}\n')
        with registry_at(base) as registry:
            runtime = producer(base, registry, engine)
            with pytest.raises(BoundaryError) as error:
                await runtime.launch_first_writer(binding(), deployment())
            assert error.value.code is BoundaryErrorCode.PRODUCER_DISABLED
            assert engine.created == []

    asyncio.run(scenario())


def test_unbound_header_discards_the_writer_and_stops_the_instance(base: Path) -> None:
    """Несовпадение выделения снимает писателя и закрывает поддержку границы (§8.4)."""

    async def scenario() -> None:
        engine = FakeEngine()
        with registry_at(base) as registry:
            runtime, _, container = await anchored(base, registry, engine)
            engine.crash(container)
            # Писатель открыл журнал с собственным journal_id вместо выделенного.
            engine.ignore_allocation = True
            with pytest.raises(BoundaryError) as error:
                await runtime.restart_writer(binding(), deployment())
            assert error.value.code is BoundaryErrorCode.NEXT_WRITER_UNBOUND
            state = registry.binding_state(binding())
            assert state is not None and not state.supported
            # Контейнер непривязанного писателя удалён, новый переход не начинается.
            assert len(engine.created) == 2
            assert engine.created[1] in engine.removed
            with pytest.raises(BoundaryError) as repeated:
                await runtime.restart_writer(binding(), deployment())
            assert repeated.value.code is BoundaryErrorCode.BOUNDARY_SEQUENCE_CONFLICT

    asyncio.run(scenario())


def test_crash_before_create_is_resumed_by_reconciliation(base: Path) -> None:
    """Сбой до create продолжается после рестарта exporter без повторных снимков (§8.4)."""

    async def scenario() -> None:
        engine = FakeEngine()
        with registry_at(base) as registry:
            runtime, anchor, container = await anchored(base, registry, engine)
            engine.crash(container)
            engine.fail_create = True
            with pytest.raises(BoundaryError):
                await runtime.restart_writer(binding(), deployment())
            intent = registry.outstanding_intent(binding())
            assert intent is not None and intent.state == "snapshots_published"
            snapshots = registry.snapshots(intent.intent_id)
            assert len(snapshots) == 1
            copy = snapshot_file(base, intent.document_id, snapshots[0].snapshot_ref)
            assert copy.exists()

            engine.fail_create = False
            # Новый запуск exporter: epoch другой, но переход продолжается тот же.
            resumed = producer(base, registry, engine)
            assert resumed.publisher_epoch != runtime.publisher_epoch
            result = await resumed.reconcile(binding(), deployment())
            assert result is not None and result.state == "exported"
            assert result.document is not None
            proof = ProcessBoundary.parse(result.document.canonical)
            assert proof.boundary_id == intent.document_id
            assert proof.publisher_epoch == resumed.publisher_epoch
            assert proof.previous_journals == snapshots

    asyncio.run(scenario())


def test_crash_after_commit_republishes_the_same_bytes(base: Path) -> None:
    """Повторная публикация пишет сохранённые байты: epoch и время от исходной фиксации."""

    async def scenario() -> None:
        engine = FakeEngine()
        with registry_at(base) as registry:
            runtime, _, container = await anchored(base, registry, engine)
            engine.crash(container)
            held = await runtime.restart_writer(binding(), deployment(), hold_export=True)
            assert held.state == "next_bound"
            intent = registry.outstanding_intent(binding())
            assert intent is not None
            document = registry.commit_boundary(intent.intent_id, runtime.publisher_epoch)
            original = document.canonical

            resumed = producer(base, registry, engine)
            result = await resumed.reconcile(binding(), deployment())
            assert result is not None and result.document is not None
            assert result.document.canonical == original
            proof = ProcessBoundary.parse(original)
            assert proof.publisher_epoch == runtime.publisher_epoch
            published = control_file(base, boundary_name(proof.boundary_seq, proof.boundary_id))
            assert published.read_bytes() == original
            # Повтор уже завершённого перехода не создаёт нового документа.
            assert await resumed.reconcile(binding(), deployment()) is None

    asyncio.run(scenario())


def test_second_transition_continues_the_counter_and_the_chain(base: Path) -> None:
    """Второй переход сверяется с next_writer принятого boundary и увеличивает счётчик."""

    async def scenario() -> None:
        engine = FakeEngine()
        with registry_at(base) as registry:
            runtime, _, first_container = await anchored(base, registry, engine)
            engine.crash(first_container)
            first = ProcessBoundary.parse(
                (await runtime.restart_writer(binding(), deployment())).document.canonical  # type: ignore[union-attr]
            )
            second_container = first.next_writer.container_id
            engine.emit(second_container, 1)
            engine.crash(second_container)
            second = ProcessBoundary.parse(
                (await runtime.restart_writer(binding(), deployment())).document.canonical  # type: ignore[union-attr]
            )
            assert second.boundary_seq == 2
            assert second.previous_boundary_id == first.boundary_id
            assert second.previous_writer.writer_id == first.next_writer.writer_id
            assert second.previous_writer.container_id == second_container
            assert [item.journal_seq for item in second.previous_journals] == [2]
            assert second.next_writer.first_journal.journal_seq == 3
            assert control_file(base, boundary_name(1, first.boundary_id)).exists()
            assert control_file(base, boundary_name(2, second.boundary_id)).exists()

    asyncio.run(scenario())


def test_low_quota_without_room_stops_before_the_new_writer(base: Path) -> None:
    """Без свободного места под новое поколение новый писатель не запускается (§8.5)."""

    async def scenario() -> None:
        engine = FakeEngine()
        with registry_at(base) as registry:
            runtime, _, container = await anchored(base, registry, engine)
            engine.crash(container)
            tight = producer(base, registry, engine, journal_quota_bytes=256 * 1024 + 100)
            with pytest.raises(BoundaryError) as error:
                await tight.restart_writer(binding(), deployment())
            assert error.value.code is BoundaryErrorCode.LIMIT_EXCEEDED
            assert len(engine.created) == 1
            assert not (slot_directory(base) / journal_name(INSTANCE, 2)).exists()

    asyncio.run(scenario())


def test_roots_inside_the_journal_root_disable_the_producer(base: Path) -> None:
    """Корни публикации не вложены в journal_root и не содержат его (§8.2)."""
    engine = FakeEngine()
    with registry_at(base) as registry:
        nested = PublicationRoots(base / "journals" / "control", base / "snapshots", COLLECTOR_GID)
        with pytest.raises(BoundaryError) as error:
            build_producer(registry, engine, nested, low_settings(base))
        assert error.value.code is BoundaryErrorCode.PRODUCER_DISABLED


def test_producer_requires_a_file_journal(base: Path) -> None:
    """Без файлового журнала Low граница процесса не поддерживается."""
    engine = FakeEngine()
    with registry_at(base) as registry:
        with pytest.raises(BoundaryError) as error:
            build_producer(registry, engine, roots(base), LowSettings(journal_root=None))
        assert error.value.code is BoundaryErrorCode.PRODUCER_DISABLED


def test_writer_container_carries_the_writer_label_and_the_allocation(base: Path) -> None:
    """Контейнер писателя получает метку writer_id и startup-выделение Low."""

    async def scenario() -> None:
        engine = FakeEngine()
        with registry_at(base) as registry:
            _, anchor, container = await anchored(base, registry, engine)
            spec = engine.specs[container]
            labels = dict(spec.labels)
            assert labels[LABEL_WRITER] == anchor.writer_id
            assert labels[LABEL_MANAGED] == "true"
            assert f"--journal-first-id={anchor.first_journal.journal_id}" in spec.command
            assert "--journal-first-seq=1" in spec.command
            assert f"--instance-id={INSTANCE}" in spec.command
            # Каталог слота монтируется записываемым, корни публикации — нет.
            assert [mount.target for mount in spec.mounts] == [JOURNAL_CONTAINER_DIR]
            assert mount_sources(spec) == [str(slot_directory(base))]

    asyncio.run(scenario())


def mount_sources(spec: ContainerSpec) -> list[str]:
    return [mount.source for mount in spec.mounts]


def test_foreign_file_in_the_slot_blocks_the_transition_until_it_is_gone(base: Path) -> None:
    """Файл, подложенный в каталог журнала, доказательством не становится (§2, §8.3).

    Неизвестное имя с префиксом экземпляра останавливает переход на состоянии fenced;
    после его удаления reconciliation повторяет копирование и доводит переход до конца.
    """

    async def scenario() -> None:
        engine = FakeEngine()
        with registry_at(base) as registry:
            runtime, anchor, container = await anchored(base, registry, engine)
            engine.crash(container)
            planted = slot_directory(base) / f"low-{INSTANCE}-pending.jsonl"
            planted.write_bytes(b'{"record":"header","schema":"honeyforge-low-journal/1"}\n')
            with pytest.raises(BoundaryError) as error:
                await runtime.restart_writer(binding(), deployment())
            assert error.value.code is BoundaryErrorCode.BOUNDARY_SNAPSHOT_UNAVAILABLE
            intent = registry.outstanding_intent(binding())
            assert intent is not None and intent.state == "fenced"
            assert registry.snapshots(intent.intent_id) == ()
            assert len(engine.created) == 1

            planted.unlink()
            result = await runtime.reconcile(binding(), deployment())
            assert result is not None and result.state == "exported"
            assert result.document is not None
            proof = ProcessBoundary.parse(result.document.canonical)
            assert proof.previous_journals[0].journal_id == anchor.first_journal.journal_id

    asyncio.run(scenario())


def test_published_documents_live_outside_the_low_mount(base: Path) -> None:
    """Корни публикации вне каталога журнала: файл в /journal proof не является (§8.2)."""

    async def scenario() -> None:
        engine = FakeEngine()
        with registry_at(base) as registry:
            runtime, anchor, container = await anchored(base, registry, engine)
            # Подложенный в каталог ловушки документ не читается producer и не публикуется.
            spoof = slot_directory(base) / ANCHOR_NAME
            spoof.write_bytes(anchor.canonical())
            published = control_file(base, ANCHOR_NAME)
            assert published.exists() and spoof.resolve() != published.resolve()
            assert slot_directory(base) not in published.parents
            engine.crash(container)
            result = await runtime.restart_writer(binding(), deployment())
            assert result.document is not None
            proof = ProcessBoundary.parse(result.document.canonical)
            # Снимки и proof лежат в своих корнях, а не рядом с журналом.
            name = proof.previous_journals[0].snapshot_ref
            assert snapshot_file(base, proof.boundary_id, name).exists()
            assert control_file(base, boundary_name(1, proof.boundary_id)).exists()

    asyncio.run(scenario())


def test_container_that_never_started_is_relaunched_with_new_identities(base: Path) -> None:
    """Контейнер без запуска снимается, writer_id и journal_id выдаются новые (§8.4)."""

    async def scenario() -> None:
        engine = FakeEngine()
        with registry_at(base) as registry:
            runtime, _, container = await anchored(base, registry, engine)
            engine.crash(container)
            engine.fail_start = True
            with pytest.raises(BoundaryError) as error:
                await runtime.restart_writer(binding(), deployment())
            assert error.value.code is BoundaryErrorCode.RUNTIME_OBSERVATION_UNKNOWN
            intent = registry.outstanding_intent(binding())
            assert intent is not None and intent.state == "next_created"
            abandoned_writer, abandoned_journal = intent.writer_id, intent.first_journal_id
            abandoned_container = intent.container_id
            assert abandoned_container is not None

            engine.fail_start = False
            result = await runtime.reconcile(binding(), deployment())
            assert result is not None and result.state == "exported"
            assert result.document is not None
            proof = ProcessBoundary.parse(result.document.canonical)
            assert proof.next_writer.writer_id != abandoned_writer
            assert proof.next_writer.container_id != abandoned_container
            assert proof.next_writer.first_journal.journal_id != abandoned_journal
            assert abandoned_container in engine.removed
            # Снимки не создавались заново: номер первого поколения сохранён.
            assert proof.next_writer.first_journal.journal_seq == 2

    asyncio.run(scenario())


def test_lost_registry_with_published_documents_is_unknown(base: Path) -> None:
    """Утрата registry при опубликованных документах не даёт выдать второй anchor.

    Историю по каталогу публикации восстанавливать запрещено (§8.4), но её наличие
    обязано остановить выдачу: иначе instance получил бы второй anchor, объявляющий
    нового писателя первым писателем instance.
    """

    async def scenario() -> None:
        engine = FakeEngine()
        with registry_at(base) as registry:
            _, anchor, _ = await anchored(base, registry, engine)
        published = control_file(base, ANCHOR_NAME)
        assert published.read_bytes() == anchor.canonical()
        # Утрата каталога состояния: новая registry ничего не знает об instance.
        shutil.rmtree(base / "state")
        with registry_at(base) as fresh:
            assert fresh.binding_state(binding()) is None
            runtime = producer(base, fresh, engine)
            with pytest.raises(BoundaryError) as error:
                await runtime.launch_first_writer(binding(), deployment())
            assert error.value.code is BoundaryErrorCode.RUNTIME_OBSERVATION_UNKNOWN
            with pytest.raises(BoundaryError) as repeated:
                await runtime.restart_writer(binding(), deployment())
            assert repeated.value.code is BoundaryErrorCode.RUNTIME_OBSERVATION_UNKNOWN
        # Второй контейнер не создавался, опубликованный anchor не изменился.
        assert len(engine.created) == 1
        assert published.read_bytes() == anchor.canonical()

    asyncio.run(scenario())


def test_lost_registry_and_deleted_anchor_still_refuse_a_second_anchor(base: Path) -> None:
    """Проверка истории не зависит от того, остался ли файл anchor на месте.

    Оператор, разбирая застрявший переход, может удалить и файл, и каталог
    состояния. Пустой слот при этом не делает instance новым: anchor выдаётся
    только instance, запущенному после включения producer (§8.1).
    """

    async def scenario() -> None:
        engine = FakeEngine()
        with registry_at(base) as registry:
            _, _, container = await anchored(base, registry, engine)
        engine.crash(container)
        # Самое недоброжелательное окружение: ни документа, ни состояния, ни журналов.
        published = control_file(base, ANCHOR_NAME)
        published.parent.chmod(0o750)
        published.unlink()
        shutil.rmtree(base / "state")
        for journal in slot_directory(base).iterdir():
            journal.unlink()
        with registry_at(base) as fresh:
            runtime = producer(base, fresh, engine)
            # Истории нет нигде: это новый instance, и здесь выдача anchor допустима.
            result = await runtime.launch_first_writer(binding(), deployment())
            assert result.state == "exported" and result.document is not None
            second = WriterAnchor.parse(result.document.canonical)
            assert second.first_journal.journal_seq == 1
            # Но повторная выдача при живой registry по-прежнему конфликт.
            with pytest.raises(BoundaryError) as error:
                await runtime.launch_first_writer(binding(), deployment())
            assert error.value.code is BoundaryErrorCode.BOUNDARY_SEQUENCE_CONFLICT

    asyncio.run(scenario())


def test_resumed_fencing_is_verified_again_before_snapshots(base: Path) -> None:
    """Возобновление из fenced заново подтверждает отсутствие RW доступа (§2, §3 шаг 2).

    Durable запись об ограждении сделана в прошлом. Если к каталогу слота появился
    доступ на запись, снимок закрепил бы чужие байты как доказанно старые.
    """

    async def scenario() -> None:
        engine = FakeEngine()
        with registry_at(base) as registry:
            runtime, _, container = await anchored(base, registry, engine)
            engine.crash(container)
            engine.fail_writer_lookup = True
            with pytest.raises(BoundaryError):
                await runtime.restart_writer(binding(), deployment())
            intent = registry.outstanding_intent(binding())
            assert intent is not None and intent.state == "fenced"

            engine.fail_writer_lookup = False
            engine.extra_holders = ("f" * 64,)
            with pytest.raises(BoundaryError) as error:
                await runtime.reconcile(binding(), deployment())
            assert error.value.code is BoundaryErrorCode.PREVIOUS_WRITER_NOT_FENCED
            assert registry.snapshots(intent.intent_id) == ()
            assert len(engine.created) == 1

            engine.extra_holders = ()
            result = await runtime.reconcile(binding(), deployment())
            assert result is not None and result.state == "exported"

    asyncio.run(scenario())


def test_bytes_changed_after_fencing_are_not_pinned_as_old(
    base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Снимок закрепляет именно старые байты: иначе SOURCE_CHANGED (§3 шаг 3, §8.3).

    Между перечислением цепочки и копированием писатель уже огорожен, поэтому любое
    расхождение означает неполное ограждение. Такой переход становится неизвестным,
    и новые переходы instance запрещаются до решения оператора (§2).
    """

    async def scenario() -> None:
        engine = FakeEngine()
        with registry_at(base) as registry:
            runtime, _, container = await anchored(base, registry, engine)
            engine.crash(container)
            journal = slot_directory(base) / journal_name(INSTANCE, 1)
            original = journal.read_bytes()
            measured = JournalSlot.collect_writer_chain

            def changed_after_measurement(
                slot: JournalSlot, journal_id: str, journal_seq: int
            ) -> tuple[JournalFile, ...]:
                """Дописывает байты сразу после измерения цепочки.

                Так выглядит неполное ограждение: кто-то сохранил доступ на запись
                и изменил журнал между перечислением набора и копированием.
                """
                chain = measured(slot, journal_id, journal_seq)
                with journal.open("ab") as stream:
                    stream.write(b'{"record":"event","seq":99}\n')
                return chain

            monkeypatch.setattr(JournalSlot, "collect_writer_chain", changed_after_measurement)
            with pytest.raises(BoundaryError) as error:
                await runtime.restart_writer(binding(), deployment())
            assert error.value.code is BoundaryErrorCode.SOURCE_CHANGED
            state = registry.binding_state(binding())
            assert state is not None and not state.supported and state.blocked_reason
            # Новый писатель не запускался, документа перехода нет.
            assert len(engine.created) == 1
            directory = control_file(base, ANCHOR_NAME).parent
            assert sorted(path.name for path in directory.iterdir()) == [ANCHOR_NAME]
            assert journal.read_bytes().startswith(original)

            monkeypatch.setattr(JournalSlot, "collect_writer_chain", measured)
            with pytest.raises(BoundaryError) as blocked:
                await runtime.restart_writer(binding(), deployment())
            assert blocked.value.code is BoundaryErrorCode.BOUNDARY_SEQUENCE_CONFLICT

    asyncio.run(scenario())


def test_published_snapshots_without_a_registry_list_are_adopted(base: Path) -> None:
    """Сбой между публикацией каталога снимков и записью перечня не запирает переход.

    Копии write-once и неизменны, поэтому перечень восстанавливается по ним со
    сверкой байтов цепочки, а не повторным копированием (§8.3, §8.4).
    """

    async def scenario() -> None:
        engine = FakeEngine()
        with registry_at(base) as registry:
            runtime, anchor, container = await anchored(base, registry, engine)
            engine.crash(container)
            original = (slot_directory(base) / journal_name(INSTANCE, 1)).read_bytes()
            failing = producer(base, registry, engine)

            def refuse(_intent_id: str, _entries: object) -> None:
                raise BoundaryError(
                    BoundaryErrorCode.REGISTRY_UNAVAILABLE, "подмена: запись перечня"
                )

            # Сбой ровно между атомарной публикацией каталога и записью перечня.
            recorded = registry.record_snapshots
            registry.record_snapshots = refuse  # type: ignore[method-assign]
            try:
                with pytest.raises(BoundaryError):
                    await failing.restart_writer(binding(), deployment())
            finally:
                registry.record_snapshots = recorded  # type: ignore[method-assign]
            intent = registry.outstanding_intent(binding())
            assert intent is not None and intent.state == "fenced"
            assert registry.snapshots(intent.intent_id) == ()
            copy = snapshot_file(base, intent.document_id, journal_name(INSTANCE, 1))
            assert copy.read_bytes() == original

            result = await runtime.reconcile(binding(), deployment())
            assert result is not None and result.state == "exported"
            assert result.document is not None
            proof = ProcessBoundary.parse(result.document.canonical)
            assert proof.boundary_id == intent.document_id
            assert proof.previous_journals[0].journal_id == anchor.first_journal.journal_id
            assert proof.previous_journals[0].sha256 == hashlib.sha256(original).hexdigest()

    asyncio.run(scenario())


def test_missing_published_document_is_republished_with_the_same_bytes(base: Path) -> None:
    """Строка exported §8.4: пропавший файл публикуется теми же сохранёнными байтами."""

    async def scenario() -> None:
        engine = FakeEngine()
        with registry_at(base) as registry:
            runtime, _, container = await anchored(base, registry, engine)
            engine.crash(container)
            result = await runtime.restart_writer(binding(), deployment())
            assert result.document is not None
            proof = ProcessBoundary.parse(result.document.canonical)
            name = boundary_name(proof.boundary_seq, proof.boundary_id)
            published = control_file(base, name)
            original = published.read_bytes()

            published.parent.chmod(0o750)
            published.unlink()
            restored = await runtime.reconcile(binding(), deployment())
            assert restored is not None and restored.document is not None
            assert restored.document.canonical == original
            assert published.read_bytes() == original
            # Документ не пересобран: epoch и время от исходной фиксации.
            assert ProcessBoundary.parse(published.read_bytes()).publisher_epoch == (
                proof.publisher_epoch
            )
            # Повторный вызов без пропавших файлов ничего не меняет.
            assert await runtime.reconcile(binding(), deployment()) is None

    asyncio.run(scenario())


def test_restart_after_held_export_finishes_without_a_second_rotation(base: Path) -> None:
    """Повторный restart доводит удержанный переход и не ротирует писателя снова.

    Иначе оператор, перепутавший finish и restart, молча убивал бы только что
    привязанного писателя B и получал два перехода подряд.
    """

    async def scenario() -> None:
        engine = FakeEngine()
        with registry_at(base) as registry:
            runtime, _, container = await anchored(base, registry, engine)
            engine.crash(container)
            held = await runtime.restart_writer(binding(), deployment(), hold_export=True)
            assert held.writer is not None
            writer_b = held.writer.container_id

            finished = await runtime.restart_writer(binding(), deployment())
            assert finished.state == "exported" and finished.document is not None
            proof = ProcessBoundary.parse(finished.document.canonical)
            assert proof.boundary_seq == 1
            assert proof.next_writer.container_id == writer_b
            assert writer_b not in engine.removed
            assert len(engine.created) == 2
            assert registry.binding_state(binding()).last_boundary_seq == 1  # type: ignore[union-attr]

    asyncio.run(scenario())


def test_failed_removal_of_the_previous_writer_is_unknown(base: Path) -> None:
    """Отказ Engine на удалении прежнего писателя — неизвестный исход, не ограждение."""

    async def scenario() -> None:
        engine = FakeEngine()
        with registry_at(base) as registry:
            runtime, _, container = await anchored(base, registry, engine)
            engine.crash(container)
            engine.fail_remove = True
            with pytest.raises(BoundaryError) as error:
                await runtime.restart_writer(binding(), deployment())
            assert error.value.code is BoundaryErrorCode.RUNTIME_OBSERVATION_UNKNOWN
            intent = registry.outstanding_intent(binding())
            assert intent is not None and intent.state == "intent"
            assert len(engine.created) == 1
            directory = control_file(base, ANCHOR_NAME).parent
            assert sorted(path.name for path in directory.iterdir()) == [ANCHOR_NAME]

            engine.fail_remove = False
            result = await runtime.reconcile(binding(), deployment())
            assert result is not None and result.state == "exported"

    asyncio.run(scenario())


def test_snapshot_root_quota_stops_the_transition_before_fencing(base: Path) -> None:
    """Квота snapshot_root проверяется до ограждения: писатель остаётся живым (§8.3)."""

    async def scenario() -> None:
        engine = FakeEngine()
        with registry_at(base) as registry:
            runtime, _, container = await anchored(base, registry, engine)
            engine.emit(container, 3)
            tight = BoundaryProducer(
                registry,
                WriterLauncher(low_settings(base), engine, roots(base)),
                SnapshotStore(
                    ProtectedTree(base / "snapshots", COLLECTOR_GID, LinuxWriteOnceRename()),
                    quota_bytes=16,
                ),
                ProtectedTree(base / "control", COLLECTOR_GID, LinuxWriteOnceRename()),
            )
            with pytest.raises(BoundaryError) as error:
                await tight.restart_writer(binding(), deployment())
            assert error.value.code is BoundaryErrorCode.BOUNDARY_SNAPSHOT_UNAVAILABLE
            # Ограждение не выполнялось: контейнер писателя A на месте.
            assert container not in engine.removed
            assert registry.outstanding_intent(binding()) is None
            assert not (base / "snapshots" / "low-snapshots").exists()

    asyncio.run(scenario())
