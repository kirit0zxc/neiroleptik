"""C01/C02/C14/C15: trusted история, живой HTTP и причинный порядок shutdown."""

import asyncio
import builtins
import subprocess
import sys
import threading
from contextlib import closing
from types import SimpleNamespace

import pytest
from continuous_low_support import (
    Receiver,
    append,
    child_env,
    command,
    complete,
    expected_events,
    journal,
    snapshot,
    worker_for,
)
from pydantic import SecretStr
from test_low_journal_reader_contract import event, footer, header, line
from test_low_production_mapper import AGENT, request
from test_sender import TOKEN

from agent.runtime.executor import telemetry
from agent.runtime.executor.store import ExecutionStore
from agent.telemetry.low_journal.models import JournalBinding
from agent.telemetry.sender import HttpBatchTransport, TransportError


class Client:
    """Только config identity; HTTP телеметрии остаётся настоящим."""

    async def config(self):
        return SimpleNamespace(host_id=AGENT)


def trusted_journal(store, root, record):
    bound = JournalBinding(record.deployment_id, record.attempt_instance_id, 1)
    path = journal(root, bound, [header(instance_id=bound.instance_id), event()])
    return bound, path


def bindings(store):
    return tuple(JournalBinding(*item) for item in store.telemetry_bindings())


def test_c01_c02_failed_unacked_start_then_ack_stop_reopen_and_footer(tmp_path):
    receiver = Receiver(tmp_path / "receiver.db")
    state_path = tmp_path / "private" / "state.db"
    with receiver.serve() as endpoint:
        with ExecutionStore(state_path) as store:
            failed = command(store)
            trusted_journal(store, tmp_path, failed)
            complete(store, failed, failed=True)
            store.acknowledge(failed.command_id)
            assert store.telemetry_bindings() == ()
            started = command(store, generation=2)
            bound, source = trusted_journal(store, tmp_path, started)
            db = store.prepare_telemetry_path()
            with closing(worker_for(db, tmp_path, endpoint)) as worker:
                assert store.telemetry_bindings() == ()
                assert worker.step(bindings(store)) == ("idle", "idle")
                complete(store, started)
                assert store.telemetry_bindings() == ()
                assert worker.step(bindings(store)) == ("idle", "idle")
                assert receiver.rows("requests") == []
                assert snapshot(db)["low_raw_records"] == []
                store.acknowledge(started.command_id)
                assert bindings(store) == (bound,)
                assert worker.step(bindings(store))[1] == "delivered:1"
                stopped = command(store, action="stop", generation=3, instance=bound.instance_id)
                complete(store, stopped)
                store.acknowledge(stopped.command_id)
                append(source, footer())
                assert bindings(store) == (bound,)
        with (
            ExecutionStore(state_path) as store,
            closing(worker_for(db, tmp_path, endpoint)) as worker,
        ):
            assert bindings(store) == (bound,)
            assert worker.step(bindings(store))[1] == "idle"
            state = snapshot(db)
            assert [row[-1] for row in state["low_raw_records"]] == source.read_bytes().splitlines(
                True
            )
            assert [row[4] for row in state["low_conversion_ledger"]] == [
                "header",
                "event",
                "footer",
            ]
            assert state["low_raw_generations"][0][2] == source.stat().st_size
            assert worker.step(bindings(store))[1] == "idle"
            assert snapshot(db) == state
        assert receiver.events() == expected_events(source, bound)
        assert len(receiver.rows("requests")) == 1


async def wait_event(event):
    assert await asyncio.wait_for(asyncio.to_thread(event.wait, 5), 6), "Не достигнут барьер"


def test_c14_real_http_deadline_allows_control_coroutine_and_retains_queue(tmp_path, monkeypatch):
    receiver = Receiver(tmp_path / "receiver.db")
    receiver.mode = "hold"
    stepped = threading.Event()
    statuses = []
    real_worker = telemetry.LowTelemetryWorker

    class ObservedWorker(real_worker):
        def step(self, bound):
            status = super().step(bound)
            statuses.append(status)
            stepped.set()
            return status

    monkeypatch.setattr(telemetry, "LowTelemetryWorker", ObservedWorker)
    with receiver.serve() as endpoint:

        async def scenario():
            with ExecutionStore(tmp_path / "private" / "state.db") as store:
                started = command(store)
                bound, source = trusted_journal(store, tmp_path, started)
                complete(store, started)
                store.acknowledge(started.command_id)
                service = telemetry.ExecutorTelemetry(
                    Client(), store, tmp_path, endpoint.rsplit("/agent/", 1)[0], TOKEN
                )
                service._transport = HttpBatchTransport(endpoint, SecretStr(TOKEN), timeout=0.5)
                task = asyncio.create_task(service.run())
                try:
                    await wait_event(receiver.entered)

                    async def control():
                        assert not stepped.is_set()
                        assert not receiver.release.is_set()
                        return "CONTROL_COMPLETED"

                    assert (
                        await asyncio.wait_for(asyncio.create_task(control()), 0.2)
                        == "CONTROL_COMPLETED"
                    )
                    await wait_event(stepped)
                    assert statuses == [("progress", "TRANSPORT_ERROR")]
                    db = store.prepare_telemetry_path()
                    state = snapshot(db)
                    assert {row[1]: row[2].encode() for row in state["pending"]} == expected_events(
                        source, bound
                    )
                finally:
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await asyncio.wait_for(task, 5)
                    receiver.release.set()
                return db, bound, source

        db, bound, source = asyncio.run(scenario())
    receiver.mode = "accepted"
    with receiver.serve() as endpoint, closing(worker_for(db, tmp_path, endpoint)) as worker:
        assert worker.step((bound,))[1] == "delivered:1"
    assert receiver.events() == expected_events(source, bound)
    assert not snapshot(db)["pending"]


def owner_probe(path):
    script = """
import sys
from agent.runtime.executor.store import ExecutionStore, StoreError
try:
    with ExecutionStore(sys.argv[1]):
        print('OPENED')
except StoreError as error:
    print(error.code)
"""
    process = subprocess.run(
        [sys.executable, "-c", script, str(path)],
        env=child_env(),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert process.returncode == 0, process.stderr
    return process.stdout.strip()


@pytest.mark.parametrize("phase", ["constructor", "step"])
def test_c15_cancel_waits_before_store_release_and_closes_on_worker_thread(
    tmp_path, monkeypatch, phase
):
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    threads = []

    class ControlledWorker:
        """Барьер удерживает выбранную операцию; после отмены она обязана завершиться."""

        def __init__(self, *args):
            threads.append(threading.get_ident())
            if phase == "constructor":
                entered.set()
                assert release.wait(10)

        def step(self, bound):
            threads.append(threading.get_ident())
            entered.set()
            assert release.wait(10)
            return ("idle", "idle")

        def close(self):
            threads.append(threading.get_ident())
            closed.set()

    monkeypatch.setattr(telemetry, "LowTelemetryWorker", ControlledWorker)
    path = tmp_path / "private" / "state.db"

    async def scenario():
        with ExecutionStore(path) as store:
            service = telemetry.ExecutorTelemetry(
                Client(), store, tmp_path, "http://127.0.0.1:1", TOKEN
            )
            task = asyncio.create_task(service.run())
            try:
                await wait_event(entered)
                task.cancel()
                # call_soon барьер пропускает обработку cancel, без временной догадки sleep.
                observed = asyncio.Event()
                asyncio.get_running_loop().call_soon(observed.set)
                await observed.wait()
                assert not task.done() and not closed.is_set()
                assert await asyncio.to_thread(owner_probe, path) == "STORE_LOCKED"
            finally:
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 5)
            assert closed.is_set()
            assert owner_probe(path) == "STORE_LOCKED"
        assert owner_probe(path) == "OPENED"

    asyncio.run(scenario())
    assert len(set(threads)) == 1
    assert threads[0] != threading.get_ident()


def test_c16_executor_prints_codes_without_private_input(tmp_path, monkeypatch, capsys):
    token = "hf_agent_TOKEN_SECRET"
    receiver = Receiver(tmp_path / "receiver.db")
    receiver.mode = "invalid"
    printed = asyncio.Event()

    def observe_print(*args, **kwargs):
        builtins.print(*args, **kwargs)
        printed.set()

    class BrokenTransport:
        def post(self, payload):
            raise TransportError("TRANSPORT_SECRET Authorization private/path")

    monkeypatch.setattr(telemetry, "print", observe_print, raising=False)

    async def scenario(endpoint):
        with ExecutionStore(tmp_path / "private" / "state.db") as store:
            started = command(store)
            bound, source = trusted_journal(store, tmp_path, started)
            source.write_bytes(
                line(header(instance_id=bound.instance_id))
                + line(request(b"GET /PAYLOAD_SECRET?token=QUERY_SECRET HTTP/1.1\r\n\r\n"))
            )
            complete(store, started)
            store.acknowledge(started.command_id)
            for code in ("INVALID_ACK", "TRANSPORT_ERROR"):
                printed.clear()
                service = telemetry.ExecutorTelemetry(
                    Client(), store, tmp_path, endpoint.rsplit("/agent/", 1)[0], token
                )
                if code == "TRANSPORT_ERROR":
                    service._transport = BrokenTransport()
                task = asyncio.create_task(service.run())
                try:
                    await asyncio.wait_for(printed.wait(), 5)
                finally:
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
            return source

    with receiver.serve() as endpoint:
        source = asyncio.run(scenario(endpoint))
    output = capsys.readouterr()
    public = output.out + output.err
    assert "INVALID_ACK" in public and "TRANSPORT_ERROR" in public
    for secret in (
        token,
        "PAYLOAD_SECRET",
        "QUERY_SECRET",
        "ACK_SECRET",
        "TRANSPORT_SECRET",
        "Authorization",
        str(source),
        "payload_b64",
    ):
        assert secret not in public
