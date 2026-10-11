"""Приёмочные тесты читателя по specs/low-journal-reader.md.

Отсутствующий модуль — ошибка, а не пропуск тестов. Дополнительная матрица
проверяется в test_low_journal_reader_files/validation/rotation.py.
"""

from __future__ import annotations

import base64
import json
import traceback
from pathlib import Path
from typing import Any

import pytest

from agent.telemetry.low_journal import JournalBinding, JournalReadError, LowJournalReader

DEPLOYMENT = "00000000-0000-4000-8000-000000000001"
INSTANCE = "00000000-0000-4000-8000-000000000002"
JOURNAL = "00000000-0000-4000-8000-000000000003"
CONNECTION = "00000000-0000-4000-8000-000000000004"
OTHER = "00000000-0000-4000-8000-000000000005"
NOW = "2026-10-11T00:00:00Z"


def binding() -> JournalBinding:
    """Привязка задаётся тестом независимо от содержимого файла."""
    return JournalBinding(deployment_id=DEPLOYMENT, instance_id=INSTANCE, profile_revision=1)


def header(**changes: Any) -> dict[str, Any]:
    row = {
        "record": "header",
        "schema": "honeyforge-low-journal/1",
        "journal_id": JOURNAL,
        "journal_seq": 1,
        "previous_journal_id": None,
        "instance_id": INSTANCE,
        "deployment_id": DEPLOYMENT,
        "profile_revision": 1,
        "opened_at": NOW,
        "first_seq": 1,
    }
    return {**row, **changes}


def event(**changes: Any) -> dict[str, Any]:
    row = {
        "record": "event",
        "seq": 1,
        "event": "connection",
        "timestamp": NOW,
        "connection_id": CONNECTION,
        "protocol": "http",
        "service": {"protocol": "http", "port": 8080},
        "peer": {"host": "127.0.0.1", "port": 45678},
        "payload_b64": "",
    }
    return {**row, **changes}


def footer(**changes: Any) -> dict[str, Any]:
    return {
        "record": "footer",
        "journal_id": JOURNAL,
        "last_seq": 1,
        "closed_at": NOW,
        "reason": "shutdown",
        **changes,
    }


def line(row: dict[str, Any]) -> bytes:
    """Ожидаемые bytes создаются без вызова кода читателя."""
    return (json.dumps(row, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")


def source(tmp_path: Path, *rows: dict[str, Any] | bytes) -> Path:
    path = tmp_path / "explicit-source.jsonl"
    path.write_bytes(b"".join(row if isinstance(row, bytes) else line(row) for row in rows))
    return path


def read(path: Path, expected: JournalBinding | None = None) -> tuple[list[Any], Any]:
    with LowJournalReader().open(path, expected or binding()) as stream:
        records = list(stream)
        return records, stream.summary


def assert_error(path: Path, code: str, offset: int | None = None) -> None:
    with pytest.raises(JournalReadError) as caught:
        read(path)
    assert caught.value.code == code
    if offset is not None:
        assert caught.value.offset == offset


def test_exact_source_bytes_positions_and_no_source_changes(tmp_path: Path) -> None:
    rows = [header(), event(), footer()]
    encoded = [line(row) for row in rows]
    path = source(tmp_path, *rows)
    names = sorted(p.name for p in tmp_path.iterdir())
    records, summary = read(path)
    assert [r.kind for r in records] == ["header", "event", "footer"]
    offset = 0
    for record, raw, expected in zip(records, encoded, rows, strict=True):
        assert record.offset == offset
        offset += len(raw)
        assert record.next_offset == offset
        assert record.raw == raw
        assert record.journal_id == JOURNAL
        assert record.document() == expected
    assert (summary.status, summary.next_offset, summary.journal_id) == ("closed", offset, JOURNAL)
    assert path.read_bytes() == b"".join(encoded)
    assert sorted(p.name for p in tmp_path.iterdir()) == names


def test_reopen_and_renamed_copy_preserve_positions(tmp_path: Path) -> None:
    path = source(tmp_path, header(), event(), footer())
    first, _ = read(path)
    copy = tmp_path / "archived-name.jsonl"
    copy.write_bytes(path.read_bytes())
    for candidate in (path, copy):
        repeated, _ = read(candidate)
        assert [(r.journal_id, r.offset, r.next_offset, r.raw) for r in repeated] == [
            (r.journal_id, r.offset, r.next_offset, r.raw) for r in first
        ]


@pytest.mark.parametrize("prefix", [b"", b'{"record":"header"'])
def test_incomplete_header_does_not_advance(tmp_path: Path, prefix: bytes) -> None:
    records, summary = read(source(tmp_path, prefix))
    assert records == []
    assert (summary.status, summary.next_offset, summary.journal_id) == ("incomplete", 0, None)


def test_header_without_events_is_open(tmp_path: Path) -> None:
    records, summary = read(source(tmp_path, header()))
    assert len(records) == 1
    assert summary.status == "open"
    assert summary.next_offset == len(line(header()))


def test_empty_closed_journal_uses_first_seq_minus_one(tmp_path: Path) -> None:
    records, summary = read(source(tmp_path, header(first_seq=8), footer(last_seq=7)))
    assert len(records) == 2
    assert summary.status == "closed"


def test_partial_tail_is_retried_after_newline_arrives(tmp_path: Path) -> None:
    path = source(tmp_path, header(), line(event())[:-1])
    first, summary = read(path)
    assert [r.kind for r in first] == ["header"]
    assert summary.status == "open"
    assert summary.next_offset == len(line(header()))
    with path.open("ab") as writer:
        writer.write(b"\n" + line(footer()))
    complete, summary = read(path)
    assert [r.kind for r in complete] == ["header", "event", "footer"]
    assert complete[1].offset == len(line(header()))
    assert summary.status == "closed"


def test_seq_gaps_earlier_time_and_cross_file_request_are_valid(tmp_path: Path) -> None:
    request = event(
        seq=3,
        event="request",
        timestamp="2026-10-10T23:59:00Z",
        payload_b64=base64.b64encode(b"GET / HTTP/1.1\r\n\r\n").decode("ascii"),
    )
    records, _ = read(source(tmp_path, header(), request, event(seq=7), footer(last_seq=7)))
    assert [r.document()["seq"] for r in records if r.kind == "event"] == [3, 7]
    assert records[1].document()["timestamp"] == request["timestamp"]
    assert records[1].document()["connection_id"] == CONNECTION


def test_null_peer_and_binary_tcp_payload_are_preserved(tmp_path: Path) -> None:
    row = event(
        event="request",
        protocol="tcp",
        service={"protocol": "tcp", "port": 2222},
        peer=None,
        payload_b64=base64.b64encode(b"\xff\x00\xfe").decode("ascii"),
    )
    records, _ = read(source(tmp_path, header(), row, footer()))
    assert records[1].document() == row


def test_document_returns_independent_nested_copies(tmp_path: Path) -> None:
    records, _ = read(source(tmp_path, header(), event(), footer()))
    returned = records[1].document()
    returned["service"]["port"] = 9999
    returned["peer"]["host"] = "192.0.2.1"
    assert records[1].document() == event()
    assert records[1].raw == line(event())


@pytest.mark.parametrize(
    "changes",
    [
        {"deployment_id": OTHER},
        {"instance_id": OTHER},
        {"profile_revision": 2},
    ],
)
def test_binding_mismatch_before_header_is_yielded(tmp_path: Path, changes: dict[str, Any]) -> None:
    path = source(tmp_path, header(**changes), event(), footer())
    with LowJournalReader().open(path, binding()) as stream:
        with pytest.raises(JournalReadError) as caught:
            next(iter(stream))
        assert caught.value.code == "IDENTITY_MISMATCH"
        with pytest.raises(RuntimeError):
            _ = stream.summary


def test_unknown_schema_is_explicit(tmp_path: Path) -> None:
    assert_error(
        source(tmp_path, header(schema="honeyforge-low-journal/2")), "UNSUPPORTED_SCHEMA", 0
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"seq": True},
        {"seq": "1"},
        {"seq": 0},
        {"unknown": "value"},
        {"connection_id": "invalid"},
        {"event": "shell.command"},
        {"timestamp": "2026-10-11T00:00:00"},
        {"timestamp": "2026-02-30T00:00:00Z"},
        {"timestamp": "2026-10-11T00:00:00.1234567Z"},
        {"service": {"protocol": "tcp", "port": 8080}},
        {"service": {"protocol": "http", "port": True}},
        {"peer": {"host": "invalid-ip", "port": 1234}},
        {"peer": {"host": "::1", "port": -1}},
        {"payload_b64": "@@"},
        {"payload_b64": "YQ=="},
    ],
)
def test_invalid_event_shape(tmp_path: Path, changes: dict[str, Any]) -> None:
    assert_error(
        source(tmp_path, header(), event(**changes)), "INVALID_RECORD", len(line(header()))
    )


@pytest.mark.parametrize(
    "bad",
    [
        b"[]\n",
        b"null\n",
        b"not-json\n",
        b"\xff\n",
        b'{"record":"event","record":"event"}\n',
        b'{"record":"event","rec\\u006frd":"event"}\n',
        b'{"record":"event","peer":{"host":"a","host":"b"}}\n',
        b'{"record":"event","seq":NaN}\n',
        b'{"record":"event","seq":Infinity}\n',
        b'{"record":"event","seq":-Infinity}\n',
    ],
)
def test_invalid_complete_json_is_not_silently_skipped(tmp_path: Path, bad: bytes) -> None:
    path = source(tmp_path, header(), bad, event(), footer())
    assert_error(path, "INVALID_RECORD", len(line(header())))


@pytest.mark.parametrize("sequences", [(1, 1), (3, 2)])
def test_duplicate_or_decreasing_seq(tmp_path: Path, sequences: tuple[int, int]) -> None:
    assert_error(
        source(tmp_path, header(), event(seq=sequences[0]), event(seq=sequences[1])),
        "INVALID_SEQUENCE",
    )


def test_seq_below_first_seq(tmp_path: Path) -> None:
    assert_error(source(tmp_path, header(first_seq=4), event(seq=3)), "INVALID_SEQUENCE")


@pytest.mark.parametrize(
    "rows",
    [
        [event()],
        [footer()],
        [header(), header()],
        [header(), event(), footer(journal_id=OTHER)],
        [header(), event(), footer(), b"partial"],
        [header(), event(), footer(), footer()],
    ],
)
def test_structure_errors(tmp_path: Path, rows: list[Any]) -> None:
    assert_error(source(tmp_path, *rows), "INVALID_STRUCTURE")


def test_footer_must_describe_last_read_seq(tmp_path: Path) -> None:
    assert_error(source(tmp_path, header(), event(seq=5), footer(last_seq=4)), "INVALID_SEQUENCE")


def test_line_limit_includes_lf_and_partial_tail(tmp_path: Path) -> None:
    # JSON-пробелы увеличивают физическую строку, не меняя её содержание.
    limit = 1024
    encoded = line(event())
    exact = encoded[:-1] + b" " * (limit - len(encoded)) + b"\n"
    path = source(tmp_path, header(), exact, footer())
    with LowJournalReader(max_line_bytes=limit).open(path, binding()) as stream:
        records = list(stream)
        assert len(records[1].raw) == limit
        assert stream.summary.status == "closed"
    for oversized in (exact[:-1] + b" \n", b"x" * (limit + 1)):
        source(tmp_path, header(), oversized)
        with pytest.raises(JournalReadError) as caught:
            with LowJournalReader(max_line_bytes=limit).open(path, binding()) as stream:
                list(stream)
        assert caught.value.code == "LINE_TOO_LONG"
        assert caught.value.offset == len(line(header()))


def test_expected_errors_do_not_leak_source_in_traceback(tmp_path: Path) -> None:
    secret = "fixture-secret-must-not-appear"
    path = source(tmp_path, header(), event(unknown=secret))
    with pytest.raises(JournalReadError) as caught:
        read(path)
    assert secret not in str(caught.value)
    assert secret not in repr(caught.value)
    assert secret not in "".join(traceback.format_exception(caught.value))


def test_complete_supplied_low_sample() -> None:
    # После переноса файла теста в tests/telemetry/ это корень рабочего дерева.
    root = Path(__file__).resolve().parents[2]
    path = root / "specs" / "samples" / "low-journal-v1.sample.jsonl"
    expected = JournalBinding(
        deployment_id="b33f0c19-3d48-4fcb-8465-2fa1b0dcd21b",
        instance_id="405f5513-4547-402a-9a26-ff1d0a0a6b1e",
        profile_revision=1,
    )
    raw = path.read_bytes()
    records, summary = read(path, expected)
    lines = raw.splitlines(keepends=True)
    assert len(records) == len(lines)
    assert summary.status == "closed"
    assert summary.next_offset == len(raw)
    offset = 0
    for record, original in zip(records, lines, strict=True):
        assert record.raw == original
        assert record.offset == offset
        offset += len(original)
        assert record.next_offset == offset
    assert path.read_bytes() == raw
