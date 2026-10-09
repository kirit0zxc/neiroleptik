"""Приёмочные тесты проекта ACK/карантина; текущая реализация ещё не полна."""

import sqlite3
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agent.telemetry.adapters.low import LowAdapter
from agent.telemetry.export_contracts import AGENT_ID, DEPLOYMENT_ID, INSTANCE_ID, example_line
from agent.telemetry.schemas import Ack, Batch, Event, RejectedEvent
from agent.telemetry.spool import SpoolRepository


@pytest.fixture
def spool(tmp_path: Path) -> Iterator[SpoolRepository]:
    repository = SpoolRepository(tmp_path / "spool.sqlite3", AGENT_ID)
    try:
        yield repository
    finally:
        repository.close()


def enqueue(spool: SpoolRepository, count: int = 3) -> Batch:
    """Создаём реальные SQLite-записи через существующий публичный API."""
    adapter = LowAdapter(AGENT_ID, DEPLOYMENT_ID, INSTANCE_ID, 1)
    for _ in range(count):
        record = adapter.parse_line(
            example_line(), source_id="test:g1", offset=spool.checkpoint("test:g1")
        )
        spool.append(record)
    batch = spool.next_batch()
    assert batch is not None
    return batch


def rejected(batch: Batch, index: int, *, retryable: bool = False) -> dict:
    return {
        "index": index,
        "event_id": str(batch.events[index].event_id),
        "code": "TEMPORARILY_UNAVAILABLE" if retryable else "EVENT_ID_CONFLICT",
        "retryable": retryable,
    }


def ack(batch: Batch, accepted: tuple[int, ...] = (), errors: list | None = None) -> Ack:
    return Ack.model_validate(
        {
            "batch_id": str(batch.batch_id),
            "accepted_ids": [str(batch.events[i].event_id) for i in accepted],
            "rejected": errors or [],
        }
    )


def pending_ids(spool: SpoolRepository) -> list:
    batch = spool.next_batch()
    return [] if batch is None else [event.event_id for event in batch.events]


def test_valid_draft_rejection_roundtrips() -> None:
    payload = {
        "index": 0,
        "event_id": str(uuid4()),
        "code": "EVENT_ID_CONFLICT",
        "retryable": False,
    }
    assert RejectedEvent.model_validate(payload).model_dump(mode="json") == payload


def test_accepted_ack_is_idempotent_and_does_not_move_checkpoint(spool) -> None:
    batch = enqueue(spool)
    offset = spool.checkpoint("test:g1")
    response = ack(batch, (0, 1, 2))
    assert spool.acknowledge(batch, response) == 3
    assert spool.acknowledge(batch, response) == 0
    assert spool.count() == 0
    assert spool.checkpoint("test:g1") == offset


def test_lost_ack_does_not_remove_or_change_events(spool) -> None:
    batch = enqueue(spool)
    retry = spool.next_batch()
    assert retry is not None
    assert retry.events == batch.events
    assert spool.count() == 3


def test_explicit_retryable_results_remain_in_order(spool) -> None:
    batch = enqueue(spool)
    response = ack(
        batch, (0,), [rejected(batch, 1, retryable=True), rejected(batch, 2, retryable=True)]
    )
    assert spool.acknowledge(batch, response) == 1
    assert pending_ids(spool) == [event.event_id for event in batch.events[1:]]
    assert spool.quarantine_count() == 0


def test_permanent_rejection_preserves_original_after_restart(spool, tmp_path) -> None:
    batch = enqueue(spool, 2)
    original = batch.events[0].canonical_json()
    offset = spool.checkpoint("test:g1")
    response = ack(batch, errors=[rejected(batch, 0), rejected(batch, 1, retryable=True)])
    assert spool.acknowledge(batch, response) == 0
    assert pending_ids(spool) == [batch.events[1].event_id]
    spool.close()
    reopened = SpoolRepository(tmp_path / "spool.sqlite3", AGENT_ID)
    try:
        assert reopened.quarantine_count() == 1
        rows = reopened.list_quarantine()
        assert len(rows) == 1
        entry = rows[0]
        assert entry.event.canonical_json() == original
        assert entry.code == "EVENT_ID_CONFLICT"
        assert entry.batch_id == batch.batch_id
        assert entry.index == 0
        assert entry.rejected_at.utcoffset() == timedelta(0)
        assert reopened.checkpoint("test:g1") == offset
        assert pending_ids(reopened) == [batch.events[1].event_id]
    finally:
        reopened.close()


def test_replayed_ack_does_not_duplicate_or_overwrite_quarantine(spool) -> None:
    batch = enqueue(spool, 1)
    response = ack(batch, errors=[rejected(batch, 0)])
    spool.acknowledge(batch, response)
    first = spool.list_quarantine()[0]
    spool.acknowledge(batch, response)
    second = spool.list_quarantine()[0]
    assert spool.quarantine_count() == 1
    assert second.rejected_at == first.rejected_at
    assert second.event == first.event
    # Поздний положительный ответ не является разрешением стереть доказательство отказа.
    assert spool.acknowledge(batch, ack(batch, (0,))) == 0
    assert spool.quarantine_count() == 1


def test_complete_mixed_ack_leaves_only_retryable(spool) -> None:
    batch = enqueue(spool, 4)
    response = ack(
        batch,
        (0,),
        [
            rejected(batch, 1),
            rejected(batch, 2, retryable=True),
            rejected(batch, 3, retryable=True),
        ],
    )
    assert spool.acknowledge(batch, response) == 1
    assert pending_ids(spool) == [batch.events[2].event_id, batch.events[3].event_id]
    assert [entry.event.event_id for entry in spool.list_quarantine()] == [batch.events[1].event_id]


def test_null_event_id_is_resolved_by_index(spool) -> None:
    batch = enqueue(spool, 2)
    error = rejected(batch, 0) | {"event_id": None, "code": "INVALID_EVENT"}
    spool.acknowledge(batch, ack(batch, errors=[error, rejected(batch, 1, retryable=True)]))
    assert pending_ids(spool) == [batch.events[1].event_id]
    assert spool.list_quarantine()[0].event.event_id == batch.events[0].event_id


@pytest.mark.parametrize(
    "mutation",
    [
        {"index": -1},
        {"index": True},
        {"index": "0"},
        {"retryable": "false"},
        {"retryable": 0},
        {"code": "UNKNOWN_BACKEND_CODE"},
        {"retryable": True},
        {"event_id": None},
        {"unexpected": "field"},
    ],
)
def test_rejection_rejects_invalid_types_and_code_combinations(mutation) -> None:
    payload = {
        "index": 0,
        "event_id": str(uuid4()),
        "code": "EVENT_ID_CONFLICT",
        "retryable": False,
    }
    with pytest.raises(ValidationError):
        RejectedEvent.model_validate(payload | mutation)


@pytest.mark.parametrize("kind", ["index_outside", "id_mismatch", "accepted_overlap_null"])
def test_invalid_reference_rolls_back_even_valid_accepted_id(spool, kind) -> None:
    batch = enqueue(spool, 3)
    error = rejected(batch, 1)
    if kind == "index_outside":
        error["index"] = 99
    elif kind == "id_mismatch":
        error["event_id"] = str(batch.events[2].event_id)
    else:
        error = error | {"index": 0, "event_id": None, "code": "INVALID_EVENT"}
    response = ack(batch, (0, 2), [error])
    with pytest.raises(ValueError):
        spool.acknowledge(batch, response)
    assert pending_ids(spool) == [event.event_id for event in batch.events]
    assert spool.quarantine_count() == 0


@pytest.mark.parametrize("kind", ["accepted", "rejected_index", "rejected_id", "overlap"])
def test_duplicates_and_contradictions_are_rejected(spool, kind) -> None:
    batch = enqueue(spool, 3)
    payload = ack(batch).model_dump(mode="json")
    if kind == "accepted":
        payload["accepted_ids"] = [str(batch.events[0].event_id)] * 2
    elif kind == "rejected_index":
        payload["rejected"] = [rejected(batch, 0), rejected(batch, 1) | {"index": 0}]
    elif kind == "rejected_id":
        payload["rejected"] = [
            rejected(batch, 0),
            rejected(batch, 1) | {"event_id": str(batch.events[0].event_id)},
        ]
    else:
        payload["accepted_ids"] = [str(batch.events[0].event_id)]
        payload["rejected"] = [rejected(batch, 0)]
    with pytest.raises(ValueError):
        spool.acknowledge(batch, Ack.model_validate(payload))
    assert pending_ids(spool) == [event.event_id for event in batch.events]


def test_foreign_batch_and_event_do_not_delete(spool) -> None:
    batch = enqueue(spool, 1)
    for payload in [
        {"batch_id": str(uuid4()), "accepted_ids": [str(batch.events[0].event_id)], "rejected": []},
        {"batch_id": str(batch.batch_id), "accepted_ids": [str(uuid4())], "rejected": []},
    ]:
        with pytest.raises(ValueError):
            spool.acknowledge(batch, Ack.model_validate(payload))
    assert spool.count() == 1


def test_batch_from_another_agent_does_not_delete(spool) -> None:
    batch = enqueue(spool, 1)
    event = Event.model_validate(
        batch.events[0].model_dump(mode="json") | {"agent_id": str(uuid4())}
    )
    foreign = Batch(batch_id=batch.batch_id, events=[event])
    with pytest.raises(ValueError):
        spool.acknowledge(foreign, ack(foreign, (0,)))
    assert spool.count() == 1


def test_modified_batch_payload_cannot_remove_saved_original(spool) -> None:
    batch = enqueue(spool, 1)
    original = batch.events[0].canonical_json()
    batch.events[0].data["path"] = "/changed-after-send"
    with pytest.raises(ValueError):
        spool.acknowledge(batch, ack(batch, (0,)))
    assert spool.count() == 1
    assert spool.next_batch().events[0].canonical_json() == original


def test_sqlite_failure_rolls_back_all_ack_actions(spool) -> None:
    batch = enqueue(spool, 2)
    response = ack(batch, (0,), [rejected(batch, 1)])
    # Точка отказа в существующей pending-таблице не навязывает схему карантина.
    event_id = str(batch.events[1].event_id)
    spool.db.execute(
        "CREATE TRIGGER fail_ack BEFORE DELETE ON pending "
        f"WHEN OLD.event_id = '{event_id}' "
        "BEGIN SELECT RAISE(ABORT, 'simulated delete failure'); END"
    )
    try:
        with pytest.raises(sqlite3.DatabaseError):
            spool.acknowledge(batch, response)
        assert pending_ids(spool) == [event.event_id for event in batch.events]
        assert spool.quarantine_count() == 0
    finally:
        spool.db.execute("DROP TRIGGER fail_ack")
    assert spool.acknowledge(batch, response) == 1
    assert spool.count() == 0
    assert spool.quarantine_count() == 1


def test_open_existing_spool_preserves_queue_and_offsets(tmp_path) -> None:
    path = tmp_path / "old.sqlite3"
    record = LowAdapter(AGENT_ID, DEPLOYMENT_ID, INSTANCE_ID, 1).parse_line(
        example_line(),
        source_id="old:g1",
        offset=0,
    )
    # Создаём старый формат независимо от нового конструктора.
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE checkpoints (source_id TEXT PRIMARY KEY, next_offset INTEGER NOT NULL);
            CREATE TABLE pending (
                queue_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE, payload TEXT NOT NULL
            );
        """)
        db.execute("INSERT INTO metadata VALUES ('agent_id', ?)", (AGENT_ID,))
        db.execute("INSERT INTO checkpoints VALUES (?, ?)", (record.source_id, record.next_offset))
        db.execute(
            "INSERT INTO pending(event_id,payload) VALUES (?,?)",
            (
                str(record.event.event_id),
                record.event.canonical_json(),
            ),
        )
    db.close()
    opened = SpoolRepository(path, AGENT_ID)
    try:
        assert opened.count() == 1
        assert opened.checkpoint("old:g1") == record.next_offset
        assert opened.next_batch().events == [record.event]
        assert opened.quarantine_count() == 0
    finally:
        opened.close()


def test_late_rejection_does_not_invent_quarantine_from_deleted_payload(spool) -> None:
    batch = enqueue(spool, 1)
    spool.acknowledge(batch, ack(batch, (0,)))
    spool.acknowledge(batch, ack(batch, errors=[rejected(batch, 0)]))
    assert spool.count() == 0
    assert spool.quarantine_count() == 0


def test_quarantine_listing_is_bounded_and_ordered(spool) -> None:
    batch = enqueue(spool, 3)
    for index in range(3):
        response = ack(batch, errors=[rejected(batch, i, retryable=i != index) for i in range(3)])
        spool.acknowledge(batch, response)
    assert spool.quarantine_count() == 3
    assert [row.event.event_id for row in spool.list_quarantine(limit=2)] == [
        batch.events[0].event_id,
        batch.events[1].event_id,
    ]
    for limit in [0, -1, 501, True, "2"]:
        with pytest.raises(ValueError):
            spool.list_quarantine(limit=limit)


def test_rejected_requires_index_even_when_event_id_is_valid() -> None:
    payload = {
        "event_id": str(uuid4()),
        "code": "EVENT_ID_CONFLICT",
        "retryable": False,
    }
    with pytest.raises(ValidationError):
        RejectedEvent.model_validate(payload)


@pytest.mark.parametrize("kind", ["empty", "accepted_only", "accepted_and_permanent"])
def test_incomplete_ack_never_partially_changes_queue(spool, kind) -> None:
    batch = enqueue(spool, 3)
    offset = spool.checkpoint("test:g1")
    if kind == "empty":
        response = ack(batch)
    elif kind == "accepted_only":
        response = ack(batch, (0,))
    else:
        response = ack(batch, (0,), [rejected(batch, 1)])
    with pytest.raises(ValueError):
        spool.acknowledge(batch, response)
    assert pending_ids(spool) == [event.event_id for event in batch.events]
    assert spool.quarantine_count() == 0
    assert spool.checkpoint("test:g1") == offset


def test_accepted_and_permanent_are_not_resent_after_restart(spool, tmp_path) -> None:
    batch = enqueue(spool, 3)
    response = ack(batch, (0,), [rejected(batch, 1), rejected(batch, 2, retryable=True)])
    spool.acknowledge(batch, response)
    spool.close()
    reopened = SpoolRepository(tmp_path / "spool.sqlite3", AGENT_ID)
    try:
        assert pending_ids(reopened) == [batch.events[2].event_id]
        assert [row.event.event_id for row in reopened.list_quarantine()] == [
            batch.events[1].event_id
        ]
    finally:
        reopened.close()
