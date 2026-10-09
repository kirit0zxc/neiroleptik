"""ACK проверяется по исходной пачке, а не по изменённому списку Batch.events."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest

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


def append(spool: SpoolRepository) -> Event:
    record = LowAdapter(AGENT_ID, DEPLOYMENT_ID, INSTANCE_ID, 1).parse_line(
        example_line(), source_id="snapshot:g1", offset=spool.checkpoint("snapshot:g1")
    )
    spool.append(record)
    return record.event


def accepted(batch: Batch) -> Ack:
    return Ack(
        batch_id=batch.batch_id, accepted_ids=[e.event_id for e in batch.events], rejected=[]
    )


def null_rejection(batch: Batch) -> Ack:
    return Ack(
        batch_id=batch.batch_id,
        accepted_ids=[],
        rejected=[RejectedEvent(index=0, event_id=None, code="INVALID_EVENT", retryable=False)],
    )


def assert_unchanged(spool: SpoolRepository, events: list[Event], checkpoint: int) -> None:
    current = spool.next_batch()
    assert current is not None
    assert current.events == events
    assert spool.quarantine_count() == 0
    assert spool.checkpoint("snapshot:g1") == checkpoint


@pytest.mark.parametrize("restart", [False, True])
def test_removing_event_cannot_turn_incomplete_ack_into_complete(
    spool: SpoolRepository, tmp_path: Path, restart: bool
) -> None:
    originals = [append(spool), append(spool)]
    batch = spool.next_batch()
    assert batch is not None
    checkpoint = spool.checkpoint("snapshot:g1")
    batch.events.pop()
    repository = spool
    if restart:
        spool.close()
        repository = SpoolRepository(tmp_path / "spool.sqlite3", AGENT_ID)
    try:
        with pytest.raises(ValueError):
            repository.acknowledge(batch, accepted(batch))
        assert_unchanged(repository, originals, checkpoint)
    finally:
        if restart:
            repository.close()


@pytest.mark.parametrize("restart", [False, True])
def test_substitution_cannot_quarantine_unsent_pending_event(
    spool: SpoolRepository, tmp_path: Path, restart: bool
) -> None:
    original = append(spool)
    batch = spool.next_batch()
    assert batch is not None
    other = append(spool)
    checkpoint = spool.checkpoint("snapshot:g1")
    batch.events[0] = other
    repository = spool
    if restart:
        spool.close()
        repository = SpoolRepository(tmp_path / "spool.sqlite3", AGENT_ID)
    try:
        with pytest.raises(ValueError):
            repository.acknowledge(batch, null_rejection(batch))
        assert_unchanged(repository, [original, other], checkpoint)
    finally:
        if restart:
            repository.close()


@pytest.mark.parametrize("mutation", ["reorder", "append", "new_model", "new_id"])
def test_batch_binding_covers_order_membership_and_id(
    spool: SpoolRepository, mutation: str
) -> None:
    originals = [append(spool), append(spool)]
    batch = spool.next_batch()
    assert batch is not None
    originals.append(append(spool))
    checkpoint = spool.checkpoint("snapshot:g1")
    if mutation == "reorder":
        batch.events.reverse()
    elif mutation == "append":
        batch.events.append(originals[2])
    elif mutation == "new_model":
        batch = Batch(batch_id=batch.batch_id, events=[originals[2]])
    else:
        batch = Batch(batch_id=uuid4(), events=batch.events)
    with pytest.raises(ValueError):
        spool.acknowledge(batch, accepted(batch))
    assert_unchanged(spool, originals, checkpoint)


def test_original_snapshot_accepts_roundtrip_and_replay_after_restart(
    spool: SpoolRepository, tmp_path: Path
) -> None:
    originals = [append(spool), append(spool)]
    batch = spool.next_batch()
    assert batch is not None
    # Снимок принадлежит batch_id, поэтому JSON roundtrip корректной пачки разрешён.
    restored = Batch.model_validate_json(batch.model_dump_json())
    spool.close()
    reopened = SpoolRepository(tmp_path / "spool.sqlite3", AGENT_ID)
    try:
        assert reopened.acknowledge(restored, accepted(restored)) == len(originals)
        assert reopened.acknowledge(restored, accepted(restored)) == 0
        assert reopened.count() == 0
        assert reopened.quarantine_count() == 0
    finally:
        reopened.close()


def test_deleted_event_payload_still_belongs_to_original_snapshot(spool: SpoolRepository) -> None:
    originals = [append(spool), append(spool)]
    batch = spool.next_batch()
    assert batch is not None
    checkpoint = spool.checkpoint("snapshot:g1")
    ack = Ack(
        batch_id=batch.batch_id,
        accepted_ids=[originals[0].event_id],
        rejected=[
            RejectedEvent(
                index=1,
                event_id=originals[1].event_id,
                code="TEMPORARILY_UNAVAILABLE",
                retryable=True,
            )
        ],
    )
    assert spool.acknowledge(batch, ack) == 1
    batch.events[0].data["path"] = "/changed-after-accepted"
    with pytest.raises(ValueError):
        spool.acknowledge(batch, ack)
    assert_unchanged(spool, originals[1:], checkpoint)


def test_snapshot_write_failure_does_not_issue_batch_or_change_pending(
    spool: SpoolRepository,
) -> None:
    original = append(spool)
    checkpoint = spool.checkpoint("snapshot:g1")
    spool.db.execute(
        "CREATE TRIGGER fail_snapshot BEFORE INSERT ON batch_snapshots "
        "BEGIN SELECT RAISE(ABORT, 'simulated snapshot failure'); END"
    )
    with pytest.raises(sqlite3.DatabaseError):
        spool.next_batch()
    assert spool.count() == 1
    assert spool.checkpoint("snapshot:g1") == checkpoint
    assert spool.quarantine_count() == 0
    spool.db.execute("DROP TRIGGER fail_snapshot")
    batch = spool.next_batch()
    assert batch is not None
    assert batch.events == [original]
    assert spool.acknowledge(batch, accepted(batch)) == 1
