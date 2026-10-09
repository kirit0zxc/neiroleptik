"""Настоящий процесс Low: запуск, трафик, корректная остановка и формат получившегося журнала."""

from __future__ import annotations

import base64
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from infra.runtime.sample_low_journal import StopSignalError, run_sample

SAMPLE = Path(__file__).resolve().parents[2] / "specs" / "samples" / "low-journal-v1.sample.jsonl"
FILE_NAME = re.compile(
    r"low-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}-\d{6}\.jsonl"
)
HEADER_KEYS = {
    "record", "schema", "journal_id", "journal_seq", "previous_journal_id", "instance_id",
    "deployment_id", "profile_revision", "opened_at", "first_seq",
}  # fmt: skip
EVENT_KEYS = {
    "record", "seq", "event", "timestamp", "connection_id", "protocol", "service", "peer",
    "payload_b64",
}  # fmt: skip
FOOTER_KEYS = {"record", "journal_id", "last_seq", "closed_at", "reason"}


def parse_time(value: str) -> datetime:
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z", value), value
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def is_uuid(value: object) -> bool:
    return isinstance(value, str) and str(UUID(value)) == value


def assert_valid_journal(rows: list[dict[str, Any]], *, closed: bool = True) -> None:
    """Проверяет один файл журнала по specs/low-journal-v1.md."""
    header, *rest = rows
    assert set(header) == HEADER_KEYS and header["record"] == "header"
    assert header["schema"] == "honeyforge-low-journal/1"
    assert is_uuid(header["journal_id"]) and is_uuid(header["instance_id"])
    assert is_uuid(header["deployment_id"])
    assert header["previous_journal_id"] is None or is_uuid(header["previous_journal_id"])
    assert type(header["journal_seq"]) is int and header["journal_seq"] >= 1
    assert type(header["profile_revision"]) is int and header["profile_revision"] >= 1
    assert type(header["first_seq"]) is int and header["first_seq"] >= 1
    opened = parse_time(header["opened_at"])

    footer = None
    if closed:
        *rest, footer = rest
    events = rest
    expected_seq = header["first_seq"]
    last_time = opened
    kinds: dict[str, list[str]] = {}
    for event in events:
        assert set(event) == EVENT_KEYS and event["record"] == "event"
        assert event["seq"] == expected_seq, "seq непрерывен внутри файла"
        expected_seq += 1
        assert event["event"] in {"connection", "request"}
        assert event["protocol"] in {"tcp", "http"}
        assert event["service"].keys() == {"protocol", "port"}
        assert event["service"]["protocol"] == event["protocol"]
        assert type(event["service"]["port"]) is int and 1 <= event["service"]["port"] <= 65535
        assert event["peer"] is None or (
            isinstance(event["peer"]["host"], str) and type(event["peer"]["port"]) is int
        )
        raw = base64.b64decode(event["payload_b64"], validate=True)
        assert event["event"] != "connection" or raw == b""
        assert is_uuid(event["connection_id"])
        moment = parse_time(event["timestamp"])
        assert moment >= last_time, "время не убывает"
        last_time = moment
        kinds.setdefault(event["connection_id"], []).append(event["event"])
    for sequence in kinds.values():
        assert sequence[0] == "connection" and sequence.count("connection") == 1
        assert sequence.count("request") <= 1

    if footer is not None:
        assert set(footer) == FOOTER_KEYS and footer["record"] == "footer"
        assert footer["journal_id"] == header["journal_id"]
        assert footer["last_seq"] == expected_seq - 1
        assert footer["reason"] in {"rotation", "shutdown"}
        assert parse_time(footer["closed_at"]) >= last_time


def read_rows(path: Path) -> list[dict[str, Any]]:
    import json

    raw = path.read_bytes()
    assert raw.endswith(b"\n") and b"\r" not in raw
    return [json.loads(line) for line in raw.decode("ascii").split("\n")[:-1]]


def test_real_low_process_serves_traffic_and_leaves_a_valid_journal(tmp_path: Path) -> None:
    try:
        result = run_sample(tmp_path / "journal")
    except StopSignalError as error:
        pytest.skip(f"в этой среде нельзя послать сигнал остановки: {error}")

    assert result.return_code == 0, result.stderr
    assert "honeyforge-low: запущен" in result.stderr
    [path] = result.files
    assert FILE_NAME.fullmatch(path.name)
    rows = read_rows(path)
    assert_valid_journal(rows)

    header, *events, footer = rows
    assert header["instance_id"] == result.instance_id
    assert header["deployment_id"] == result.deployment_id
    assert footer["reason"] == "shutdown"
    by_connection: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        by_connection.setdefault(event["connection_id"], []).append(event)
    assert len(by_connection) == result.connections == 7
    assert all(len(pair) == 2 for pair in by_connection.values())
    assert {(e["protocol"], e["service"]["port"]) for e in events} == {
        ("tcp", 2200),
        ("http", 8800),
    }
    payloads = [base64.b64decode(e["payload_b64"]) for e in events if e["event"] == "request"]
    assert b"GET / HTTP/1.1" in payloads[2]
    assert any(p.startswith(b"POST /login") for p in payloads)
    assert max(len(p) for p in payloads) == 2048, "лимит max_input_bytes соблюдён"


def test_the_sample_given_to_the_collector_is_valid() -> None:
    rows = read_rows(SAMPLE)

    assert_valid_journal(rows)
    assert len({e["connection_id"] for e in rows[1:-1]}) == 7


def test_validator_detects_a_broken_journal() -> None:
    rows = read_rows(SAMPLE)
    rows[3] = {**rows[3], "seq": 99}

    with pytest.raises(AssertionError):
        assert_valid_journal(rows)


def test_run_sample_refuses_a_non_empty_directory(tmp_path: Path) -> None:
    (tmp_path / "occupied").write_text("x")

    with pytest.raises(ValueError):
        run_sample(tmp_path)
