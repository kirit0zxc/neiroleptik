"""Сетевой/дисковый worker не задерживает loop и завершается раньше снятия замка."""

import asyncio
import threading
from types import SimpleNamespace

import pytest

from agent.runtime.executor import telemetry
from agent.runtime.executor.store import ExecutionStore


def test_cancellation_waits_for_worker_and_closes_on_same_thread(tmp_path, monkeypatch):
    started = threading.Event()
    release = threading.Event()
    closed = threading.Event()
    threads = []

    class Client:
        """Config возвращает идентичность из доверенного ответа центра."""

        async def config(self):
            return SimpleNamespace(host_id="00000000-0000-4000-8000-000000000000")

    class Worker:
        """Долгий шаг моделирует HTTP: отмена не должна освободить store раньше него."""

        def __init__(self, *args):
            threads.append(threading.get_ident())

        def step(self, bindings):
            threads.append(threading.get_ident())
            started.set()
            assert release.wait(5)
            return ("idle", "idle")

        def close(self):
            threads.append(threading.get_ident())
            closed.set()

    monkeypatch.setattr(telemetry, "LowTelemetryWorker", Worker)

    async def scenario():
        with ExecutionStore(tmp_path / "private" / "state.db") as store:
            service = telemetry.ExecutorTelemetry(
                Client(), store, tmp_path, "http://127.0.0.1:8000", "hf_agent_test"
            )
            task = asyncio.create_task(service.run())
            try:
                async with asyncio.timeout(5):
                    while not started.is_set():
                        await asyncio.sleep(0.01)
                task.cancel()
                await asyncio.sleep(0.02)
                assert not task.done()
                assert not closed.is_set()
            finally:
                release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert closed.is_set()
        assert len(set(threads)) == 1
        assert threads[0] != threading.get_ident()

    asyncio.run(scenario())
