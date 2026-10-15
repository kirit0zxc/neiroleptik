"""Постоянный сбор Low: доверенный snapshot исполнителя и отдельный worker SQLite."""

from __future__ import annotations

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from itertools import islice
from pathlib import Path
from uuid import UUID

from pydantic import SecretStr

from agent.runtime.executor.client import ClientError, HttpCommandClient
from agent.runtime.executor.store import ExecutionStore, StoreError
from agent.telemetry.low_journal.collector import LowJournalCollector
from agent.telemetry.low_journal.conversion import LowRawConverter
from agent.telemetry.low_journal.durable import RawJournalError, RawJournalRepository
from agent.telemetry.low_journal.models import JournalBinding
from agent.telemetry.low_journal.production import LowProductionMapper
from agent.telemetry.sender import BatchTransport, HttpBatchTransport, TelemetrySender
from agent.telemetry.spool import SpoolRepository


class LowTelemetryWorker:
    """Все соединения создаются, используются и закрываются в одном потоке."""

    def __init__(self, path: Path, root: Path, agent_id: str, transport: BatchTransport) -> None:
        self._raw = RawJournalRepository(path, agent_id)
        try:
            self._spool = SpoolRepository(path, agent_id)
        except BaseException:
            self._raw.close()
            raise
        self._root = root
        self._mapper = LowProductionMapper(UUID(agent_id))
        self._sender = TelemetrySender(self._spool, transport)
        self._position = 0

    def step(self, bindings: tuple[JournalBinding, ...]) -> tuple[str, str]:
        """Один источник за шаг: повреждённая цепочка не блокирует другие и sender."""
        collection = "idle"
        if bindings:
            binding = bindings[self._position % len(bindings)]
            self._position += 1
            try:
                paths = list(
                    islice(
                        (self._root / binding.deployment_id).glob(
                            f"low-{binding.instance_id}-*.jsonl"
                        ),
                        65,
                    )
                )
                result = LowJournalCollector(self._raw, binding).step(paths)
                conversion = LowRawConverter(self._spool, self._mapper, self._mapper.version).step(
                    binding
                )
                collection = result.code or result.state
                if conversion.state == "error":
                    collection = "CONVERSION_ERROR"
            except (OSError, ValueError, sqlite3.Error, RawJournalError):
                collection = "COLLECTION_ERROR"
        result_send = self._sender.step()
        delivery = result_send.error_code or result_send.state
        if result_send.accepted:
            delivery = f"delivered:{result_send.accepted}"
        return collection, delivery

    def close(self) -> None:
        """Закрытие после завершения шага; исходные журналы не удаляются."""
        self._spool.close()
        self._raw.close()


class ExecutorTelemetry:
    """Snapshot store читается в event loop, блокирующая работа — в одном worker."""

    def __init__(
        self,
        client: HttpCommandClient,
        store: ExecutionStore,
        root: Path,
        center: str,
        token: str,
    ) -> None:
        self._client = client
        self._store = store
        self._root = root
        self._transport = HttpBatchTransport(
            center.rstrip("/") + "/agent/v1/events", SecretStr(token)
        )

    async def run(self) -> None:
        """Сеть при запуске повторяется; ошибка worker видна и не останавливает команды."""
        while True:
            try:
                agent_id = (await self._client.config()).host_id
                break
            except ClientError as error:
                print(f"Телеметрия: config {error.code}", flush=True)
                await asyncio.sleep(10)
        loop = asyncio.get_running_loop()
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="low-telemetry")
        worker: LowTelemetryWorker | None = None
        previous: tuple[str, str] | None = None
        try:
            # Shield не позволяет отмене бросить работающий поток с открытой SQLite.
            creation = loop.run_in_executor(
                pool,
                LowTelemetryWorker,
                self._store.prepare_telemetry_path(),
                self._root,
                agent_id,
                self._transport,
            )
            try:
                worker = await asyncio.shield(creation)
            except asyncio.CancelledError:
                worker = await creation
                raise
            while True:
                bindings = tuple(JournalBinding(*item) for item in self._store.telemetry_bindings())
                pending = loop.run_in_executor(pool, worker.step, bindings)
                try:
                    status = await asyncio.shield(pending)
                except asyncio.CancelledError:
                    await pending
                    raise
                if status != previous:
                    print(f"Телеметрия: {status}", flush=True)
                    previous = status
                await asyncio.sleep(1)
        except (OSError, ValueError, sqlite3.Error, RawJournalError, StoreError):
            print(
                "Телеметрия остановлена: LOCAL_STORAGE_ERROR; перезапустите после исправления",
                flush=True,
            )
        finally:
            try:
                if worker is not None:
                    await loop.run_in_executor(pool, worker.close)
            finally:
                pool.shutdown(wait=True)
