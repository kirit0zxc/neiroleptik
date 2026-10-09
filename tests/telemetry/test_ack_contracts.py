"""Дополнительные проверки границы ACK, фикстур и неизменности доказательств."""

import json
import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError as SchemaError
from pydantic import ValidationError

from agent.telemetry.adapters.low import LowAdapter
from agent.telemetry.export_contracts import (
    AGENT_ID,
    DEPLOYMENT_ID,
    INSTANCE_ID,
    build_examples,
    example_line,
)
from agent.telemetry.schemas import Ack, Batch, Event
from agent.telemetry.spool import SpoolRepository


@pytest.fixture
def saved(tmp_path: Path) -> Iterator[tuple[SpoolRepository, Batch]]:
    repository = SpoolRepository(tmp_path / "spool.sqlite3", AGENT_ID)
    examples = build_examples()
    batch = Batch.model_validate(examples["fixtures/telemetry/batch-mixed.json"])
    save_events(repository, batch.events)
    issued = repository.next_batch()
    assert issued is not None
    try:
        yield repository, issued
    finally:
        repository.close()


def save_events(repository: SpoolRepository, events: list[Event]) -> None:
    """Сохраняем содержимое фикстуры, а batch_id выдаёт сам spool со снимком."""
    adapter = LowAdapter(AGENT_ID, DEPLOYMENT_ID, INSTANCE_ID, 1)
    for event in events:
        line = (
            json.dumps(
                {
                    key: value
                    for key, value in event.model_dump(mode="json").items()
                    if key in {"occurred_at", "type", "source", "service", "data"}
                }
            )
            + "\n"
        ).encode()
        record = adapter.parse_line(
            line, source_id="contracts:g1", offset=repository.checkpoint("contracts:g1")
        )
        # Сохраняем события из фикстуры через тот же атомарный append, не прямым SQL.
        repository.append(replace(record, event=event))


def fixture_ack(batch: Batch, name: str = "ack-mixed") -> Ack:
    """Пример сохраняет результаты, заменяется только случайный ID выданной пачки."""
    value = build_examples()[f"fixtures/telemetry/{name}.json"]
    assert isinstance(value, dict)
    return Ack.model_validate(value | {"batch_id": batch.batch_id})


def response(batch: Batch) -> Ack:
    return Ack(
        batch_id=batch.batch_id, accepted_ids=[e.event_id for e in batch.events], rejected=[]
    )


@pytest.mark.parametrize(
    "kind", ["accepted_duplicate", "index", "retryable", "event_nan", "batch_duplicate"]
)
def test_nested_mutation_is_revalidated_without_changes(saved, kind: str) -> None:
    spool, batch = saved
    original = spool.next_batch().events
    ack = response(batch)
    if kind == "accepted_duplicate":
        ack.accepted_ids.append(ack.accepted_ids[0])
    elif kind in {"index", "retryable"}:
        ack = fixture_ack(batch)
        field = "index" if kind == "index" else "retryable"
        value = True if kind == "index" else "false"
        ack.rejected[0] = ack.rejected[0].model_copy(update={field: value})
    elif kind == "event_nan":
        batch.events[0].data["invalid_number"] = float("nan")
    else:
        batch.events.append(batch.events[0])
    with pytest.raises(ValueError) as error:
        spool.acknowledge(batch, ack)
    assert "invalid_number" not in str(error.value)
    assert spool.next_batch().events == original
    assert spool.quarantine_count() == 0


def test_quarantine_insert_failure_rolls_back_accepted_deletion(saved) -> None:
    spool, batch = saved
    spool.db.execute(
        "CREATE TRIGGER fail_quarantine BEFORE INSERT ON quarantine "
        "BEGIN SELECT RAISE(ABORT, 'simulated quarantine failure'); END"
    )
    ack = fixture_ack(batch)
    with pytest.raises(sqlite3.DatabaseError):
        spool.acknowledge(batch, ack)
    assert spool.next_batch().events == batch.events
    assert spool.quarantine_count() == 0


def test_quarantine_listing_cannot_mutate_saved_proof(saved) -> None:
    spool, batch = saved
    ack = fixture_ack(batch)
    spool.acknowledge(batch, ack)
    entry = spool.list_quarantine()[0]
    original = entry.event.canonical_json()
    entry.event.data["path"] = "/changed"
    entry.event.artifact_ids.clear()
    assert spool.list_quarantine()[0].event.canonical_json() == original
    assert spool.list_quarantine(limit=500)[0].rejected_at.tzname() == "UTC"


def test_changed_retryable_snapshot_invalidates_entire_ack(saved) -> None:
    spool, batch = saved
    ack = fixture_ack(batch)
    batch.events[2].data["path"] = "/changed"
    with pytest.raises(ValueError):
        spool.acknowledge(batch, ack)
    assert spool.count() == 3
    assert spool.quarantine_count() == 0


def test_ack_fixtures_validate_and_match_expected_queue_results(saved) -> None:
    spool, batch = saved
    examples = build_examples()
    schema = examples["agent-ack-v1.schema.json"]
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    for name, value in examples.items():
        if name.startswith("fixtures/telemetry/ack"):
            validator.validate(value)
            Ack.model_validate(value)
    ack = fixture_ack(batch)
    assert spool.acknowledge(batch, ack) == 1
    assert spool.next_batch().events == [batch.events[2]]
    assert spool.list_quarantine()[0].event == batch.events[1]


@pytest.mark.parametrize(
    "fixture", ["ack", "ack-permanent", "ack-invalid-event-id", "ack-unknown-event-id"]
)
def test_single_event_ack_fixtures_are_applied_by_original_position(
    tmp_path: Path, fixture: str
) -> None:
    examples = build_examples()
    batch = Batch.model_validate(examples["fixtures/telemetry/batch-mixed.json"])
    spool = SpoolRepository(tmp_path / "single.sqlite3", AGENT_ID)
    try:
        save_events(spool, batch.events[:1])
        single = spool.next_batch()
        assert single is not None
        save_events(spool, batch.events[1:])
        ack = fixture_ack(single, fixture)
        if fixture == "ack-unknown-event-id":
            with pytest.raises(ValueError):
                spool.acknowledge(single, ack)
            assert spool.count() == 3
            assert spool.quarantine_count() == 0
        else:
            assert spool.acknowledge(single, ack) == (1 if fixture == "ack" else 0)
            assert spool.next_batch().events == batch.events[1:]
            assert spool.quarantine_count() == (0 if fixture == "ack" else 1)
    finally:
        spool.close()


@pytest.mark.parametrize(
    "change", [{"retryable": True}, {"event_id": None}, {"index": True}, {"code": "UNKNOWN"}]
)
def test_json_schema_rejects_bad_rejection_combinations(change: dict) -> None:
    examples = build_examples()
    value = Ack.model_validate(examples["fixtures/telemetry/ack-mixed.json"]).model_dump(
        mode="json"
    )
    value["rejected"][0].update(change)
    with pytest.raises(SchemaError):
        Draft202012Validator(examples["agent-ack-v1.schema.json"]).validate(value)


@pytest.mark.parametrize(
    "timestamp", ["2026-10-09T12:00:00.1234567Z", "2026-10-09T15:00:00.1234560+03:00"]
)
def test_timestamp_precision_cannot_be_silently_lost(timestamp: str) -> None:
    event = Event.model_validate(build_examples()["fixtures/telemetry/agent-event.json"])
    with pytest.raises(ValidationError):
        Event.model_validate(event.model_dump(mode="json") | {"occurred_at": timestamp})


def test_quarantine_default_limit_and_original_order(tmp_path: Path) -> None:
    spool = SpoolRepository(tmp_path / "many.sqlite3", AGENT_ID)
    adapter = LowAdapter(AGENT_ID, DEPLOYMENT_ID, INSTANCE_ID, 1)
    originals = []
    try:
        for _ in range(101):
            record = adapter.parse_line(
                example_line(), source_id="many:g1", offset=spool.checkpoint("many:g1")
            )
            spool.append(record)
            originals.append(record.event.event_id)
        while batch := spool.next_batch():
            ack = Ack.model_validate(
                {
                    "batch_id": batch.batch_id,
                    "accepted_ids": [],
                    "rejected": [
                        {
                            "index": index,
                            "event_id": event.event_id,
                            "code": "INVALID_EVENT",
                            "retryable": False,
                        }
                        for index, event in enumerate(batch.events)
                    ],
                }
            )
            spool.acknowledge(batch, ack)
        assert spool.count() == 0
        assert spool.quarantine_count() == 101
        assert [row.event.event_id for row in spool.list_quarantine()] == originals[:100]
        assert [row.event.event_id for row in spool.list_quarantine(limit=500)] == originals
    finally:
        spool.close()


def test_late_different_reason_preserves_first_proof_after_restart(saved, tmp_path: Path) -> None:
    spool, batch = saved
    ack = fixture_ack(batch)
    spool.acknowledge(batch, ack)
    first = spool.list_quarantine()[0]
    spool.close()
    reopened = SpoolRepository(tmp_path / "spool.sqlite3", AGENT_ID)
    try:
        ack.rejected[0] = ack.rejected[0].model_copy(update={"code": "INVALID_EVENT"})
        assert reopened.acknowledge(batch, ack) == 0
        assert reopened.list_quarantine() == [first]
        assert reopened.next_batch().events == [batch.events[2]]
    finally:
        reopened.close()
