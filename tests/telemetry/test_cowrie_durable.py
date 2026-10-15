"""Durable Cowrie: настоящая SQLite, неизменяемые raw и восстановление ledger."""

import asyncio
import json
import os
import subprocess
import sys
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest
from test_continuous_low import Center

from agent.runtime.executor.store import ExecutionStore
from agent.runtime.executor.telemetry import LowTelemetryWorker
from agent.telemetry.adapters.cowrie import CowrieBinding
from agent.telemetry.cowrie_journal.collector import CowrieJournalCollector
from agent.telemetry.cowrie_journal.conversion import CowrieRawConverter
from agent.telemetry.cowrie_journal.models import (
    CowrieCollectionLimits,
    CowrieJournalError,
    CowrieJournalSource,
)
from agent.telemetry.cowrie_journal.repository import CowrieJournalRepository
from agent.telemetry.low_journal.models import JournalBinding
from agent.telemetry.spool import SpoolRepository

AGENT = UUID(int=100)
BINDING = CowrieBinding(AGENT, UUID(int=101), UUID(int=102), 1, 2222)
JOURNAL = UUID(int=103)


def line(**changes):
    return (
        json.dumps(
            {
                "eventid": "cowrie.command.input",
                "timestamp": "2026-10-15T09:00:00Z",
                "session": "session-a",
                "input": "echo привет",
                **changes,
            },
            ensure_ascii=False,
        ).encode()
        + b"\n"
    )


def source(root, *, name="cowrie.json", journal=JOURNAL, binding=BINDING):
    return CowrieJournalSource(binding, journal, root, name)


@pytest.fixture
def repository(tmp_path):
    with closing(SpoolRepository(tmp_path / "spool.db", str(AGENT))) as spool:
        yield CowrieJournalRepository(spool)


def collect(repository, source, limits=None):
    return CowrieJournalCollector(repository).step(source, limits)


def count(repository, table):
    assert table in {
        "cowrie_raw",
        "cowrie_ledger",
        "cowrie_scopes",
        "cowrie_generations",
        "pending",
    }
    return repository.db.execute("SELECT count(*) FROM " + table).fetchone()[0]


def test_raw_first_then_atomic_conversion_and_repeat_after_ack(repository, tmp_path):
    raw = line()
    (tmp_path / "cowrie.json").write_bytes(raw)
    src = source(tmp_path)
    assert collect(repository, src).records == 1
    assert repository.state(JOURNAL)[2] == len(raw)
    assert count(repository, "pending") == count(repository, "cowrie_ledger") == 0
    converter = CowrieRawConverter(repository)
    assert converter.convert(BINDING, JOURNAL, 0) == "created"
    sender_center = Center()
    from agent.telemetry.sender import TelemetrySender

    assert TelemetrySender(repository.spool, sender_center).step().accepted == 1
    assert repository.spool.count() == 0
    assert converter.convert(BINDING, JOURNAL, 0) == "repeated"
    assert converter.step(BINDING, JOURNAL).state == "idle"
    assert repository.spool.count() == 0
    assert count(repository, "cowrie_raw") == count(repository, "cowrie_ledger") == 1
    assert repository.db.execute("SELECT payload FROM cowrie_ledger").fetchone()[0] is not None


def test_unknown_is_ignored_but_bad_complete_record_is_saved_and_blocks(repository, tmp_path):
    first = line(eventid="cowrie.client.version")
    invalid = line(timestamp="SECRET")
    path = tmp_path / "cowrie.json"
    path.write_bytes(first + invalid + line(input="later"))
    assert collect(repository, source(tmp_path)).records == 3
    result = CowrieRawConverter(repository).step(BINDING, JOURNAL)
    assert result.records == 1 and result.code == "INVALID_RECORD"
    assert repository.state(JOURNAL)[4] == "INVALID_RECORD"
    assert repository.db.execute("SELECT result FROM cowrie_ledger").fetchall() == [("ignored",)]
    assert count(repository, "cowrie_raw") == 3 and count(repository, "pending") == 0
    assert "SECRET" not in repr(result)


def test_tail_survives_reopen_and_completes_without_premature_checkpoint(tmp_path):
    raw = line()
    path = tmp_path / "cowrie.json"
    path.write_bytes(raw[:-3])
    src = source(tmp_path)
    db = tmp_path / "spool.db"
    with closing(SpoolRepository(db, str(AGENT))) as spool:
        repo = CowrieJournalRepository(spool)
        assert collect(repo, src).code == "INCOMPLETE_LINE"
        assert repo.state(JOURNAL)[2:4] == (0, raw[:-3])
    with path.open("ab") as stream:
        stream.write(raw[-3:])
    with closing(SpoolRepository(db, str(AGENT))) as spool:
        repo = CowrieJournalRepository(spool)
        assert collect(repo, src).records == 1
        assert repo.state(JOURNAL)[2:4] == (len(raw), b"")
        assert CowrieRawConverter(repo).step(BINDING, JOURNAL).records == 1


@pytest.mark.parametrize("mutation", ["prefix", "tail", "truncate", "replace"])
def test_saved_positions_and_file_identity_cannot_be_reused(repository, tmp_path, mutation):
    raw = line()
    path = tmp_path / "cowrie.json"
    path.write_bytes(raw + b"tail")
    src = source(tmp_path)
    collect(repository, src)
    if mutation == "prefix":
        path.write_bytes(raw.replace(b"echo", b"ECHO") + b"tail")
    elif mutation == "tail":
        path.write_bytes(raw + b"evil")
    elif mutation == "truncate":
        path.write_bytes(raw[:-1])
    else:
        path.rename(tmp_path / "kept.json")
        path.write_bytes(raw + b"tail")
    result = collect(repository, src)
    assert result.code in {"POSITION_CONFLICT", "SOURCE_TRUNCATED", "SOURCE_REPLACED"}
    assert repository.state(JOURNAL)[4] == result.code
    assert count(repository, "cowrie_raw") == 1
    assert CowrieRawConverter(repository).step(BINDING, JOURNAL).code == result.code


def test_rename_keeps_generation_new_file_requires_new_uuid(repository, tmp_path):
    raw = line()
    path = tmp_path / "cowrie.json"
    path.write_bytes(raw)
    src = source(tmp_path)
    collect(repository, src)
    path.rename(tmp_path / "cowrie.1.json")
    assert collect(repository, replace(src, relative_path="cowrie.1.json")).records == 0
    path.write_bytes(line(input="new"))
    second = replace(src, journal_id=UUID(int=104))
    assert collect(repository, second).records == 1
    assert len(repository.bindings()) == 2
    assert CowrieRawConverter(repository).step(BINDING, JOURNAL).records == 1
    assert CowrieRawConverter(repository).step(BINDING, second.journal_id).records == 1
    assert count(repository, "pending") == 2


def test_same_file_cannot_be_registered_as_another_generation(repository, tmp_path):
    (tmp_path / "cowrie.json").write_bytes(line())
    src = source(tmp_path)
    collect(repository, src)
    assert collect(repository, replace(src, journal_id=UUID(int=104))).code == "GENERATION_CONFLICT"


@pytest.mark.parametrize(
    "change", [{"profile_revision": 2}, {"service_port": 22}, {"instance_id": UUID(int=555)}]
)
def test_binding_and_journal_scope_are_pinned(repository, tmp_path, change):
    (tmp_path / "cowrie.json").write_bytes(line())
    src = source(tmp_path)
    collect(repository, src)
    result = collect(repository, replace(src, binding=replace(BINDING, **change)))
    assert result.code in {"BINDING_CONFLICT", "GENERATION_CONFLICT"}
    assert count(repository, "cowrie_raw") == 1
    # Ошибка входного реестра не отравляет ранее подтверждённый другой scope.
    assert repository.state(JOURNAL)[4] is None
    assert CowrieRawConverter(repository).step(BINDING, JOURNAL).records == 1


@pytest.mark.parametrize("field", ["max_records", "max_bytes", "max_scan_bytes"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_limits_are_strict(field, value):
    with pytest.raises(ValueError):
        CowrieCollectionLimits(**{field: value})


def test_record_byte_scan_and_conversion_limits(repository, tmp_path):
    raw = line()
    (tmp_path / "cowrie.json").write_bytes(raw * 3)
    src = source(tmp_path)
    assert collect(repository, src, CowrieCollectionLimits(max_scan_bytes=1)).code == "SCAN_LIMIT"
    assert (
        collect(repository, src, CowrieCollectionLimits(max_bytes=len(raw) - 1)).code
        == "STEP_LIMIT"
    )
    result = collect(repository, src, CowrieCollectionLimits(max_records=1))
    assert result.code == "STEP_LIMIT" and result.records == 1
    assert collect(repository, src).records == 2
    converter = CowrieRawConverter(repository)
    assert converter.step(BINDING, JOURNAL, CowrieCollectionLimits(max_records=1)).records == 1
    assert converter.step(BINDING, JOURNAL).records == 2


def test_quota_rolls_back_pending_ledger_and_raw(tmp_path):
    raw = line()
    (tmp_path / "cowrie.json").write_bytes(raw)
    with closing(SpoolRepository(tmp_path / "spool.db", str(AGENT))) as spool:
        repo = CowrieJournalRepository(spool, max_storage_bytes=len(raw) - 1)
        assert collect(repo, source(tmp_path)).code == "STORAGE_LIMIT"
        assert repo.state(JOURNAL)[2] == count(repo, "cowrie_raw") == 0
        repo.quota = len(raw)
        assert collect(repo, source(tmp_path)).records == 1
        assert CowrieRawConverter(repo).step(BINDING, JOURNAL).code == "STORAGE_LIMIT"
        assert count(repo, "pending") == count(repo, "cowrie_ledger") == 0
        assert repo.db.execute("SELECT bytes FROM cowrie_usage").fetchone()[0] == len(raw)


def test_versions_owner_and_reopen_preserve_low_schema(repository):
    low = repository.db.execute(
        "SELECT value FROM metadata WHERE key='low_storage_version'"
    ).fetchone()
    repository.db.execute("UPDATE metadata SET value='999' WHERE key='cowrie_storage_version'")
    repository.db.commit()
    with pytest.raises(CowrieJournalError, match="UNSUPPORTED_STORAGE_VERSION"):
        CowrieJournalRepository(repository.spool)
    assert (
        repository.db.execute(
            "SELECT value FROM metadata WHERE key='low_storage_version'"
        ).fetchone()
        == low
    )
    repository.db.execute("UPDATE metadata SET value='other' WHERE key='agent_id'")
    repository.db.commit()
    with pytest.raises(CowrieJournalError, match="OWNER_MISMATCH"):
        CowrieJournalRepository(repository.spool)


@pytest.mark.parametrize(
    "name", ["../outside", "/absolute", "./cowrie.json", "a//b", "a\\b", "C:stream", "a\x00b", ""]
)
def test_untrusted_paths_are_rejected_before_io(tmp_path, name):
    with pytest.raises(CowrieJournalError, match="INVALID_BINDING"):
        source(tmp_path, name=name)


def test_hardlink_is_rejected_before_read(repository, tmp_path):
    original = tmp_path / "original"
    original.write_bytes(line())
    os.link(original, tmp_path / "cowrie.json")
    assert collect(repository, source(tmp_path)).code == "UNSAFE_PATH"
    assert count(repository, "cowrie_raw") == 0


@pytest.mark.skipif(os.name != "posix", reason="FIFO/symlink проверяются Linux CI")
@pytest.mark.parametrize("kind", ["fifo", "leaf", "parent"])
def test_unsafe_posix_paths_do_not_open_or_read_outside_root(repository, tmp_path, kind):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "cowrie.json").write_bytes(line())
    root = tmp_path / "root"
    root.mkdir()
    if kind == "fifo":
        os.mkfifo(root / "cowrie.json")
    elif kind == "leaf":
        (root / "cowrie.json").symlink_to(outside / "cowrie.json")
    else:
        (root / "sub").symlink_to(outside, target_is_directory=True)
    result = collect(
        repository, source(root, name="sub/cowrie.json" if kind == "parent" else "cowrie.json")
    )
    assert result.code == "UNSAFE_PATH" and count(repository, "cowrie_raw") == 0


@pytest.mark.parametrize(
    "stage",
    [
        "raw_inserted",
        "raw_before_commit",
        "raw_committed",
        "pending_inserted",
        "ledger_inserted",
        "before_commit",
        "after_commit",
    ],
)
def test_process_crash_recovers_real_transactions(tmp_path, stage):
    db = tmp_path / "spool.db"
    src = source(tmp_path)
    raw = line()
    with closing(SpoolRepository(db, str(AGENT))) as spool:
        repo = CowrieJournalRepository(spool)
        repo.register(src)
        if not stage.startswith("raw_"):
            repo.append(JOURNAL, 0, raw)
    script = """
import os, sys
from uuid import UUID
from agent.telemetry.spool import SpoolRepository
from agent.telemetry.cowrie_journal.repository import CowrieJournalRepository
from agent.telemetry.cowrie_journal.conversion import CowrieRawConverter
from agent.telemetry.adapters.cowrie import CowrieBinding
spool = SpoolRepository(sys.argv[1], sys.argv[2])
def crash(self, stage):
    if stage == sys.argv[3]: os._exit(77)
CowrieJournalRepository._checkpoint = crash
CowrieRawConverter._checkpoint = crash
repo = CowrieJournalRepository(spool)
journal = UUID(int=103)
if sys.argv[3].startswith('raw_'):
    repo.append(journal, 0, bytes.fromhex(sys.argv[4]))
else:
    bound = CowrieBinding(UUID(int=100), UUID(int=101), UUID(int=102), 1, 2222)
    CowrieRawConverter(repo).convert(bound, journal, 0)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(db), str(AGENT), stage, raw.hex()],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 77, result.stderr.decode(errors="replace")
    with closing(SpoolRepository(db, str(AGENT))) as spool:
        repo = CowrieJournalRepository(spool)
        committed_raw = not stage.startswith("raw_") or stage == "raw_committed"
        assert count(repo, "cowrie_raw") == int(committed_raw)
        assert (
            count(repo, "pending") == count(repo, "cowrie_ledger") == int(stage == "after_commit")
        )
        if not committed_raw:
            repo.append(JOURNAL, 0, raw)
        CowrieRawConverter(repo).step(BINDING, JOURNAL)
        assert count(repo, "pending") == count(repo, "cowrie_ledger") == 1


def trusted(binding=BINDING):
    return JournalBinding(
        str(binding.deployment_id), str(binding.instance_id), binding.profile_revision
    )


def test_worker_requires_ack_and_lost_ack_replays_exact_events_after_restart(tmp_path):
    src = source(tmp_path)
    (tmp_path / "cowrie.json").write_bytes(line())
    db = tmp_path / "spool.db"
    center = Center(lose_ack=True)
    with closing(LowTelemetryWorker(db, tmp_path, str(AGENT), center)) as worker:
        assert worker.step((), (src,)) == ("UNTRUSTED_BINDING", "idle")
        assert count(worker._cowrie, "cowrie_scopes") == 0
        assert worker.step((trusted(),), (src,))[1] == "TRANSPORT_ERROR"
    (tmp_path / "cowrie.json").unlink()
    with closing(LowTelemetryWorker(db, tmp_path, str(AGENT), center)) as worker:
        assert worker.step((trusted(),), ())[1] == "delivered:1"
        assert worker.step((trusted(),), ())[1] == "idle"
    assert center.batches[0]["events"] == center.batches[1]["events"]
    assert len(center.ids) == 1


def test_worker_one_bad_source_does_not_block_another_or_sender(tmp_path):
    bad = source(tmp_path)
    good_binding = replace(BINDING, instance_id=UUID(int=777))
    good = source(tmp_path, name="good.json", journal=UUID(int=778), binding=good_binding)
    (tmp_path / "cowrie.json").write_bytes(line(timestamp="SECRET"))
    (tmp_path / "good.json").write_bytes(line(input="good"))
    center = Center()
    with closing(LowTelemetryWorker(tmp_path / "spool.db", tmp_path, str(AGENT), center)) as worker:
        bindings = (trusted(), trusted(good_binding))
        assert worker.step(bindings, (bad, good))[0] == "INVALID_RECORD"
        assert worker.step(bindings, (bad, good))[1] == "delivered:1"
        assert worker.step(bindings, (bad, good))[0] == "INVALID_RECORD"
    assert len(center.ids) == 1


def test_saved_raw_converts_without_original_file_or_provider(tmp_path):
    db = tmp_path / "spool.db"
    with closing(SpoolRepository(db, str(AGENT))) as spool:
        repo = CowrieJournalRepository(spool)
        repo.register(source(tmp_path))
        repo.append(JOURNAL, 0, line())
    center = Center()
    with closing(LowTelemetryWorker(db, tmp_path, str(AGENT), center)) as worker:
        assert worker.step((trusted(),))[1] == "delivered:1"
    assert len(center.ids) == 1


def test_legacy_scope_cannot_implicitly_switch_identity(tmp_path):
    legacy = tmp_path / "medium" / str(BINDING.deployment_id) / str(BINDING.instance_id) / "log"
    legacy.mkdir(parents=True)
    (legacy / "cowrie.json").write_bytes(line())
    center = Center()
    with closing(LowTelemetryWorker(tmp_path / "spool.db", tmp_path, str(AGENT), center)) as worker:
        assert worker.step((trusted(),))[1] == "delivered:1"
        src = source(legacy)
        assert worker.step((trusted(),), (src,))[0] == "LEGACY_CONFLICT"
        assert count(worker._cowrie, "cowrie_scopes") == 0
    assert len(center.ids) == 1


def test_worker_collects_low_and_cowrie_with_one_sender(tmp_path):
    from test_low_journal_reader_contract import binding, event, header
    from test_low_journal_reader_contract import line as low_line

    low = binding()
    directory = tmp_path / low.deployment_id
    directory.mkdir()
    (directory / f"low-{low.instance_id}-one.jsonl").write_bytes(
        low_line(header()) + low_line(event())
    )
    (tmp_path / "cowrie.json").write_bytes(line())
    center = Center()
    with closing(LowTelemetryWorker(tmp_path / "spool.db", tmp_path, str(AGENT), center)) as worker:
        status = worker.step((low,), ())
        assert status[1] == "delivered:1"
        assert worker.step((trusted(), low), (source(tmp_path),))[1] == "delivered:1"
        assert len(center.batches) == 2 and len(center.ids) == 2
        assert count(worker._cowrie, "cowrie_ledger") == 1


def test_actual_executor_ack_history_retains_trust_after_stop(tmp_path):
    from continuous_low_support import command, complete

    with ExecutionStore(tmp_path / "private" / "state.db") as store:
        started = command(store)
        bound = CowrieBinding(
            AGENT, UUID(started.deployment_id), UUID(started.attempt_instance_id), 1, 2222
        )
        src = source(tmp_path, binding=bound)
        (tmp_path / "cowrie.json").write_bytes(line())
        center = Center()
        with closing(
            LowTelemetryWorker(store.prepare_telemetry_path(), tmp_path, str(AGENT), center)
        ) as worker:
            complete(store, started)
            assert store.telemetry_bindings() == ()
            assert worker.step((), (src,))[0] == "UNTRUSTED_BINDING"
            store.acknowledge(started.command_id)
            acked = tuple(JournalBinding(*item) for item in store.telemetry_bindings())
            assert worker.step(acked, (src,))[1] == "delivered:1"
            stopped = command(store, action="stop", generation=2, instance=str(bound.instance_id))
            complete(store, stopped)
            store.acknowledge(stopped.command_id)
            with (tmp_path / "cowrie.json").open("ab") as stream:
                stream.write(line(input="late"))
            historical = tuple(JournalBinding(*item) for item in store.telemetry_bindings())
            assert worker.step(historical, (src,))[1] == "delivered:1"
    assert len(center.ids) == 2


def test_worker_claim_never_falls_back_to_legacy_when_provider_disappears(tmp_path):
    src = source(tmp_path)
    (tmp_path / "cowrie.json").write_bytes(line())
    legacy = tmp_path / "medium" / str(BINDING.deployment_id) / str(BINDING.instance_id) / "log"
    legacy.mkdir(parents=True)
    (legacy / "cowrie.json").write_bytes(line(input="must-not-use-legacy"))
    db = tmp_path / "spool.db"
    center = Center()
    with closing(LowTelemetryWorker(db, tmp_path, str(AGENT), center)) as worker:
        assert worker.step((trusted(),), (src,))[1] == "delivered:1"
        assert worker.step((trusted(),), ())[1] == "idle"
    with closing(LowTelemetryWorker(db, tmp_path, str(AGENT), center)) as worker:
        assert worker.step((trusted(),), ())[1] == "idle"
    assert len(center.ids) == 1


def test_changed_saved_raw_cannot_create_another_event(repository, tmp_path):
    raw = line()
    repository.register(source(tmp_path))
    repository.append(JOURNAL, 0, raw)
    converter = CowrieRawConverter(repository)
    converter.convert(BINDING, JOURNAL, 0)
    repository.db.execute("UPDATE cowrie_raw SET raw=?", (line(input="different"),))
    repository.db.commit()
    with pytest.raises(CowrieJournalError, match="RESULT_CONFLICT"):
        converter.convert(BINDING, JOURNAL, 0)
    assert count(repository, "pending") == 1


def test_stable_raw_in_quarantine_is_not_requeued(repository, tmp_path):
    repository.register(source(tmp_path))
    repository.append(JOURNAL, 0, line())
    converter = CowrieRawConverter(repository)
    converter.convert(BINDING, JOURNAL, 0)
    batch = repository.spool.next_batch()
    from agent.telemetry.schemas import Ack

    ack = Ack.model_validate(
        {
            "batch_id": batch.batch_id,
            "accepted_ids": [],
            "rejected": [
                {
                    "index": 0,
                    "event_id": batch.events[0].event_id,
                    "code": "DEPLOYMENT_MISMATCH",
                    "retryable": False,
                }
            ],
        }
    )
    repository.spool.acknowledge(batch, ack)
    assert repository.spool.quarantine_count() == 1 and repository.spool.count() == 0
    assert converter.convert(BINDING, JOURNAL, 0) == "repeated"
    assert repository.spool.quarantine_count() == 1 and repository.spool.count() == 0


def test_actual_http_ack_loss_preserves_event_bytes_and_post_ack_is_idle(tmp_path):
    from continuous_low_support import Receiver
    from pydantic import SecretStr
    from test_sender import TOKEN

    from agent.telemetry.sender import HttpBatchTransport

    raw = line()
    (tmp_path / "cowrie.json").write_bytes(raw)
    receiver = Receiver(tmp_path / "receiver.db")
    receiver.mode = "drop"
    db = tmp_path / "spool.db"
    with receiver.serve() as endpoint:
        transport = HttpBatchTransport(endpoint, SecretStr(TOKEN), timeout=2)
        with closing(LowTelemetryWorker(db, tmp_path, str(AGENT), transport)) as worker:
            assert worker.step((trusted(),), (source(tmp_path),))[1] == "TRANSPORT_ERROR"
        receiver.mode = "accepted"
        with closing(LowTelemetryWorker(db, tmp_path, str(AGENT), transport)) as worker:
            assert worker.step((trusted(),), (source(tmp_path),))[1] == "delivered:1"
            assert worker.step((trusted(),), (source(tmp_path),))[1] == "idle"
            assert worker._spool.count() == 0
            assert count(worker._cowrie, "cowrie_raw") == 1
            assert count(worker._cowrie, "cowrie_ledger") == 1
        requests = receiver.rows("requests")
        assert len(requests) == 2
        # После restart batch_id может измениться по существующему sender-контракту.
        # Остальные wire bytes, включая Event ID и payload, должны совпасть буквально.
        before, after = (item[0] for item in requests)
        first_id = json.loads(before)["batch_id"].encode()
        second_id = json.loads(after)["batch_id"].encode()
        assert before.replace(first_id, second_id, 1) == after
        events = receiver.rows("events")
        assert len(events) == 1
        assert json.loads(events[0][2])["data"] == {"input": "echo привет"}


def test_provider_failure_still_recovers_saved_raw_and_closes_worker(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace

    from continuous_low_support import command, complete
    from test_sender import TOKEN

    from agent.runtime.executor import telemetry

    async def scenario():
        done = asyncio.Event()
        loop = asyncio.get_running_loop()
        statuses = []
        closed = []
        real_worker = telemetry.LowTelemetryWorker

        class Observed(real_worker):
            def step(self, bindings, sources=()):
                status = super().step(bindings, sources)
                statuses.append(status)
                loop.call_soon_threadsafe(done.set)
                return status

            def close(self):
                super().close()
                closed.append(True)

        class Client:
            async def config(self):
                return SimpleNamespace(host_id=str(AGENT))

        def broken_provider():
            raise RuntimeError("SECRET-provider-path")

        monkeypatch.setattr(telemetry, "LowTelemetryWorker", Observed)
        with ExecutionStore(tmp_path / "private" / "state.db") as store:
            started = command(store)
            complete(store, started)
            store.acknowledge(started.command_id)
            bound = CowrieBinding(
                AGENT, UUID(started.deployment_id), UUID(started.attempt_instance_id), 1, 2222
            )
            with closing(SpoolRepository(store.prepare_telemetry_path(), str(AGENT))) as spool:
                repo = CowrieJournalRepository(spool)
                repo.register(source(tmp_path, binding=bound))
                repo.append(JOURNAL, 0, line())
            service = telemetry.ExecutorTelemetry(
                Client(),
                store,
                tmp_path,
                "http://127.0.0.1:1",
                TOKEN,
                cowrie_sources=broken_provider,
            )
            service._transport = Center()
            task = asyncio.create_task(service.run())
            try:
                await asyncio.wait_for(done.wait(), 10)
            finally:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 10)
            assert statuses[0][1] == "delivered:1" and closed == [True]
            assert len(service._transport.ids) == 1

    asyncio.run(scenario())
    assert "SECRET" not in capsys.readouterr().out


def test_unsupported_cowrie_version_keeps_existing_sender_alive(tmp_path):
    db = tmp_path / "spool.db"
    with closing(SpoolRepository(db, str(AGENT))) as spool:
        repo = CowrieJournalRepository(spool)
        repo.register(source(tmp_path))
        repo.append(JOURNAL, 0, line())
        CowrieRawConverter(repo).step(BINDING, JOURNAL)
        repo.db.execute("UPDATE metadata SET value='999' WHERE key='cowrie_storage_version'")
        repo.db.commit()
    center = Center()
    with closing(LowTelemetryWorker(db, tmp_path, str(AGENT), center)) as worker:
        assert worker.step((trusted(),), (source(tmp_path),)) == (
            "UNSUPPORTED_STORAGE_VERSION",
            "delivered:1",
        )
    assert len(center.ids) == 1


def test_invalid_registry_snapshot_does_not_prevent_backlog_delivery(tmp_path):
    db = tmp_path / "spool.db"
    with closing(SpoolRepository(db, str(AGENT))) as spool:
        repo = CowrieJournalRepository(spool)
        repo.register(source(tmp_path))
        repo.append(JOURNAL, 0, line())
    center = Center()
    with closing(LowTelemetryWorker(db, tmp_path, str(AGENT), center)) as worker:
        assert worker.step((trusted(),), ("not-source",))[1] == "delivered:1"
        assert worker.step((trusted(),), ("not-source",))[0] == "INVALID_REGISTRY"
    assert len(center.ids) == 1


def test_surrogate_path_is_safe_binding_error(tmp_path):
    with pytest.raises(CowrieJournalError, match="^INVALID_BINDING$"):
        source(tmp_path, name="\ud800")
