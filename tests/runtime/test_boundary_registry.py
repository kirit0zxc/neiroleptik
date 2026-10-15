"""Долговечная registry границы процесса: состояния, счётчик и неизменность байт (§8.4)."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent.runtime.boundary.documents import (
    ProcessBoundary,
    SnapshotEntry,
    WriterAnchor,
    WriterBinding,
)
from agent.runtime.boundary.errors import BoundaryError, BoundaryErrorCode
from agent.runtime.boundary.registry import BoundaryRegistry

AGENT = "00000000-0000-4000-8000-000000000001"
DEPLOYMENT = "e0d692f0-fb4c-4a37-920e-0c8683471639"
INSTANCE = "a2df557e-785f-48f3-ba6e-348b76730f41"
EPOCH = "df5c9ee5-19b8-4c6d-8aac-d3d82e787210"
HEADER = "e0ac29d1eebe54fc294feae7bdb5e0439f84f9c1b1f4c03979fea599441a7a28"
NEXT_HEADER = "e6ed4ba956d7438bedee7ea34949069e79b6ac3ab4ed1c98774364fe43c717b2"
CONTAINER_A = "a" * 64
CONTAINER_B = "b" * 64
CONTAINER_C = "c" * 64
JOURNAL_MAX = 8 * 1024 * 1024
JOURNAL_QUOTA = 256 * 1024 * 1024


def binding(revision: int = 1) -> WriterBinding:
    return WriterBinding(AGENT, DEPLOYMENT, INSTANCE, revision)


def identity(prefix: str) -> str:
    """Удобный синтетический UUID для идентичностей теста."""
    return f"{prefix * 8}-{prefix * 4}-4{prefix * 3}-8{prefix * 3}-{prefix * 12}"


def moment(second: int = 0) -> datetime:
    return datetime(2026, 10, 14, 10, 0, second, tzinfo=UTC)


def registry(tmp_path: Path) -> BoundaryRegistry:
    """Каталог состояния создаёт сама registry: его права 0700 проверяются на POSIX."""
    return BoundaryRegistry(tmp_path / "state" / "boundary.db")


def entry(seq: int, journal: str) -> SnapshotEntry:
    return SnapshotEntry(
        journal_id=journal,
        journal_seq=seq,
        snapshot_ref=f"low-{INSTANCE}-{seq:06d}.jsonl",
        size_bytes=885,
        sha256="4" * 64,
        complete_lf_end=885,
        footer_present=False,
    )


def anchor_intent(
    store: BoundaryRegistry, *, writer: str = "1", journal: str = "2", bound: bool = True
) -> str:
    """Проводит anchor по графу §8.4 и возвращает intent_id.

    `bound=False` останавливается на `next_started`: это стадия с выданным RW, с
    которой producer и закрывает непривязанного писателя.
    """
    intent = store.begin_intent(
        binding(),
        kind="anchor",
        intent_id=identity("3"),
        document_id=identity("4"),
        writer_id=identity(writer),
        first_journal_id=identity(journal),
        first_journal_seq=1,
        journal_max_bytes=JOURNAL_MAX,
        journal_quota_bytes=JOURNAL_QUOTA,
        engine_identity="engine/29",
        now=moment(),
    )
    store.advance(intent.intent_id, "next_created", container_id=CONTAINER_A)
    store.advance(intent.intent_id, "next_started")
    if bound:
        store.advance(intent.intent_id, "next_bound", header_sha256=HEADER)
    return intent.intent_id


def publish_anchor(store: BoundaryRegistry) -> WriterAnchor:
    """Фиксирует и публикует anchor, возвращая разобранный документ."""
    intent_id = anchor_intent(store)
    document = store.commit_anchor(intent_id, EPOCH, moment(1))
    store.mark_published(intent_id)
    return WriterAnchor.parse(document.canonical)


def boundary_intent(
    store: BoundaryRegistry,
    *,
    previous_writer: str,
    previous_container: str,
    writer: str,
    journal: str,
    intent: str,
    document: str,
    journals: tuple[SnapshotEntry, ...],
    next_seq: int,
    container: str = CONTAINER_B,
) -> str:
    """Проводит переход от intent до next_bound и возвращает intent_id."""
    stored = store.begin_intent(
        binding(),
        kind="boundary",
        intent_id=identity(intent),
        document_id=identity(document),
        writer_id=identity(writer),
        first_journal_id=identity(journal),
        journal_max_bytes=JOURNAL_MAX,
        journal_quota_bytes=JOURNAL_QUOTA,
        engine_identity="engine/29",
        previous_writer_id=previous_writer,
        previous_container_id=previous_container,
        now=moment(2),
    )
    store.advance(stored.intent_id, "fenced", fenced_at="2026-10-14T10:00:03Z")
    store.record_snapshots(stored.intent_id, journals)
    store.advance(stored.intent_id, "snapshots_published", first_journal_seq=next_seq)
    store.advance(stored.intent_id, "next_created", container_id=container)
    store.advance(stored.intent_id, "next_started")
    store.advance(stored.intent_id, "next_bound", header_sha256=NEXT_HEADER)
    return stored.intent_id


def test_anchor_binds_the_first_chain_of_the_instance(tmp_path: Path) -> None:
    """Выданный anchor становится начальной привязкой writer и первой цепочки (§8.1)."""
    with registry(tmp_path) as store:
        assert store.binding_state(binding()) is None
        anchor = publish_anchor(store)
        assert anchor.binding == binding()
        assert anchor.first_journal.journal_seq == 1
        assert anchor.publisher_epoch == EPOCH
        state = store.binding_state(binding())
        assert state is not None and state.head is not None
        assert state.anchor_id == anchor.anchor_id
        assert state.head.writer_id == anchor.writer_id
        assert state.head.container_id == CONTAINER_A
        assert state.last_boundary_seq == 0 and state.last_boundary_id is None
        assert state.supported


def test_second_anchor_of_the_same_instance_is_a_conflict(tmp_path: Path) -> None:
    """Instance имеет ровно один anchor; другой — конфликт, а не замена (§8.1)."""
    with registry(tmp_path) as store:
        publish_anchor(store)
        with pytest.raises(BoundaryError) as error:
            anchor_intent(store, writer="5", journal="6")
        assert error.value.code is BoundaryErrorCode.BOUNDARY_SEQUENCE_CONFLICT


def test_boundary_counter_grows_and_links_to_the_previous_transition(tmp_path: Path) -> None:
    """boundary_seq выделяется только при commit и ссылается на прежний переход (§5)."""
    with registry(tmp_path) as store:
        anchor = publish_anchor(store)
        first_id = boundary_intent(
            store,
            previous_writer=anchor.writer_id,
            previous_container=CONTAINER_A,
            writer="7",
            journal="8",
            intent="9",
            document="a",
            journals=(entry(1, anchor.first_journal.journal_id),),
            next_seq=2,
        )
        document = store.commit_boundary(first_id, EPOCH, moment(4))
        store.mark_published(first_id)
        first = ProcessBoundary.parse(document.canonical)
        assert (first.boundary_seq, first.previous_boundary_id) == (1, None)
        assert first.previous_writer.writer_id == anchor.writer_id
        assert first.next_writer.first_journal.journal_seq == 2

        second_id = boundary_intent(
            store,
            previous_writer=first.next_writer.writer_id,
            previous_container=first.next_writer.container_id,
            writer="b",
            journal="c",
            intent="d",
            document="e",
            journals=(entry(2, first.next_writer.first_journal.journal_id),),
            next_seq=3,
            container=CONTAINER_C,
        )
        second = ProcessBoundary.parse(store.commit_boundary(second_id, EPOCH, moment(5)).canonical)
        store.mark_published(second_id)
        assert second.boundary_seq == 2
        assert second.previous_boundary_id == first.boundary_id
        state = store.binding_state(binding())
        assert state is not None and state.last_boundary_id == second.boundary_id


def test_committed_bytes_are_stored_once_and_never_rebuilt(tmp_path: Path) -> None:
    """Канонические байты создаются в транзакции commit и далее только повторяются."""
    with registry(tmp_path) as store:
        intent_id = anchor_intent(store)
        document = store.commit_anchor(intent_id, EPOCH, moment(1))
        stored = store.document(document.document_id)
        assert stored is not None and stored.canonical == document.canonical
        assert not stored.published
        published = store.mark_published(intent_id)
        assert published.canonical == document.canonical
        assert published.published
        # Повторная фиксация того же документа невозможна: состояние уже не next_bound.
        with pytest.raises(BoundaryError):
            store.commit_anchor(intent_id, identity("f"), moment(9))


def test_only_one_outstanding_intent_per_binding(tmp_path: Path) -> None:
    """На привязке допускается один незавершённый intent (§8.4)."""
    with registry(tmp_path) as store:
        anchor_intent(store)
        with pytest.raises(BoundaryError) as error:
            store.begin_intent(
                binding(),
                kind="anchor",
                intent_id=identity("5"),
                document_id=identity("6"),
                writer_id=identity("7"),
                first_journal_id=identity("8"),
                first_journal_seq=1,
                journal_max_bytes=JOURNAL_MAX,
                journal_quota_bytes=JOURNAL_QUOTA,
                engine_identity="engine/29",
            )
        assert error.value.code is BoundaryErrorCode.BOUNDARY_SEQUENCE_CONFLICT


@pytest.mark.parametrize("reused", ["writer", "journal", "document", "intent"])
def test_identities_are_burned_even_after_a_failure(tmp_path: Path, reused: str) -> None:
    """boundary_id, anchor_id, writer_id и journal_id повторно не используются (§8.4)."""
    with registry(tmp_path) as store:
        intent_id = anchor_intent(store, bound=False)
        store.fail_after_launch(intent_id, "заголовок не получен")
        arguments = {
            "intent_id": identity("5"),
            "document_id": identity("6"),
            "writer_id": identity("7"),
            "first_journal_id": identity("8"),
        }
        field = {
            "writer": "writer_id",
            "journal": "first_journal_id",
            "document": "document_id",
            "intent": "intent_id",
        }[reused]
        burned = {
            "writer": identity("1"),
            "journal": identity("2"),
            "document": identity("4"),
            "intent": identity("3"),
        }[reused]
        arguments[field] = burned
        with pytest.raises(BoundaryError) as error:
            store.begin_intent(
                binding(),
                kind="anchor",
                first_journal_seq=1,
                journal_max_bytes=JOURNAL_MAX,
                journal_quota_bytes=JOURNAL_QUOTA,
                engine_identity="engine/29",
                **arguments,
            )
        assert error.value.code is BoundaryErrorCode.BOUNDARY_SEQUENCE_CONFLICT


def test_failed_launch_blocks_further_transitions_of_the_instance(tmp_path: Path) -> None:
    """После неудачи с выданным RW переход не продолжается автоматически (§8.4)."""
    with registry(tmp_path) as store:
        intent_id = anchor_intent(store, bound=False)
        store.fail_after_launch(intent_id, "заголовок не совпал")
        state = store.binding_state(binding())
        assert state is not None and not state.supported and state.blocked_reason
        assert state.last_boundary_seq == 0
        assert store.outstanding_intent(binding()) is None


@pytest.mark.parametrize("state", ["intent", "next_created", "next_bound"])
def test_states_move_only_forward(tmp_path: Path, state: str) -> None:
    """Повтор и откат состояния intent запрещены."""
    with registry(tmp_path) as store:
        intent_id = anchor_intent(store)
        with pytest.raises(BoundaryError) as error:
            store.advance(intent_id, state)  # type: ignore[arg-type]
        assert error.value.code is BoundaryErrorCode.REGISTRY_UNAVAILABLE


def test_commit_requires_fencing_header_and_snapshots(tmp_path: Path) -> None:
    """Переход без ограждения или снимков не фиксируется.

    Проверяются две защиты подряд: граф §8.4 не даёт обойти ограждение, а фиксация
    отказывает на законном пути, если перечень снимков не записан.
    """
    with registry(tmp_path) as store:
        anchor = publish_anchor(store)
        stored = store.begin_intent(
            binding(),
            kind="boundary",
            intent_id=identity("9"),
            document_id=identity("a"),
            writer_id=identity("7"),
            first_journal_id=identity("8"),
            journal_max_bytes=JOURNAL_MAX,
            journal_quota_bytes=JOURNAL_QUOTA,
            engine_identity="engine/29",
            previous_writer_id=anchor.writer_id,
            previous_container_id=CONTAINER_A,
        )
        # Ограждение нельзя пропустить: это не соседнее ребро графа перехода.
        with pytest.raises(BoundaryError) as jumped:
            store.advance(stored.intent_id, "snapshots_published", first_journal_seq=2)
        assert jumped.value.code is BoundaryErrorCode.REGISTRY_UNAVAILABLE

        store.advance(stored.intent_id, "fenced", fenced_at="2026-10-14T10:00:03Z")
        store.advance(stored.intent_id, "snapshots_published", first_journal_seq=2)
        store.advance(stored.intent_id, "next_created", container_id=CONTAINER_B)
        store.advance(stored.intent_id, "next_started")
        store.advance(stored.intent_id, "next_bound", header_sha256=NEXT_HEADER)
        # Перечень снимков — отдельная операция: без него документ не выдаётся.
        with pytest.raises(BoundaryError) as error:
            store.commit_boundary(stored.intent_id, EPOCH, moment(4))
        assert error.value.code is BoundaryErrorCode.BOUNDARY_SNAPSHOT_UNAVAILABLE


def test_commit_refuses_a_writer_that_is_not_the_current_chain(tmp_path: Path) -> None:
    """Закрываемый writer обязан совпадать с текущей цепочкой привязки (§5)."""
    with registry(tmp_path) as store:
        anchor = publish_anchor(store)
        intent_id = boundary_intent(
            store,
            previous_writer=identity("7"),
            previous_container=CONTAINER_C,
            writer="8",
            journal="9",
            intent="a",
            document="b",
            journals=(entry(1, anchor.first_journal.journal_id),),
            next_seq=2,
        )
        with pytest.raises(BoundaryError) as error:
            store.commit_boundary(intent_id, EPOCH, moment(4))
        assert error.value.code is BoundaryErrorCode.BOUNDARY_BINDING_MISMATCH


def test_snapshot_list_is_written_once(tmp_path: Path) -> None:
    """Перечень копий фиксируется один раз: повторная публикация его не пересобирает."""
    with registry(tmp_path) as store:
        anchor = publish_anchor(store)
        stored = store.begin_intent(
            binding(),
            kind="boundary",
            intent_id=identity("9"),
            document_id=identity("a"),
            writer_id=identity("7"),
            first_journal_id=identity("8"),
            journal_max_bytes=JOURNAL_MAX,
            journal_quota_bytes=JOURNAL_QUOTA,
            engine_identity="engine/29",
            previous_writer_id=anchor.writer_id,
            previous_container_id=CONTAINER_A,
        )
        entries = (entry(1, anchor.first_journal.journal_id),)
        # Перечень относится только к ограждённому переходу: до fenced его не примут.
        with pytest.raises(BoundaryError) as unfenced:
            store.record_snapshots(stored.intent_id, entries)
        assert unfenced.value.code is BoundaryErrorCode.REGISTRY_UNAVAILABLE
        store.advance(stored.intent_id, "fenced", fenced_at="2026-10-14T10:00:03Z")
        store.record_snapshots(stored.intent_id, entries)
        assert store.snapshots(stored.intent_id) == entries
        with pytest.raises(BoundaryError) as error:
            store.record_snapshots(stored.intent_id, entries)
        assert error.value.code is BoundaryErrorCode.BOUNDARY_SEQUENCE_CONFLICT


def test_reallocation_returns_to_the_stage_before_create(tmp_path: Path) -> None:
    """Снятая попытка получает новые идентичности, прежние остаются сожжёнными (§8.4)."""
    with registry(tmp_path) as store:
        anchor = publish_anchor(store)
        stored = store.begin_intent(
            binding(),
            kind="boundary",
            intent_id=identity("9"),
            document_id=identity("a"),
            writer_id=identity("7"),
            first_journal_id=identity("8"),
            journal_max_bytes=JOURNAL_MAX,
            journal_quota_bytes=JOURNAL_QUOTA,
            engine_identity="engine/29",
            previous_writer_id=anchor.writer_id,
            previous_container_id=CONTAINER_A,
        )
        store.advance(stored.intent_id, "fenced", fenced_at="2026-10-14T10:00:03Z")
        store.record_snapshots(stored.intent_id, (entry(1, anchor.first_journal.journal_id),))
        store.advance(stored.intent_id, "snapshots_published", first_journal_seq=2)
        store.advance(stored.intent_id, "next_created", container_id=CONTAINER_B)
        updated = store.reallocate(stored.intent_id, identity("b"), identity("c"))
        assert updated.state == "snapshots_published"
        assert updated.container_id is None
        assert updated.writer_id == identity("b")
        assert updated.first_journal_seq == 2
        # Прежние идентичности сожжены и повторно не выдаются.
        with pytest.raises(BoundaryError):
            store.reallocate(stored.intent_id, identity("7"), identity("d"))


def test_state_survives_reopening_and_a_second_owner_is_refused(tmp_path: Path) -> None:
    """Состояние переживает рестарт; каталог состояния принадлежит одному процессу."""
    path = tmp_path / "state" / "boundary.db"
    with BoundaryRegistry(path) as store:
        anchor = publish_anchor(store)
        with pytest.raises(BoundaryError) as error:
            BoundaryRegistry(path)
        assert error.value.code is BoundaryErrorCode.REGISTRY_UNAVAILABLE
    with BoundaryRegistry(path) as reopened:
        state = reopened.binding_state(binding())
        assert state is not None and state.anchor_id == anchor.anchor_id
        document = reopened.document(anchor.anchor_id)
        assert document is not None and WriterAnchor.parse(document.canonical) == anchor


def test_foreign_database_file_is_refused(tmp_path: Path) -> None:
    """Чужая схема или чужой application_id не принимаются за registry."""
    path = tmp_path / "state" / "boundary.db"
    path.parent.mkdir(mode=0o700, parents=True)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE unrelated (id TEXT)")
    with pytest.raises(BoundaryError) as error:
        BoundaryRegistry(path)
    assert error.value.code is BoundaryErrorCode.REGISTRY_UNAVAILABLE


def test_other_applied_revision_of_the_instance_is_a_binding_mismatch(tmp_path: Path) -> None:
    """Registry знает применённую revision instance: другая — расхождение привязки."""
    with registry(tmp_path) as store:
        publish_anchor(store)
        with pytest.raises(BoundaryError) as error:
            store.binding_state(binding(revision=2))
        assert error.value.code is BoundaryErrorCode.BOUNDARY_BINDING_MISMATCH


def test_second_anchor_is_refused_before_the_container_is_created(tmp_path: Path) -> None:
    """Защита §8.1 срабатывает на выдаче intent, а не только при фиксации.

    Идентичности здесь свежие, поэтому отказ не может прийти из таблицы сожжённых:
    проверяется именно правило «instance имеет ровно один anchor».
    """
    with registry(tmp_path) as store:
        publish_anchor(store)
        with pytest.raises(BoundaryError) as error:
            store.begin_intent(
                binding(),
                kind="anchor",
                intent_id=identity("7"),
                document_id=identity("8"),
                writer_id=identity("9"),
                first_journal_id=identity("a"),
                first_journal_seq=1,
                journal_max_bytes=JOURNAL_MAX,
                journal_quota_bytes=JOURNAL_QUOTA,
                engine_identity="engine/29",
            )
        assert error.value.code is BoundaryErrorCode.BOUNDARY_SEQUENCE_CONFLICT
        # Идентичности отказавшей попытки не сожжены: отказ произошёл до их записи.
        assert store.outstanding_intent(binding()) is None


def test_blocked_binding_refuses_new_transitions(tmp_path: Path) -> None:
    """Неизвестный исход запрещает новые переходы instance до решения оператора (§2)."""
    with registry(tmp_path) as store:
        anchor = publish_anchor(store)
        store.block_binding(binding(), "ограждение не подтверждено")
        state = store.binding_state(binding())
        assert state is not None and not state.supported
        assert state.blocked_reason == "ограждение не подтверждено"
        with pytest.raises(BoundaryError) as error:
            boundary_intent(
                store,
                previous_writer=anchor.writer_id,
                previous_container=CONTAINER_A,
                writer="7",
                journal="8",
                intent="9",
                document="a",
                journals=(entry(1, anchor.first_journal.journal_id),),
                next_seq=2,
            )
        assert error.value.code is BoundaryErrorCode.BOUNDARY_SEQUENCE_CONFLICT
        # Уже выданный anchor остаётся действительным.
        assert store.document(anchor.anchor_id) is not None


def test_completed_intent_is_not_closed_again(tmp_path: Path) -> None:
    """Завершённый переход нельзя превратить в неудачу и заблокировать им привязку."""
    with registry(tmp_path) as store:
        intent_id = anchor_intent(store)
        store.commit_anchor(intent_id, EPOCH, moment(1))
        store.mark_published(intent_id)
        with pytest.raises(BoundaryError) as error:
            store.fail_after_launch(intent_id, "поздняя попытка закрыть")
        assert error.value.code is BoundaryErrorCode.REGISTRY_UNAVAILABLE
        state = store.binding_state(binding())
        assert state is not None and state.supported


def boundary_begin(store: BoundaryRegistry, anchor: WriterAnchor) -> str:
    """Начинает переход от принятой цепочки и возвращает intent_id."""
    stored = store.begin_intent(
        binding(),
        kind="boundary",
        intent_id=identity("9"),
        document_id=identity("a"),
        writer_id=identity("7"),
        first_journal_id=identity("8"),
        journal_max_bytes=JOURNAL_MAX,
        journal_quota_bytes=JOURNAL_QUOTA,
        engine_identity="engine/29",
        previous_writer_id=anchor.writer_id,
        previous_container_id=CONTAINER_A,
    )
    return stored.intent_id


def anchor_begin(store: BoundaryRegistry) -> str:
    """Создаёт anchor intent без продвижения по графу."""
    stored = store.begin_intent(
        binding(),
        kind="anchor",
        intent_id=identity("3"),
        document_id=identity("4"),
        writer_id=identity("1"),
        first_journal_id=identity("2"),
        first_journal_seq=1,
        journal_max_bytes=JOURNAL_MAX,
        journal_quota_bytes=JOURNAL_QUOTA,
        engine_identity="engine/29",
    )
    return stored.intent_id


@pytest.mark.parametrize(
    ("state", "fields"),
    [
        ("next_started", {}),
        ("next_bound", {"header_sha256": NEXT_HEADER}),
        ("fenced", {"fenced_at": "2026-10-14T10:00:03Z"}),
        ("snapshots_published", {"first_journal_seq": 2}),
    ],
)
def test_anchor_graph_refuses_jumps_and_foreign_stages(
    tmp_path: Path, state: str, fields: dict[str, object]
) -> None:
    """У anchor есть ровно одно соседнее ребро; остальные стадии ему не принадлежат.

    Сравнение номеров состояний допускало скачки, и документ выдавался в обход
    create, start или ограждения. Согласованный граф §8.4 это закрывает.
    """
    with registry(tmp_path) as store:
        intent_id = anchor_begin(store)
        with pytest.raises(BoundaryError) as error:
            store.advance(intent_id, state, **fields)  # type: ignore[arg-type]
        assert error.value.code is BoundaryErrorCode.REGISTRY_UNAVAILABLE
        # Состояние, привязка и отсутствие документа сохраняются.
        current = store.intent(intent_id)
        assert current is not None and current.state == "intent"
        assert current.container_id is None and current.header_sha256 is None
        assert store.document(identity("4")) is None
        binding_state = store.binding_state(binding())
        assert binding_state is not None and binding_state.anchor_id is None


@pytest.mark.parametrize(
    ("state", "fields"),
    [
        ("snapshots_published", {"first_journal_seq": 2}),
        ("next_created", {"container_id": CONTAINER_B}),
        ("next_started", {}),
        ("next_bound", {"header_sha256": NEXT_HEADER}),
    ],
)
def test_boundary_graph_requires_fencing_first(
    tmp_path: Path, state: str, fields: dict[str, object]
) -> None:
    """Первое ребро перехода — только ограждение (§3 шаг 2, §8.4)."""
    with registry(tmp_path) as store:
        anchor = publish_anchor(store)
        intent_id = boundary_begin(store, anchor)
        with pytest.raises(BoundaryError) as error:
            store.advance(intent_id, state, **fields)  # type: ignore[arg-type]
        assert error.value.code is BoundaryErrorCode.REGISTRY_UNAVAILABLE
        current = store.intent(intent_id)
        assert current is not None and current.state == "intent"


def test_boundary_graph_refuses_create_before_snapshots(tmp_path: Path) -> None:
    """После ограждения следующий шаг — снимки, а не создание контейнера (§3 шаг 3)."""
    with registry(tmp_path) as store:
        anchor = publish_anchor(store)
        intent_id = boundary_begin(store, anchor)
        store.advance(intent_id, "fenced", fenced_at="2026-10-14T10:00:03Z")
        with pytest.raises(BoundaryError) as error:
            store.advance(intent_id, "next_created", container_id=CONTAINER_B)
        assert error.value.code is BoundaryErrorCode.REGISTRY_UNAVAILABLE


@pytest.mark.parametrize("state", ["committed", "exported", "failed_after_launch"])
def test_advance_cannot_set_states_of_other_operations(tmp_path: Path, state: str) -> None:
    """Фиксацию, публикацию и неудачу устанавливают только свои операции.

    Иначе `advance(committed)` выдавал состояние зафиксированного перехода без
    созданных канонических байт, а `mark_published` завершал intent без документа.
    """
    with registry(tmp_path) as store:
        intent_id = anchor_intent(store, bound=False)
        with pytest.raises(BoundaryError) as error:
            store.advance(intent_id, state)  # type: ignore[arg-type]
        assert error.value.code is BoundaryErrorCode.REGISTRY_UNAVAILABLE
        current = store.intent(intent_id)
        assert current is not None and current.state == "next_started"
        assert store.document(identity("4")) is None


def test_each_edge_accepts_only_its_own_fields(tmp_path: Path) -> None:
    """Ребро принимает ровно свои поля: ни лишних, ни отсутствующих."""
    with registry(tmp_path) as store:
        intent_id = anchor_begin(store)
        with pytest.raises(BoundaryError) as missing:
            store.advance(intent_id, "next_created")
        assert missing.value.code is BoundaryErrorCode.REGISTRY_UNAVAILABLE
        with pytest.raises(BoundaryError) as extra:
            store.advance(intent_id, "next_created", container_id=CONTAINER_A, header_sha256=HEADER)
        assert extra.value.code is BoundaryErrorCode.REGISTRY_UNAVAILABLE
        store.advance(intent_id, "next_created", container_id=CONTAINER_A)
        # Поле следующего ребра на ребре без полей: выделение не переписывается.
        with pytest.raises(BoundaryError) as late:
            store.advance(intent_id, "next_started", first_journal_seq=5)
        assert late.value.code is BoundaryErrorCode.REGISTRY_UNAVAILABLE
        current = store.intent(intent_id)
        assert current is not None and current.first_journal_seq == 1


def test_fixed_value_of_an_edge_is_not_overwritten(tmp_path: Path) -> None:
    """Уже закреплённое значение не переписывается повторным проходом ребра."""
    with registry(tmp_path) as store:
        anchor = publish_anchor(store)
        intent_id = boundary_begin(store, anchor)
        store.advance(intent_id, "fenced", fenced_at="2026-10-14T10:00:03Z")
        store.advance(intent_id, "snapshots_published", first_journal_seq=2)
        store.advance(intent_id, "next_created", container_id=CONTAINER_B)
        with pytest.raises(BoundaryError):
            store.advance(intent_id, "next_created", container_id=CONTAINER_C)
        current = store.intent(intent_id)
        assert current is not None and current.container_id == CONTAINER_B
        assert current.first_journal_seq == 2


def test_committed_intent_is_protected_from_late_failure(tmp_path: Path) -> None:
    """Зафиксированный документ не закрывается неудачей после запуска.

    Иначе поздний вызов блокировал бы привязку, у которой доказательство уже
    выдано, и переход становился бы невосстановимым.
    """
    with registry(tmp_path) as store:
        intent_id = anchor_intent(store)
        document = store.commit_anchor(intent_id, EPOCH, moment(1))
        with pytest.raises(BoundaryError) as error:
            store.fail_after_launch(intent_id, "поздняя попытка закрыть")
        assert error.value.code is BoundaryErrorCode.REGISTRY_UNAVAILABLE
        state = store.binding_state(binding())
        assert state is not None and state.supported
        # Документ остался и доступен для публикации.
        published = store.mark_published(intent_id)
        assert published.canonical == document.canonical


@pytest.mark.parametrize("state", ["intent", "next_bound"])
def test_failure_after_launch_requires_a_started_writer(tmp_path: Path, state: str) -> None:
    """Закрытие после запуска допустимо только со стадий с выданным RW (§8.4)."""
    with registry(tmp_path) as store:
        intent_id = anchor_begin(store) if state == "intent" else anchor_intent(store)
        with pytest.raises(BoundaryError) as error:
            store.fail_after_launch(intent_id, "неуместное закрытие")
        assert error.value.code is BoundaryErrorCode.REGISTRY_UNAVAILABLE
        current = store.intent(intent_id)
        assert current is not None and current.state == state
        binding_state = store.binding_state(binding())
        assert binding_state is not None and binding_state.supported
