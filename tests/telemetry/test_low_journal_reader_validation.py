"""Матрица формы всех полей исходного формата, без Event-ограничений."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest
from test_low_journal_reader_contract import (
    JOURNAL,
    OTHER,
    event,
    footer,
    header,
    line,
    read,
    source,
)

from agent.telemetry.low_journal import JournalReadError

ROWS = {"header": header(), "event": event(), "footer": footer(last_seq=0)}
FIELDS = [(kind, field) for kind, row in ROWS.items() for field in row]


def verify_invalid(tmp_path: Path, kind: str, row: dict[str, Any]) -> None:
    prefix = [] if kind == "header" else [header()]
    path = source(tmp_path, *prefix, row)
    with pytest.raises(JournalReadError) as caught:
        read(path)
    assert caught.value.code == "INVALID_RECORD"
    assert caught.value.offset == (0 if kind == "header" else len(line(header())))


@pytest.mark.parametrize("kind,field", FIELDS)
@pytest.mark.parametrize("mutation", ["missing", "type"])
def test_each_required_field(tmp_path: Path, kind: str, field: str, mutation: str) -> None:
    row = dict(ROWS[kind])
    if mutation == "missing":
        del row[field]
    else:
        row[field] = []
    verify_invalid(tmp_path, kind, row)


@pytest.mark.parametrize("kind", ROWS)
def test_unknown_field_in_each_kind(tmp_path: Path, kind: str) -> None:
    verify_invalid(tmp_path, kind, {**ROWS[kind], "extra": "fixture-only"})


@pytest.mark.parametrize(
    "kind,field",
    [
        ("header", "journal_seq"),
        ("header", "first_seq"),
        ("header", "profile_revision"),
        ("event", "seq"),
        ("footer", "last_seq"),
    ],
)
@pytest.mark.parametrize("value", [True, False, -1, 1.5, "1"])
def test_all_integer_fields_strict(tmp_path: Path, kind: str, field: str, value: Any) -> None:
    verify_invalid(tmp_path, kind, {**ROWS[kind], field: value})


@pytest.mark.parametrize(
    "object_name,field",
    [("service", "protocol"), ("service", "port"), ("peer", "host"), ("peer", "port")],
)
@pytest.mark.parametrize("mutation", ["missing", "extra", "type"])
def test_nested_fields_exact(tmp_path: Path, object_name: str, field: str, mutation: str) -> None:
    row = event()
    nested = dict(row[object_name])
    if mutation == "missing":
        del nested[field]
    elif mutation == "extra":
        nested["extra"] = 1
    else:
        nested[field] = []
    row[object_name] = nested
    verify_invalid(tmp_path, "event", row)


@pytest.mark.parametrize(
    "name,value",
    [("service", None), ("service", []), ("peer", []), ("service", "http"), ("peer", "::1")],
)
def test_nested_object_types(tmp_path: Path, name: str, value: Any) -> None:
    verify_invalid(tmp_path, "event", event(**{name: value}))


@pytest.mark.parametrize("name", ["service", "peer"])
@pytest.mark.parametrize("port", [True, False, -1, 65536, 1.5, "1"])
def test_port_types_and_ranges(tmp_path: Path, name: str, port: Any) -> None:
    row = event()
    row[name] = {**row[name], "port": port}
    verify_invalid(tmp_path, "event", row)


@pytest.mark.parametrize("host", ["::1", "2001:db8::1", "127.0.0.1", "192.0.2.1"])
@pytest.mark.parametrize("port", [0, 65535])
def test_valid_peer_addresses_preserved(tmp_path: Path, host: str, port: int) -> None:
    row = event(peer={"host": host, "port": port})
    records, _ = read(source(tmp_path, header(), row, footer()))
    assert records[1].document() == row


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-10-11T00:00:00+00:00",
        "2026-10-11t00:00:00z",
        "2026-10-11T24:00:00Z",
        "2026-10-11T00:00:60Z",
        "0000-01-01T00:00:00Z",
        "2026-10-11T00:00:00.0000000Z",
    ],
)
@pytest.mark.parametrize(
    "kind,field", [("header", "opened_at"), ("event", "timestamp"), ("footer", "closed_at")]
)
def test_timestamp_in_every_kind(tmp_path: Path, kind: str, field: str, timestamp: str) -> None:
    verify_invalid(tmp_path, kind, {**ROWS[kind], field: timestamp})


@pytest.mark.parametrize("raw", [b"\r\n", b"\xef\xbb\xbf", b"\xc3\xa9", b"\xff"])
def test_physical_encoding_strict(tmp_path: Path, raw: bytes) -> None:
    original = line(header())
    bad = original[:-1] + raw + b"\n"
    with pytest.raises(JournalReadError, match="INVALID_RECORD"):
        read(source(tmp_path, bad))


def test_escaped_unicode_decodes_then_validates(tmp_path: Path) -> None:
    raw = line(event()).replace(b'"http"', b'"h\\u0074tp"')
    records, _ = read(source(tmp_path, header(), raw, footer()))
    assert records[1].document() == event()
    assert records[1].raw == raw


def test_deep_json_has_bounded_safe_error(tmp_path: Path) -> None:
    bad = b'{"extra":' + b"[" * 2000 + b"0" + b"]" * 2000 + b"}\n"
    with pytest.raises(JournalReadError, match="INVALID_RECORD"):
        read(source(tmp_path, header(), bad))


@pytest.mark.parametrize("payload", ["YQ=", "YQ===", "YR==", "YQ==\n", "YQ==!", "====", "AA"])
def test_strict_request_base64(tmp_path: Path, payload: str) -> None:
    verify_invalid(tmp_path, "event", event(event="request", payload_b64=payload))


def test_request_larger_than_event_limit_preserved(tmp_path: Path) -> None:
    payload = base64.b64encode(b"\xff" * 12000).decode("ascii")
    row = event(event="request", payload_b64=payload)
    records, _ = read(source(tmp_path, header(), row, footer()))
    assert records[1].document() == row


def test_later_header_can_have_unknown_predecessor(tmp_path: Path) -> None:
    records, _ = read(source(tmp_path, header(journal_seq=5, previous_journal_id=None)))
    assert records[0].document()["previous_journal_id"] is None


def test_first_header_cannot_claim_predecessor(tmp_path: Path) -> None:
    verify_invalid(tmp_path, "header", header(previous_journal_id=OTHER))


@pytest.mark.parametrize(
    "kind,field",
    [
        ("header", "journal_id"),
        ("header", "instance_id"),
        ("header", "deployment_id"),
        ("header", "previous_journal_id"),
        ("event", "connection_id"),
        ("footer", "journal_id"),
    ],
)
@pytest.mark.parametrize("value", ["INVALID", JOURNAL.replace("-", ""), " " + JOURNAL])
def test_canonical_uuid_required(tmp_path: Path, kind: str, field: str, value: str) -> None:
    verify_invalid(tmp_path, kind, {**ROWS[kind], field: value})
