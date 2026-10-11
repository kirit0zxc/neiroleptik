"""Малый воспроизводимый стенд проверяет поведение, без целевых обещаний throughput."""

import pytest

from agent.telemetry.benchmark import BenchmarkConfig, SQLiteBenchmark


class VirtualClock:
    def __init__(self):
        self.now = 0.0

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.mark.parametrize("mode", ["accept", "lose_once"])
def test_outage_restore_exact_payloads_and_metrics(tmp_path, mode):
    clock = VirtualClock()
    report = SQLiteBenchmark(
        BenchmarkConfig(count=10, payload_bytes=64, outage_seconds=0.1, receiver_mode=mode),
        tmp_path,
        clock.clock,
        clock.sleep,
    ).run()
    assert report["status"] == "passed"
    assert report["unique_received"] == report["produced"] == 10
    assert report["pending"] == 0 and report["max_backlog"] == 10
    assert report["peak_bytes"]["wal"] > 0 and report["peak_bytes"]["snapshot_blob"] > 0
    assert report["latency_seconds"]["max"] >= 0.1


def test_deadline_is_failure_with_preserved_pending(tmp_path):
    clock = VirtualClock()
    report = SQLiteBenchmark(
        BenchmarkConfig(count=3, outage_seconds=1, max_seconds=0.02),
        tmp_path,
        clock.clock,
        clock.sleep,
    ).run()
    assert report["status"] == "failed" and report["code"] == "DEADLINE"
    assert report["pending"] == 3


@pytest.mark.parametrize(
    "parameters",
    [
        {"count": 10001},
        {"payload_bytes": 9000},
        {"count": 10000, "payload_bytes": 7500},
        {"max_seconds": float("inf")},
        {"seed": True},
    ],
)
def test_hard_budgets(parameters):
    with pytest.raises(ValueError):
        BenchmarkConfig(**parameters)
