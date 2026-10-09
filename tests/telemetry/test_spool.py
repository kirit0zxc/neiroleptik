"""Проверки сохранности данных и совместимости с контрактами центра."""

import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ValidationError

from agent.telemetry.adapters.low import LowAdapter
from agent.telemetry.export_contracts import build_examples
from agent.telemetry.schemas import Ack, Batch, Event, RejectedEvent
from agent.telemetry.spool import OffsetConflict, SpoolRepository

ROOT = Path(__file__).resolve().parents[2]
AGENT = "00000000-0000-4000-8000-000000000001"
DEPLOYMENT = "00000000-0000-4000-8000-000000000002"
INSTANCE = "00000000-0000-4000-8000-000000000003"


def low_line(data: dict | None = None) -> bytes:
    return (
        json.dumps(
            {
                "occurred_at": "2026-10-09T15:00:00+03:00",
                "type": "http.request",
                "source": {"ip": "192.0.2.23", "port": 51342},
                "service": {"protocol": "http", "port": 8080},
                "data": data
                if data is not None
                else {"method": "GET", "path": "/", "truncated": False},
            },
            ensure_ascii=False,
        )
        + "\n"
    ).encode()


class TelemetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "spool.sqlite3"
        self.spool = SpoolRepository(self.path, AGENT)
        self.addCleanup(lambda: self.spool.close())
        self.adapter = LowAdapter(AGENT, DEPLOYMENT, INSTANCE, 1)

    def append(self, count: int, data: dict | None = None) -> list[Event]:
        events = []
        for _ in range(count):
            record = self.adapter.parse_line(
                low_line(data),
                source_id="low:g1",
                offset=self.spool.checkpoint("low:g1"),
            )
            self.spool.append(record)
            events.append(record.event)
        return events

    def test_offline_100_survive_restart_and_lost_ack(self) -> None:
        events = self.append(100)
        checkpoint = self.spool.checkpoint("low:g1")
        batch = self.spool.next_batch()
        self.assertEqual(len(batch.events), 100)
        self.spool.close()
        self.spool = SpoolRepository(self.path, AGENT)
        retry = self.spool.next_batch()
        self.assertEqual(retry.events, events)
        self.assertEqual(self.spool.checkpoint("low:g1"), checkpoint)
        self.assertEqual(self.spool.count(), 100)
        self.assertEqual(self.spool.db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        ack = Ack(batch_id=retry.batch_id, accepted_ids=[e.event_id for e in events], rejected=[])
        self.assertEqual(self.spool.acknowledge(retry, ack), 100)
        self.assertEqual(self.spool.acknowledge(retry, ack), 0)
        self.assertIsNone(self.spool.next_batch())

    def test_conflict_rolls_back_offset_and_insert(self) -> None:
        record = self.adapter.parse_line(low_line(), source_id="low:g1", offset=0)
        self.spool.append(record)
        with self.assertRaises(OffsetConflict):
            self.spool.append(record)
        # Повтор event_id на другой позиции должен откатить checkpoint.
        with self.assertRaises(sqlite3.IntegrityError):
            self.spool.append(replace(record, offset=record.next_offset, next_offset=9999))
        self.assertEqual(self.spool.count(), 1)
        self.assertEqual(self.spool.checkpoint("low:g1"), record.next_offset)

    def test_complete_ack_keeps_explicit_retryable_rejections(self) -> None:
        events = self.append(3)
        batch = self.spool.next_batch()
        ack = Ack(
            batch_id=batch.batch_id,
            accepted_ids=[events[0].event_id],
            rejected=[
                RejectedEvent(
                    index=index,
                    event_id=events[index].event_id,
                    code="TEMPORARILY_UNAVAILABLE",
                    retryable=True,
                )
                for index in (1, 2)
            ],
        )
        self.spool.acknowledge(batch, ack)
        self.assertEqual(self.spool.next_batch().events, events[1:])
        self.assertEqual(self.spool.quarantine_count(), 0)

    def test_incomplete_ack_keeps_entire_queue_and_quarantine(self) -> None:
        events = self.append(3)
        batch = self.spool.next_batch()
        ack = Ack(
            batch_id=batch.batch_id,
            accepted_ids=[events[0].event_id],
            rejected=[
                RejectedEvent(
                    index=1,
                    event_id=events[1].event_id,
                    code="INVALID_EVENT",
                    retryable=False,
                ),
            ],
        )
        with self.assertRaises(ValueError):
            self.spool.acknowledge(batch, ack)
        self.assertEqual(self.spool.next_batch().events, events)
        self.assertEqual(self.spool.quarantine_count(), 0)

    def test_wrong_ack_cannot_remove_events(self) -> None:
        events = self.append(1)
        batch = self.spool.next_batch()
        for ack in [
            Ack(batch_id=uuid4(), accepted_ids=[events[0].event_id], rejected=[]),
            Ack(batch_id=batch.batch_id, accepted_ids=[uuid4()], rejected=[]),
        ]:
            with self.assertRaises(ValueError):
                self.spool.acknowledge(batch, ack)
        self.assertEqual(self.spool.count(), 1)
        with self.assertRaises(ValidationError):
            Ack(batch_id=batch.batch_id, accepted_ids=[events[0].event_id] * 2, rejected=[])

    def test_other_agent_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            SpoolRepository(self.path, str(uuid4()))
        adapter = LowAdapter(str(uuid4()), DEPLOYMENT, INSTANCE, 1)
        with self.assertRaises(ValueError):
            self.spool.append(adapter.parse_line(low_line(), source_id="low:g1", offset=0))
        self.assertEqual(self.spool.count(), 0)

    def test_same_position_stable_id_and_rotation_new_id(self) -> None:
        a = self.adapter.parse_line(low_line(), source_id="low:g1", offset=0)
        b = self.adapter.parse_line(low_line(), source_id="low:g1", offset=0)
        c = self.adapter.parse_line(low_line(), source_id="low:g2", offset=0)
        self.assertEqual(a.event.event_id, b.event.event_id)
        self.assertNotEqual(a.event.event_id, c.event.event_id)
        self.spool.append(a)
        self.spool.append(c)
        self.assertEqual(self.spool.count(), 2)

    def test_bad_input_does_not_advance(self) -> None:
        for line in [low_line()[:-1], b"[]\n", b"\xff\n", low_line() + low_line(), b"{bad}\n"]:
            with self.subTest(line=line), self.assertRaises(ValueError):
                self.adapter.parse_line(line, source_id="low:g1", offset=0)
        self.assertEqual(self.spool.checkpoint("low:g1"), 0)

    def test_batch_respects_count_and_bytes(self) -> None:
        self.append(110, {"text": "я" * 3900, "truncated": False})
        seen = []
        while batch := self.spool.next_batch():
            self.assertLessEqual(len(batch.events), 100)
            self.assertLessEqual(len(batch.to_bytes()), 256 * 1024)
            ids = [e.event_id for e in batch.events]
            seen.extend(ids)
            self.spool.acknowledge(
                batch, Ack(batch_id=batch.batch_id, accepted_ids=ids, rejected=[])
            )
        self.assertEqual(len(set(seen)), 110)

    def test_event_validation_and_utc(self) -> None:
        event = self.append(1)[0]
        self.assertEqual(event.occurred_at.isoformat(), "2026-10-09T12:00:00+00:00")
        payload = event.model_dump(mode="json")
        bad_values = [
            {"agent_id": "agent-01"},
            {"occurred_at": "2026-10-09T12:00:00"},
            {"occurred_at": 1791547200},
            {"source_seq": True},
            {"occurred_at": "0"},
            {"schema_version": True},
            {"profile_revision": 0},
            {"source_seq": 9007199254740992},
            {"received_at": "2026-10-09T12:00:00Z"},
            {"source": {"ip": "not-an-ip", "port": 1}},
            {"data": {"x": "я" * 4096}},
            {"data": {"x": float("nan")}},
            {"artifact_ids": [str(UUID(int=4)), str(UUID(int=4))]},
        ]
        for changed in bad_values:
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                Event.model_validate(payload | changed)

    def test_ui_schema_compatible_after_server_timestamp(self) -> None:
        event = self.append(1)[0].model_dump(mode="json")
        event["received_at"] = "2026-10-09T12:00:01Z"
        schema = json.loads((ROOT / "contracts/event-v1.schema.json").read_text(encoding="utf-8"))
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(event)

    def test_batch_rejects_duplicate_events(self) -> None:
        event = self.append(1)[0]
        with self.assertRaises(ValidationError):
            Batch(events=[event, event])

    def test_mutated_payload_is_revalidated_before_save(self) -> None:
        record = self.adapter.parse_line(low_line(), source_id="low:g1", offset=0)
        record.event.data["oversize"] = "x" * 9000
        with self.assertRaises(ValueError):
            self.spool.append(record)
        self.assertEqual(self.spool.count(), 0)
        self.assertEqual(self.spool.checkpoint("low:g1"), 0)

    def test_checkpoint_write_failure_rolls_back_event(self) -> None:
        self.spool.db.execute(
            "CREATE TRIGGER fail_checkpoint BEFORE INSERT ON checkpoints "
            "BEGIN SELECT RAISE(ABORT, 'simulated checkpoint failure'); END"
        )
        record = self.adapter.parse_line(low_line(), source_id="low:g1", offset=0)
        with self.assertRaises(sqlite3.IntegrityError):
            self.spool.append(record)
        self.assertEqual(self.spool.count(), 0)
        self.assertEqual(self.spool.checkpoint("low:g1"), 0)

    def test_two_readers_cannot_advance_same_position_twice(self) -> None:
        other = SpoolRepository(self.path, AGENT)
        self.addCleanup(other.close)
        record = self.adapter.parse_line(low_line(), source_id="low:g1", offset=0)
        self.spool.append(record)
        with self.assertRaises(OffsetConflict):
            other.append(record)
        self.assertEqual(other.count(), 1)

    def test_fixtures_follow_both_agent_and_frontend_schemas(self) -> None:
        examples = build_examples()
        for fixture, schema_file in [
            ("agent-event", "agent-event-v1.schema.json"),
            ("batch", "agent-batch-v1.schema.json"),
            ("ack", "agent-ack-v1.schema.json"),
            ("ui-event", "event-v1.schema.json"),
            ("replay-chunk", "replay-chunk-v1.schema.json"),
        ]:
            schema = json.loads((ROOT / "contracts" / schema_file).read_text(encoding="utf-8"))
            with self.subTest(fixture=fixture):
                Draft202012Validator(schema, format_checker=FormatChecker()).validate(
                    examples[f"fixtures/telemetry/{fixture}.json"]
                )


if __name__ == "__main__":
    unittest.main()
