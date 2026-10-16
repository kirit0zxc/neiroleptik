"""Постоянный сбор Low: доверенный snapshot исполнителя и отдельный worker SQLite."""

from __future__ import annotations

import asyncio
import random
import sqlite3
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from itertools import islice
from pathlib import Path
from uuid import UUID

from pydantic import SecretStr

from agent.runtime.executor.client import ClientError, HttpCommandClient
from agent.runtime.executor.store import ExecutionStore, StoreError
from agent.telemetry.adapters.cowrie import CowrieBinding, CowrieCollector
from agent.telemetry.cowrie_journal.collector import CowrieJournalCollector
from agent.telemetry.cowrie_journal.conversion import CowrieRawConverter
from agent.telemetry.cowrie_journal.models import CowrieJournalError, CowrieJournalSource
from agent.telemetry.cowrie_journal.repository import CowrieJournalRepository
from agent.telemetry.low_journal.collector import LowJournalCollector
from agent.telemetry.low_journal.conversion import LowRawConverter
from agent.telemetry.low_journal.durable import RawJournalError, RawJournalRepository
from agent.telemetry.low_journal.models import JournalBinding
from agent.telemetry.low_journal.production import LowProductionMapper
from agent.telemetry.sender import BatchTransport, HttpBatchTransport, TelemetrySender
from agent.telemetry.spool import SpoolRepository


class LowTelemetryWorker:
    """Все соединения создаются, используются и закрываются в одном потоке."""

    def __init__(
        self,
        path: Path,
        root: Path,
        agent_id: str,
        transport: BatchTransport,
        *,
        clock: Callable[[], float] = time.monotonic,
        jitter: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self._raw = RawJournalRepository(path, agent_id)
        try:
            self._spool = SpoolRepository(path, agent_id)
        except BaseException:
            self._raw.close()
            raise
        self._root = root
        self._cowrie: CowrieJournalRepository | None
        self._cowrie_error: str | None = None
        try:
            self._cowrie = CowrieJournalRepository(self._spool)
        except CowrieJournalError as error:
            # Неизвестная версия Cowrie не мешает прежним pending и независимому Low.
            self._cowrie = None
            self._cowrie_error = error.code
        except BaseException:
            self._spool.close()
            self._raw.close()
            raise
        self._mapper = LowProductionMapper(UUID(agent_id))
        self._sender = TelemetrySender(self._spool, transport, clock=clock, jitter=jitter)
        self._position = 0
        self._cowrie_position = 0

    def step(
        self,
        bindings: tuple[JournalBinding, ...],
        cowrie_sources: tuple[CowrieJournalSource, ...] = (),
    ) -> tuple[str, str]:
        """Один источник за шаг: повреждённая цепочка не блокирует другие и sender."""
        collection = "idle"
        trusted = {(b.deployment_id, b.instance_id, b.profile_revision) for b in bindings}
        cowrie_status = self._step_cowrie(cowrie_sources, trusted)
        if bindings:
            binding = bindings[self._position % len(bindings)]
            self._position += 1
            try:
                scope = CowrieBinding(
                    self._spool.agent_id,
                    UUID(binding.deployment_id),
                    UUID(binding.instance_id),
                    binding.profile_revision,
                    2222,
                )
                if self._cowrie is not None and not self._cowrie.claimed(scope):
                    # Legacy replay остаётся только у scope, не переключённых явно.
                    try:
                        CowrieCollector(self._spool, self._root).step(binding)
                    except (OSError, ValueError, sqlite3.Error):
                        collection = "COLLECTION_ERROR"
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
                if collection == "idle":
                    collection = result.code or result.state
                if conversion.state == "error":
                    collection = "CONVERSION_ERROR"
            except (OSError, ValueError, sqlite3.Error, RawJournalError):
                collection = "COLLECTION_ERROR"
        result_send = self._sender.step()
        delivery = result_send.error_code or result_send.state
        if result_send.accepted:
            delivery = f"delivered:{result_send.accepted}"
        return cowrie_status or collection, delivery

    def _step_cowrie(
        self,
        sources: tuple[CowrieJournalSource, ...],
        trusted: set[tuple[str, str, int]],
    ) -> str | None:
        """Один доверенный source и один сохранённый raw backlog за шаг, общий sender."""
        status = None
        if self._cowrie is None:
            return self._cowrie_error
        try:
            if (
                type(sources) is not tuple
                or len(sources) > 4096
                or any(not isinstance(item, CowrieJournalSource) for item in sources)
            ):
                sources = ()
                status = "INVALID_REGISTRY"
            if sources:
                source = sources[self._cowrie_position % len(sources)]
                self._cowrie_position += 1
                b = source.binding
                if (
                    b.agent_id != self._spool.agent_id
                    or (str(b.deployment_id), str(b.instance_id), b.profile_revision) not in trusted
                ):
                    status = "UNTRUSTED_BINDING"
                else:
                    progress = CowrieJournalCollector(self._cowrie).step(source)
                    status = progress.code or progress.state
            saved = self._cowrie.bindings()
            if saved:
                journal, binding = saved[(self._cowrie_position - 1) % len(saved)]
                if not sources:
                    self._cowrie_position += 1
                if (
                    str(binding.deployment_id),
                    str(binding.instance_id),
                    binding.profile_revision,
                ) in trusted:
                    progress = CowrieRawConverter(self._cowrie).step(binding, journal)
                    if progress.code or progress.state == "progress":
                        status = progress.code or progress.state
        except (CowrieJournalError, OSError, ValueError, sqlite3.Error):
            status = "COWRIE_STORAGE_ERROR"
        return status

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
        *,
        cowrie_sources: Callable[[], tuple[CowrieJournalSource, ...]] | None = None,
    ) -> None:
        self._client = client
        self._store = store
        self._root = root
        self._cowrie_sources = cowrie_sources
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
                if self._cowrie_sources is None:
                    pending = loop.run_in_executor(pool, worker.step, bindings)
                else:
                    try:
                        sources = self._cowrie_sources()
                    except Exception:
                        # Ошибка внешнего provider не отменяет восстановление raw и sender.
                        sources = ()
                    pending = loop.run_in_executor(pool, worker.step, bindings, sources)
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
