"""Исполняемая Linux-проверка DAC под отдельным UID с группой чтения Low.

Root нужен только родителю для подготовки владельцев и необратимой смены UID
дочернего процесса. Это не проверка read-only mount или контейнерного runtime.
"""

import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from test_low_journal_reader_contract import binding, event, footer, header, line
from test_sender import AGENT


@pytest.mark.parametrize("reader_group", [False, True])
def test_separate_linux_uid_reads_group_journal_and_writes_own_spool(reader_group):
    required = os.environ.get("HF_REQUIRE_LINUX_ACCESS") == "1"
    if sys.platform != "linux" or os.geteuid() != 0:
        if required:
            pytest.fail("Проверке необходим Linux и root только для родительского процесса")
        pytest.skip("Отдельный UID проверяется выделенным Linux CI шагом через sudo")

    original = b"".join(line(row) for row in (header(), event(), footer()))
    # Закрытый TMPDIR оператора может не давать проход отдельному UID.
    with TemporaryDirectory(prefix="hf-low-access-", dir="/tmp") as temporary:
        root = Path(temporary)
        root.chmod(0o755)
        journals = root / "journals"
        journals.mkdir()
        os.chown(journals, 0, 65532)
        journals.chmod(0o750)
        journal = journals / "source.jsonl"
        journal.write_bytes(original)
        os.chown(journal, 0, 65532)
        journal.chmod(0o640)
        source_stat = journal.stat()
        local = root / "collector"
        local.mkdir()
        os.chown(local, 65533, 65533)
        local.chmod(0o700)
        spool = local / "raw.db"

        script = """
import os, sys
from pathlib import Path
from agent.telemetry.low_journal import JournalBinding
from agent.telemetry.low_journal.collector import LowJournalCollector
from agent.telemetry.low_journal.durable import RawJournalRepository
from agent.telemetry.spool import SpoolRepository
journal, spool = Path(sys.argv[1]), Path(sys.argv[2])
group = sys.argv[3] == "1"
bound = JournalBinding(sys.argv[5], sys.argv[6], 1)
os.setgroups([65532] if group else [])
os.setgid(65533)
os.setuid(65533)
assert os.getuid() == os.geteuid() == 65533
assert os.getgid() == os.getegid() == 65533
assert os.getgroups() == ([65532] if group else [])
capabilities = dict(row.split(":", 1) for row in Path("/proc/self/status").read_text().splitlines()
                    if ":" in row)
assert all(int(capabilities[name], 16) == 0 for name in ("CapEff", "CapPrm", "CapAmb"))
repo = RawJournalRepository(spool, sys.argv[4])
try:
    result = LowJournalCollector(repo, bound).step([journal])
    assert (result.state, result.saved_records, result.code) == (
        ("progress", 3, None) if group else ("waiting", 0, "IO_ERROR")
    )
    journal_id = sys.argv[7]
    assert repo.checkpoint(bound, journal_id) == (journal.stat().st_size if group else 0)
finally:
    repo.close()
# Проверяем фактические операции ядра, а не os.access или биты stat.
for mode in ("ab", "wb"):
    try:
        with journal.open(mode) as stream:
            stream.write(b"forbidden")
    except PermissionError:
        pass
    else:
        raise AssertionError("Сборщик получил запись в исходный журнал")
for operation in (lambda: journal.unlink(),
                  lambda: journal.rename(journal.with_name("moved.jsonl")),
                  lambda: journal.chmod(0o660)):
    try:
        operation()
    except PermissionError:
        pass
    else:
        raise AssertionError("Сборщик смог изменить журнал или его права")
pending = SpoolRepository(spool, sys.argv[4])
try:
    assert pending.count() == 0
finally:
    pending.close()
# Повторное открытие под тем же UID подтверждает доступность WAL/spool.
repo = RawJournalRepository(spool, sys.argv[4])
try:
    result = LowJournalCollector(repo, bound).step([journal])
    assert result.saved_records == 0
    assert result.code == (None if group else "IO_ERROR")
    assert repo.checkpoint(bound, journal_id) == (journal.stat().st_size if group else 0)
finally:
    repo.close()
"""
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                str(journal),
                str(spool),
                str(int(reader_group)),
                AGENT,
                binding().deployment_id,
                binding().instance_id,
                header()["journal_id"],
            ],
            capture_output=True,
            timeout=20,
        )
        assert result.returncode == 0, result.stderr.decode(errors="replace")
        assert journal.read_bytes() == original
        after = journal.stat()
        assert (after.st_uid, after.st_gid, after.st_mode) == (
            source_stat.st_uid,
            source_stat.st_gid,
            source_stat.st_mode,
        )
        assert spool.stat().st_uid == 65533
        assert list(journals.iterdir()) == [journal]
        with closing(sqlite3.connect(spool)) as db:
            rows = db.execute("SELECT raw FROM low_raw_records ORDER BY offset").fetchall()
            assert b"".join(row[0] for row in rows) == (original if reader_group else b"")
            assert db.execute("SELECT COUNT(*) FROM pending").fetchone() == (0,)
