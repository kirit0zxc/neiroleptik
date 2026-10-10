"""Экспорт синтетических ответов сервисов для проверки настоящего StreamClient."""

import json
from pathlib import Path

from .test_read_services import record, services


def fixture():
    base = 2**53 + 1
    rows = [
        record(base, event_id=1),
        record(base + 1, event_id=1, agent=2),
        record(base + 2, event_id=3),
        record(base + 3, event_id=4),
        record(base + 4, event_id=5),
    ]
    _, history, catchup = services(rows)
    return {
        "after": str(base - 1),
        "hello": {"type": "hello", "watermark": str(base + 2)},
        "pages": [
            catchup.page(str(base - 1), str(base + 2), 1),
            catchup.page(str(base), str(base + 2), 2),
        ],
        "live": {"type": "event", **catchup.page(str(base + 2), str(base + 3))["items"][0]},
        "reconnect_hello": {"type": "hello", "watermark": str(base + 4)},
        "reconnect_page": catchup.page(str(base + 3), str(base + 4)),
        "history": history.page(history.filters()),
    }


def test_wire_fixture_matches_service_output():
    path = Path(__file__).resolve().parents[3] / "contracts/fixtures/telemetry-stream-wire.json"
    assert json.loads(path.read_text(encoding="utf-8")) == fixture()


if __name__ == "__main__":
    path = Path(__file__).resolve().parents[3] / "contracts/fixtures/telemetry-stream-wire.json"
    path.write_text(json.dumps(fixture(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
