"""Cowrie сохраняет checkpoint между перезапусками и не передаёт исходный пароль."""

import json
import os
from uuid import uuid4

import pytest

from agent.telemetry.adapters.cowrie import CowrieCollector
from agent.telemetry.low_journal.models import JournalBinding
from agent.telemetry.spool import SpoolRepository


def test_cowrie_resume_identity_and_password_mask(tmp_path):
    agent = str(uuid4())
    binding = JournalBinding(str(uuid4()), str(uuid4()), 1)
    path = tmp_path / "medium" / binding.deployment_id / binding.instance_id / "log" / "cowrie.json"
    path.parent.mkdir(parents=True)
    raw = {
        "eventid": "cowrie.login.success",
        "session": "abc",
        "username": "root",
        "password": "secret",
        "timestamp": "2026-10-15T00:00:00Z",
        "src_ip": "127.0.0.1",
    }
    path.write_text(json.dumps(raw) + "\n", encoding="utf8")
    spool_path = tmp_path / "spool.db"
    spool = SpoolRepository(spool_path, agent)
    CowrieCollector(spool, tmp_path).step(binding)
    first = spool.db.execute("SELECT event_id,payload FROM pending").fetchall()
    assert len(first) == 1
    assert "secret" not in first[0][1]
    spool.close()
    spool = SpoolRepository(spool_path, agent)
    CowrieCollector(spool, tmp_path).step(binding)
    assert spool.db.execute("SELECT event_id,payload FROM pending").fetchall() == first
    spool.close()


@pytest.mark.parametrize(
    "raw", [b"[]\n", b"{}\n", b"x" * 65537 + b"\n"], ids=["array", "missing-fields", "oversize"]
)
def test_bad_record_is_controlled_error(tmp_path, raw):
    binding = JournalBinding(str(uuid4()), str(uuid4()), 1)
    path = tmp_path / "medium" / binding.deployment_id / binding.instance_id / "log" / "cowrie.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    spool = SpoolRepository(tmp_path / "queue.db", str(uuid4()))
    try:
        with pytest.raises(ValueError):
            CowrieCollector(spool, tmp_path).step(binding)
        assert spool.count() == 0
    finally:
        spool.close()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO требует POSIX")
def test_fifo_is_rejected_before_open(tmp_path):
    binding = JournalBinding(str(uuid4()), str(uuid4()), 1)
    path = tmp_path / "medium" / binding.deployment_id / binding.instance_id / "log" / "cowrie.json"
    path.parent.mkdir(parents=True)
    os.mkfifo(path)
    spool = SpoolRepository(tmp_path / "queue.db", str(uuid4()))
    try:
        with pytest.raises(ValueError):
            CowrieCollector(spool, tmp_path).step(binding)
    finally:
        spool.close()
