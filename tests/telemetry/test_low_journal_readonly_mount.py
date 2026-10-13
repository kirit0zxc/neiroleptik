"""Серверный bind mount: EROFS при разрешающем запись DAC, вне runtime deployment."""

import os
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from test_low_journal_reader_contract import binding, event, footer, header, line
from test_sender import AGENT


def test_readonly_mount_blocks_mutation_and_preserves_writable_spool():
    required = os.environ.get("HF_REQUIRE_READONLY_MOUNT") == "1"
    if not required:
        pytest.skip("Привилегированный mount-сценарий запускается отдельно только на сервере")
    if sys.platform != "linux" or os.geteuid() != 0:
        pytest.fail("Нужен Linux и root только для подготовки приватного namespace")
    unshare, mount = shutil.which("unshare"), shutil.which("mount")
    if unshare is None or mount is None:
        pytest.fail("Нужны util-linux unshare/mount и CAP_SYS_ADMIN; skip недопустим")

    original = b"".join(line(row) for row in (header(), event(), footer()))
    # Предки фикстуры должны быть проходимы после необратимого setuid.
    with TemporaryDirectory(prefix="hf-low-ro-", dir="/tmp") as temporary:
        root = Path(temporary)
        root.chmod(0o755)
        source = root / "source"
        target = root / "mounted"
        local = root / "spool"
        for directory in (source, target, local):
            directory.mkdir()
            os.chown(directory, 65533, 65533)
            directory.chmod(0o770)
        journal = source / "source.jsonl"
        journal.write_bytes(original)
        # Один владелец намеренно имеет write: запрет доказывает именно mount.
        os.chown(journal, 65533, 65533)
        journal.chmod(0o660)
        spool = local / "raw.db"
        script = """
import errno, os, subprocess, sys
from pathlib import Path
from agent.telemetry.low_journal import JournalBinding
from agent.telemetry.low_journal.collector import LowJournalCollector
from agent.telemetry.low_journal.durable import RawJournalRepository
source, target, spool = map(Path, sys.argv[1:4])
mount = sys.argv[4]
subprocess.run([mount, "--bind", str(source), str(target)], check=True, timeout=5)
subprocess.run([mount, "-o", "remount,bind,ro", str(target)], check=True, timeout=5)
os.setgroups([])
os.setgid(65533)
os.setuid(65533)
assert os.getuid() == os.geteuid() == 65533
assert os.getgid() == os.getegid() == 65533 and os.getgroups() == []
capabilities = dict(row.split(":", 1) for row in Path("/proc/self/status").read_text().splitlines()
                    if ":" in row)
assert all(int(capabilities[name], 16) == 0 for name in ("CapEff", "CapPrm", "CapAmb"))
journal = target / "source.jsonl"
assert journal.stat().st_uid == os.getuid()
assert journal.stat().st_mode & 0o200
assert target.stat().st_uid == os.getuid() and target.stat().st_mode & 0o200
bound = JournalBinding(sys.argv[6], sys.argv[7], 1)
for attempt in range(2):
    repo = RawJournalRepository(spool, sys.argv[5])
    try:
        result = LowJournalCollector(repo, bound).step([journal])
        assert result.saved_records == (3 if attempt == 0 else 0)
        assert result.code is None
        assert repo.checkpoint(bound, sys.argv[8]) == journal.stat().st_size
    finally:
        repo.close()
def append():
    with journal.open("ab") as stream:
        stream.write(b"forbidden")
def truncate():
    with journal.open("wb"):
        pass
for operation in (append, truncate, lambda: journal.unlink(),
                  lambda: journal.rename(target / "moved.jsonl"),
                  lambda: journal.chmod(0o600)):
    try:
        operation()
    except OSError as error:
        assert error.errno == errno.EROFS
    else:
        raise AssertionError("Read-only mount разрешил изменение исходника")
"""
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])}
        child = subprocess.run(
            [
                unshare,
                "--mount",
                "--propagation",
                "private",
                sys.executable,
                "-c",
                script,
                str(source),
                str(target),
                str(spool),
                mount,
                AGENT,
                binding().deployment_id,
                binding().instance_id,
                header()["journal_id"],
            ],
            env=env,
            capture_output=True,
            timeout=25,
        )
        assert child.returncode == 0, child.stderr.decode(errors="replace")
        # После завершения namespace исчез; очищается только TemporaryDirectory.
        assert list(target.iterdir()) == []
        assert list(source.iterdir()) == [journal]
        assert journal.read_bytes() == original
        assert journal.stat().st_mode & 0o777 == 0o660
        assert spool.stat().st_uid == 65533
        with closing(sqlite3.connect(spool)) as db:
            rows = db.execute("SELECT raw FROM low_raw_records ORDER BY offset").fetchall()
            assert b"".join(row[0] for row in rows) == original
            assert db.execute("SELECT COUNT(*) FROM pending").fetchone() == (0,)
