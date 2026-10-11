"""Полная настоящая ротация и намеренный перенос connection/request между файлами."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest
from test_low_journal_reader_contract import DEPLOYMENT, INSTANCE, binding, event, read

from agent.telemetry.low_journal import JournalBinding, JournalReadError, LowJournalReader
from agent.telemetry.low_journal.validation import RecordValidator
from honeypots.low.journal import JournalSettings, RotatingJournalSink
from infra.runtime.sample_low_journal import run_sample


def assert_exact_file(path: Path, expected: JournalBinding) -> list[dict[str, object]]:
    """Эталонные offsets вычисляются по bytes файла независимо от кода reader."""
    raw = path.read_bytes()
    names = sorted(item.name for item in path.parent.iterdir())
    digest = hashlib.sha256(raw).digest()
    expected_lines = raw.splitlines(keepends=True)
    with LowJournalReader().open(path, expected) as stream:
        records = list(stream)
        assert stream.summary.status == "closed"
        assert stream.summary.next_offset == len(raw)
    assert len(records) == len(expected_lines)
    offset = 0
    for record, original in zip(records, expected_lines, strict=True):
        assert record.offset == offset
        offset += len(original)
        assert record.next_offset == offset
        assert record.raw == original
        assert record.document() == json.loads(original)
    assert path.read_bytes() == raw
    assert hashlib.sha256(path.read_bytes()).digest() == digest
    assert sorted(item.name for item in path.parent.iterdir()) == names
    return [record.document() for record in records]


def test_real_low_process_rotation_all_complete_files(tmp_path: Path) -> None:
    # Ошибка мягкой остановки здесь является ошибкой smoke, а не skip/xfail.
    result = run_sample(tmp_path / "process-journal", rotate=True)
    assert result.return_code == 0
    assert len(result.files) >= 2
    assert result.connections == 167
    expected = JournalBinding(result.deployment_id, result.instance_id, 1)
    previous = None
    seq = 1
    by_connection: dict[str, list[str]] = {}
    for index, path in enumerate(result.files, start=1):
        first, *events, last = assert_exact_file(path, expected)
        assert first["journal_seq"] == index
        assert first["previous_journal_id"] == previous
        assert first["first_seq"] == seq
        for row in events:
            assert row["seq"] == seq
            seq += 1
            by_connection.setdefault(row["connection_id"], []).append(row["event"])
        assert last["journal_id"] == first["journal_id"]
        assert last["last_seq"] == seq - 1
        assert last["reason"] == ("shutdown" if index == len(result.files) else "rotation")
        previous = first["journal_id"]
    assert seq == 335
    assert len(by_connection) == result.connections
    assert all(kinds == ["connection", "request"] for kinds in by_connection.values())


def test_sink_forced_cross_file_connection_and_seq_gap(tmp_path: Path) -> None:
    settings = JournalSettings(tmp_path, INSTANCE, DEPLOYMENT, 1, max_file_bytes=262144)
    sink = RotatingJournalSink(settings)
    sink.open()
    try:
        # По 120000 base64-байт: две записи помещаются, третья требует ротации.
        payload = base64.b64encode(b"\xff" * 90000).decode("ascii")
        for seq in (1, 2):
            row = event(seq=seq, event="request", payload_b64=payload)
            del row["record"]
            sink.emit(row)
        connection = event(seq=3)
        del connection["record"]
        sink.emit(connection)
        request = event(seq=5, event="request", payload_b64=payload)
        del request["record"]
        sink.emit(request)
    finally:
        sink.close()
    paths = sorted(tmp_path.glob("*.jsonl"))
    assert len(paths) == 2
    # Эти standalone-записи намеренно больше дефолтного предела reader.
    rows = []
    for path in paths:
        raw = path.read_bytes()
        with LowJournalReader(max_line_bytes=131072).open(path, binding()) as stream:
            records = list(stream)
            assert stream.summary.status == "closed"
        offset = 0
        for record, original in zip(records, raw.splitlines(keepends=True), strict=True):
            assert (record.offset, record.raw) == (offset, original)
            offset += len(original)
            assert record.next_offset == offset
        assert path.read_bytes() == raw
        rows.append([record.document() for record in records])
    assert rows[0][-2]["event"] == "connection"
    assert rows[1][1]["event"] == "request"
    assert rows[0][-2]["connection_id"] == rows[1][1]["connection_id"]
    assert rows[0][-1]["reason"] == "rotation"
    assert rows[1][-1]["reason"] == "shutdown"
    assert rows[1][0]["previous_journal_id"] == rows[0][0]["journal_id"]
    assert rows[1][0]["journal_seq"] == 2
    assert rows[1][0]["first_seq"] == 5
    assert [row["seq"] for file in rows for row in file if row["record"] == "event"] == [1, 2, 3, 5]


def test_fresh_sink_process_can_start_without_predecessor(tmp_path: Path) -> None:
    settings = JournalSettings(tmp_path, INSTANCE, DEPLOYMENT, 1)
    for _ in range(2):
        sink = RotatingJournalSink(settings)
        sink.open()
        sink.close()
    paths = sorted(tmp_path.glob("*.jsonl"))
    assert len(paths) == 2
    records, summary = read(paths[1])
    assert summary.status == "closed"
    assert records[0].document()["journal_seq"] == 2
    assert records[0].document()["previous_journal_id"] is None


def test_executor_fragments_keep_declared_source_positions() -> None:
    """Проверяет только строки и метаданные выборки, не недоступные исходные файлы."""
    directory = Path(__file__).resolve().parents[2] / "specs/samples/low-executor-real"
    metadata_raw = (directory / "positions.json").read_bytes()
    metadata = json.loads(metadata_raw)
    expected = JournalBinding(
        "0b60f7ec-9175-4ea1-bc90-437baceccf04", "3df48877-5e4c-496d-b825-63c2105a999e", 1
    )
    validator = RecordValidator()
    previous_id = None
    for index, item in enumerate(metadata, start=1):
        path = directory / item["fragment"]
        original = path.read_bytes()
        lines = original.splitlines(keepends=True)
        positions = item["positions"]
        assert len(lines) == len(positions)
        assert len(original) < item["source_size"]
        documents = [
            validator.parse(raw, position["byte_offset"])
            for raw, position in zip(lines, positions, strict=True)
        ]
        assert positions[0] == {"source_line": 1, "byte_offset": 0}
        assert documents[0]["journal_seq"] == index
        assert documents[0]["previous_journal_id"] == previous_id
        assert documents[0]["instance_id"] == expected.instance_id
        assert documents[0]["deployment_id"] == expected.deployment_id
        previous_id = documents[0]["journal_id"]
        assert documents[-1]["journal_id"] == previous_id
        assert positions[-1]["byte_offset"] + len(lines[-1]) == item["source_size"]
        for before, after, raw in zip(positions, positions[1:], lines, strict=False):
            assert after["source_line"] > before["source_line"]
            distance = after["byte_offset"] - before["byte_offset"]
            if after["source_line"] == before["source_line"] + 1:
                assert distance == len(raw)
            else:
                assert distance > len(raw)
        # Footer описывает пропущенные события; склеенная выборка не полный журнал.
        with pytest.raises(JournalReadError) as caught:
            read(path, expected)
        assert caught.value.code == "INVALID_SEQUENCE"
        assert path.read_bytes() == original
    assert (directory / "positions.json").read_bytes() == metadata_raw
