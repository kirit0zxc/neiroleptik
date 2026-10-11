"""Ограниченный SQLite/WAL стенд; приёмник синтетический, не PostgreSQL."""

import argparse
import json
import math
import platform
import shutil
import sqlite3
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4, uuid5

import pydantic

from agent.telemetry.adapters.low import SourceRecord
from agent.telemetry.schemas import Ack, Batch, Event, RejectedEvent
from agent.telemetry.sender import HttpResponse, TelemetrySender, TransportError
from agent.telemetry.spool import SpoolRepository


@dataclass(frozen=True)
class BenchmarkConfig:
    """Жёсткий бюджет исключает неограниченную нагрузку на диск и время."""

    count: int = 200
    payload_bytes: int = 1024
    ingress_rate: float = 1000
    outage_seconds: float = 0.2
    receive_rate: float = 10000
    receiver_mode: Literal["accept", "lose_once"] = "accept"
    max_seconds: float = 30
    seed: int = 1

    def __post_init__(self) -> None:
        if (
            type(self.count) is not int
            or not 1 <= self.count <= 10000
            or type(self.payload_bytes) is not int
            or not 0 <= self.payload_bytes <= 7500
            or type(self.seed) is not int
            or not 0 <= self.seed < 2**32
            or self.count * (self.payload_bytes + 1024) > 64 * 1024 * 1024
            or self.receiver_mode not in {"accept", "lose_once"}
        ):
            raise ValueError("Параметры превышают бюджет стенда")
        for value, low, high in (
            (self.ingress_rate, 1, 10000),
            (self.receive_rate, 1, 100000),
            (self.outage_seconds, 0, 30),
            (self.max_seconds, 0.01, 120),
        ):
            if isinstance(value, bool) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError("Неверный временной или скоростной бюджет")


class SyntheticReceiver:
    """Измерительный ACK-приёмник; точные canonical bytes служат проверкой дедупликации."""

    def __init__(
        self,
        config: BenchmarkConfig,
        start: float,
        clock: Callable[[], float],
        sleep: Callable[[float], None],
    ) -> None:
        self.config, self.start, self.clock, self.sleep = config, start, clock, sleep
        self.originals: dict[str, str] = {}
        self.lost = False

    def post(self, payload: bytes) -> HttpResponse:
        if self.clock() < self.start + self.config.outage_seconds:
            return HttpResponse(503, b"")
        batch = Batch.model_validate_json(payload)
        limit = min(len(batch.events), max(1, int(self.config.receive_rate)))
        self.sleep(limit / self.config.receive_rate)
        accepted, rejected = [], []
        for index, event in enumerate(batch.events):
            if index >= limit:
                rejected.append(
                    RejectedEvent(
                        index=index,
                        event_id=event.event_id,
                        code="TEMPORARILY_UNAVAILABLE",
                        retryable=True,
                    )
                )
                continue
            key = str(event.event_id)
            canonical = event.canonical_json()
            if key in self.originals and self.originals[key] != canonical:
                raise ValueError("BENCHMARK_ID_CONFLICT")
            self.originals[key] = canonical
            accepted.append(event.event_id)
        if self.config.receiver_mode == "lose_once" and not self.lost:
            self.lost = True
            raise TransportError("Синтетическая потеря ответа")
        return HttpResponse(
            200,
            Ack(batch_id=batch.batch_id, accepted_ids=accepted, rejected=rejected)
            .model_dump_json()
            .encode(),
        )


class SQLiteBenchmark:
    """Нагрузка использует существующие append и sender; не управляет runtime."""

    def __init__(
        self,
        config: BenchmarkConfig,
        directory: Path,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config, self.directory, self.clock, self.sleep = config, directory, clock, sleep

    def _event(self, index: int) -> Event:
        namespace = UUID(int=self.config.seed)
        return Event.model_validate(
            {
                "schema_version": 1,
                "event_id": str(uuid5(namespace, str(index))),
                "agent_id": str(UUID(int=1)),
                "deployment_id": str(UUID(int=2)),
                "instance_id": str(UUID(int=3)),
                "profile_revision": 1,
                "source_seq": index,
                "occurred_at": "2026-10-11T00:00:00Z",
                "type": "runtime.state",
                "source": {"ip": "127.0.0.1", "port": 0},
                "service": {"protocol": "http", "port": 8080},
                "data": {"test_padding": "x" * self.config.payload_bytes},
                "artifact_ids": [],
            }
        )

    def run(self) -> dict[str, Any]:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / ("benchmark_" + uuid4().hex + ".sqlite3")
        start = self.clock()
        receiver = SyntheticReceiver(self.config, start, self.clock, self.sleep)
        spool = SpoolRepository(path, str(UUID(int=1)))
        sender = TelemetrySender(spool, receiver, clock=self.clock, jitter=lambda low, high: low)
        expected: dict[str, str] = {}
        committed: dict[str, float] = {}
        latencies: list[float] = []
        peaks = {"db": 0, "wal": 0, "shm": 0, "event_text": 0, "snapshot_blob": 0}
        backlog = produced = 0
        ingress_end = start
        free_min = shutil.disk_usage(self.directory).free
        code = None

        def measure() -> None:
            nonlocal backlog, free_min
            backlog = max(backlog, spool.count())
            for name, suffix in (("db", ""), ("wal", "-wal"), ("shm", "-shm")):
                file = Path(str(path) + suffix)
                peaks[name] = max(peaks[name], file.stat().st_size if file.exists() else 0)
            for name, table in (("event_text", "pending"), ("snapshot_blob", "batch_snapshots")):
                value = spool.db.execute(
                    f"SELECT COALESCE(SUM(length(CAST(payload AS BLOB))),0) FROM {table}"
                ).fetchone()[0]
                peaks[name] = max(peaks[name], value)
            free_min = min(free_min, shutil.disk_usage(self.directory).free)

        try:
            while self.clock() - start < self.config.max_seconds:
                elapsed = self.clock() - start
                if produced < self.config.count and elapsed >= produced / self.config.ingress_rate:
                    event = self._event(produced)
                    spool.append(SourceRecord("synthetic-benchmark", produced, produced + 1, event))
                    key = str(event.event_id)
                    expected[key] = event.canonical_json()
                    committed[key] = self.clock()
                    produced += 1
                    ingress_end = self.clock()
                    measure()
                result = sender.step()
                if result.accepted:
                    remaining = {row[0] for row in spool.db.execute("SELECT event_id FROM pending")}
                    for key in list(committed):
                        if key not in remaining:
                            latencies.append(self.clock() - committed.pop(key))
                measure()
                if produced == self.config.count and spool.count() == 0:
                    break
                self.sleep(min(0.001, 1 / self.config.ingress_rate))
            if produced != self.config.count or spool.count():
                code = "DEADLINE"
            elif expected != receiver.originals:
                code = "PAYLOAD_MISMATCH"
        except (sqlite3.Error, ValueError):
            code = "WRITE_OR_VALIDATION_ERROR"
        finally:
            elapsed = self.clock() - start
            pending = spool.count()
            spool.close()
        ordered = sorted(latencies)
        drain_seconds = max(0.0, elapsed - self.config.outage_seconds)
        return {
            "status": "passed" if code is None else "failed",
            "code": code,
            "synthetic_receiver": True,
            "parameters": asdict(self.config),
            "environment": {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "sqlite": sqlite3.sqlite_version,
                "pydantic": pydantic.__version__,
            },
            "spool_path": str(path.resolve()),
            "produced": produced,
            "unique_received": len(receiver.originals),
            "pending": pending,
            "max_backlog": backlog,
            "elapsed_seconds": elapsed,
            "drain_seconds_after_outage": drain_seconds,
            "ingress_events_per_second": produced / max(ingress_end - start, 1e-9),
            "drain_events_per_second": len(latencies) / max(drain_seconds, 1e-9),
            "peak_bytes": {**peaks, "raw_blob": 0},
            "min_free_disk_bytes": free_min,
            "latency_seconds": {
                "max": max(ordered, default=0),
                "p95": ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)] if ordered else None,
            },
            "measurement_boundaries": "commit append -> применение accepted ACK; drain от конца "
            "настроенного outage до пустого pending, включая продолжающийся ingress",
            "not_measured": ["real_ENOSPC", "runtime_network_losses_before_append", "PostgreSQL"],
        }


def main() -> int:
    """CLI сохраняет безопасный машинный отчёт; ошибку не выдаёт за успешный замер."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    for name, kind, default in (
        ("count", int, 200),
        ("payload_bytes", int, 1024),
        ("ingress_rate", float, 1000),
        ("outage_seconds", float, 0.2),
        ("receive_rate", float, 10000),
        ("max_seconds", float, 30),
        ("seed", int, 1),
    ):
        parser.add_argument("--" + name.replace("_", "-"), type=kind, default=default)
    parser.add_argument("--receiver-mode", choices=["accept", "lose_once"], default="accept")
    args = vars(parser.parse_args())
    directory, report = args.pop("directory"), args.pop("report")
    try:
        result = SQLiteBenchmark(BenchmarkConfig(**args), directory).run()
    except (OSError, ValueError, sqlite3.Error):
        result = {"status": "failed", "code": "SETUP_OR_BUDGET_ERROR"}
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
