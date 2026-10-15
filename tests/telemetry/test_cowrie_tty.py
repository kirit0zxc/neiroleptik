"""Реальный бинарный формат Cowrie: направление, время, разрывы и повтор доставки."""

import base64
import json
from pathlib import Path
from uuid import uuid4

from agent.telemetry.adapters.cowrie import CowrieCollector
from agent.telemetry.adapters.cowrie_tty import HEADER, CowrieTtyCollector
from agent.telemetry.low_journal.models import JournalBinding
from agent.telemetry.spool import SpoolRepository


def recording(path: Path, *, closed: bool = True) -> None:
    raw = HEADER.pack(1, 0, 0, 0, 100, 0)
    for direction, stamp, data in [(1, 101, b"whoami\r"), (2, 102, b"root\r\n")]:
        raw += HEADER.pack(3, 0, len(data), direction, stamp, 0) + data
    if closed:
        raw += HEADER.pack(2, 0, 0, 0, 103, 0)
    path.write_bytes(raw)


def test_real_tty_format_preserves_bytes_and_timing(tmp_path):
    path = tmp_path / "tty"
    recording(path)
    chunks = list(CowrieTtyCollector.chunks(path))
    assert [c["direction"] for c in chunks] == ["in", "out", "out"]
    assert [c["offset_ms"] for c in chunks] == [1000, 2000, 3000]
    assert base64.b64decode(chunks[1]["data_base64"]) == b"root\r\n"
    assert chunks[-1]["final"] and not chunks[-1]["truncated"]


def test_missing_close_has_explicit_gap(tmp_path):
    path = tmp_path / "tty"
    recording(path, closed=False)
    chunks = list(CowrieTtyCollector.chunks(path))
    assert chunks[-1]["final"] and chunks[-1]["truncated"]


def test_large_frame_is_split_below_event_budget(tmp_path):
    path = tmp_path / "tty"
    payload = b"z" * 9000
    path.write_bytes(
        HEADER.pack(1, 0, 0, 0, 100, 0)
        + HEADER.pack(3, 0, len(payload), 2, 101, 0)
        + payload
        + HEADER.pack(2, 0, 0, 0, 102, 0)
    )
    chunks = list(CowrieTtyCollector.chunks(path))
    assert b"".join(base64.b64decode(c["data_base64"]) for c in chunks) == payload
    assert all(len(base64.b64decode(c["data_base64"])) <= 4096 for c in chunks)


def test_restart_mid_recording_resumes_without_duplicate_and_closes_manifest(tmp_path):
    binding = JournalBinding(str(uuid4()), str(uuid4()), 1)
    directory = tmp_path / "medium" / binding.deployment_id / binding.instance_id
    (directory / "tty").mkdir(parents=True)
    (directory / "log").mkdir()
    name = "a" * 64
    content = HEADER.pack(1, 0, 0, 0, 100, 0)
    for index in range(150):
        content += HEADER.pack(3, 0, 1, 2, 101 + index, 0) + b"x"
    content += HEADER.pack(2, 0, 0, 0, 252, 0)
    (directory / "tty" / name).write_bytes(content)
    common = {"session": "ssh1", "timestamp": "2026-10-15T12:00:00Z"}
    records = [
        {**common, "eventid": "cowrie.log.closed", "ttylog": "/untrusted/" + name},
        {**common, "eventid": "cowrie.session.closed"},
    ]
    (directory / "log" / "cowrie.json").write_text("".join(json.dumps(v) + "\n" for v in records))
    agent = str(uuid4())
    spool = SpoolRepository(tmp_path / "queue.db", agent)
    CowrieCollector(spool, tmp_path).step(binding)
    assert spool.count() == 100
    spool.close()
    spool = SpoolRepository(tmp_path / "queue.db", agent)
    CowrieCollector(spool, tmp_path).step(binding)
    assert spool.count() == 153
    CowrieCollector(spool, tmp_path).step(binding)
    assert spool.count() == 153
    payloads = [json.loads(row[0]) for row in spool.db.execute("SELECT payload FROM pending")]
    close = next(p for p in payloads if p["type"] == "session.closed")
    assert len(close["data"]["replay_recordings"]) == 1
    spool.close()
