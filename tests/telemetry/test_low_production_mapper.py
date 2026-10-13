"""Принятый Low MVP: контрольные UUID, честный capture и повтор после ACK."""

import base64
from contextlib import closing
from dataclasses import replace

import pytest
from test_low_journal_reader_contract import JOURNAL, OTHER, binding, event, footer, header, line

from agent.telemetry.low_journal.conversion import LowRawConverter
from agent.telemetry.low_journal.durable import RawJournalRepository
from agent.telemetry.low_journal.models import JournalRecord
from agent.telemetry.low_journal.production import LowProductionMapper
from agent.telemetry.low_journal.reader import LowJournalReader
from agent.telemetry.schemas import Ack, compact_json
from agent.telemetry.spool import SpoolRepository

AGENT = "00000000-0000-4000-8000-000000000000"


def request(payload=b"", protocol="http", **changes):
    return event(
        event="request",
        protocol=protocol,
        service={"protocol": protocol, "port": 8080},
        payload_b64=base64.b64encode(payload).decode("ascii"),
        **changes,
    )


def record(row, offset=512, journal=JOURNAL):
    raw = line(row)
    return JournalRecord(row["record"], journal, offset, offset + len(raw), raw)


def test_golden_vectors_rotation_and_separate_connections():
    mapper = LowProductionMapper(AGENT)
    original = record(request())
    first = mapper.convert(binding(), original)
    assert str(first.event_id) == "1024f648-cd77-5b90-8834-6be84c3cfa50"
    assert str(first.session_id) == "d2756db2-741d-5edb-b9f6-b4b347ea754b"
    assert mapper.convert(binding(), original).canonical_json() == first.canonical_json()
    moved = mapper.convert(binding(), record(request(), offset=513))
    assert str(moved.event_id) == "8954694d-3908-5db0-8b3f-effbc2e1080a"
    rotated = mapper.convert(binding(), record(request(), journal=OTHER))
    assert rotated.event_id != first.event_id and rotated.session_id == first.session_id
    separate = mapper.convert(binding(), record(request(connection_id=OTHER)))
    assert separate.session_id != first.session_id
    assert first.source_seq == 512 and first.data["low_seq"] == "1"


@pytest.mark.parametrize(
    "payload", [b"", b"\x00\xff\xfe", "привет".encode(), b"x" * 2048, b"x" * 14000]
)
@pytest.mark.parametrize("protocol", ["tcp", "http"])
def test_capture_unknown_and_exact_data_budget(payload, protocol):
    result = LowProductionMapper(AGENT).convert(binding(), record(request(payload, protocol)))
    assert result.type == protocol + ".request"
    data = result.data
    prefix = base64.b64decode(data["payload_b64"])
    assert prefix == payload[: len(prefix)]
    assert data["captured_bytes"] == len(payload)
    assert data["capture_completeness"] == "unknown"
    assert data["mapper_truncated"] == (len(prefix) < len(payload))
    assert len(compact_json(data).encode()) <= 8192
    if len(prefix) < len(payload):
        larger = data | {"payload_b64": base64.b64encode(payload[: len(prefix) + 1]).decode()}
        assert len(compact_json(larger).encode()) > 8192


@pytest.mark.parametrize(
    ("payload", "state", "method"),
    [
        (b"GET /secret?token=a HTTP/1.1\r\nHost: test\r\n\r\n", "parsed", "GET"),
        (b"POST / HTTP/1.0\r\n", "incomplete", "POST"),
        (b"GET / HTTP/1.1\r\ninvalid\r\n\r\n", "invalid", "GET"),
        (b"BOGUS / HTTP/1.1\r\n\r\n", "invalid", None),
        (b"get / HTTP/1.1\r\n\r\n", "invalid", None),
        (b"GET / HTTP/1.1", "incomplete", None),
        (b"X" * 1025, "limited", None),
        (b"GET " + b"/" * 1011 + b" HTTP/1.1\r\n\r\n", "parsed", "GET"),
        (b"GET " + b"/" * 1012 + b" HTTP/1.1\r\n\r\n", "limited", None),
        (b"GET / HTTP/1.1\r\n" + b"A: b\r\n" * 32 + b"\r\n", "parsed", "GET"),
        (b"GET / HTTP/1.1\r\n" + b"A: b\r\n" * 33 + b"\r\n", "limited", "GET"),
    ],
)
def test_http_bounded_parse_without_invented_method_or_response(payload, state, method):
    data = LowProductionMapper(AGENT).convert(binding(), record(request(payload))).data
    assert data["http_parse_state"] == state
    assert data.get("method") == method
    assert "status" not in data and "response" not in data
    assert data.get("path") in {None, "/", "[скрыто]"}


def test_null_peer_service_records_and_invalid_shape():
    mapper = LowProductionMapper(AGENT)
    assert mapper.convert(binding(), record(request(peer=None))).source is None
    assert mapper.convert(binding(), record(header())) is None
    assert mapper.convert(binding(), record(footer())) is None
    with pytest.raises(ValueError):
        mapper.convert(binding(), record(request() | {"unexpected": True}))
    with pytest.raises(ValueError):
        mapper.convert(binding(), replace(record(request()), kind="footer"))


def test_http_total_budget_and_large_process_sequence():
    mapper = LowProductionMapper(AGENT)
    prefix = b"GET / HTTP/1.1\r\n" + (b"X:" + b"x" * 1022 + b"\r\n") * 3
    exact = prefix + b"X:" + b"x" * 996 + b"\r\n\r\n"
    assert len(exact) == 4096
    parsed = mapper.convert(binding(), record(request(exact, seq=10**25)))
    assert parsed.data["http_parse_state"] == "parsed"
    assert parsed.data["low_seq"] == "10000000000000000000000000"
    limited = mapper.convert(binding(), record(request(exact[:-4] + b"x\r\n\r\n")))
    assert limited.data["http_parse_state"] == "limited"


def test_durable_conversion_after_ack_and_renamed_source(tmp_path):
    path = tmp_path / "source.jsonl"
    path.write_bytes(line(header()) + line(request(b"abc", "tcp")) + line(footer()))
    db = tmp_path / "spool.db"
    mapper = LowProductionMapper(AGENT)
    with (
        closing(RawJournalRepository(db, AGENT)) as raw,
        closing(SpoolRepository(db, AGENT)) as spool,
    ):
        with LowJournalReader().open(path, binding()) as stream:
            records = list(stream)
        for item in records:
            raw.append(binding(), item)
        converter = LowRawConverter(spool, mapper, mapper.version)
        assert converter.step(binding()).records == 3
        batch = spool.next_batch()
        assert len(batch.events) == 1 and batch.events[0].type == "tcp.request"
        spool.acknowledge(
            batch,
            Ack(batch_id=batch.batch_id, accepted_ids=[batch.events[0].event_id], rejected=[]),
        )
        renamed = tmp_path / "renamed.jsonl"
        path.rename(renamed)
        with LowJournalReader().open(renamed, binding()) as stream:
            for item in stream:
                assert converter.convert(binding(), item) == "repeated"
        assert spool.db.execute("SELECT count(*) FROM pending").fetchone()[0] == 0
        assert raw.db.execute("SELECT count(*) FROM low_raw_records").fetchone()[0] == 3
