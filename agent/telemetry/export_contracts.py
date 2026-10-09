"""Экспорт входных контрактов агента и синтетических примеров D1."""

import argparse
import base64
import json
from pathlib import Path
from uuid import UUID

from agent.telemetry.adapters.low import LowAdapter
from agent.telemetry.schemas import Ack, Batch, Event, RejectedEvent

ROOT = Path(__file__).resolve().parents[2]
AGENT_ID = "00000000-0000-4000-8000-000000000001"
DEPLOYMENT_ID = "00000000-0000-4000-8000-000000000002"
INSTANCE_ID = "00000000-0000-4000-8000-000000000003"


def example_line() -> bytes:
    """Только тестовый IP и безвредный HTTP-запрос, не реальное наблюдение."""
    return (
        json.dumps(
            {
                "occurred_at": "2026-10-09T12:00:00Z",
                "type": "http.request",
                "source": {"ip": "192.0.2.23", "port": 51342},
                "service": {"protocol": "http", "port": 8080},
                "data": {"method": "GET", "path": "/", "truncated": False},
            },
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")


def build_examples() -> dict[str, object]:
    """Детерминированный набор для проверки отсутствия дрейфа контракта."""
    event = (
        LowAdapter(AGENT_ID, DEPLOYMENT_ID, INSTANCE_ID, 1)
        .parse_line(
            example_line(),
            source_id="example-low:g1",
            offset=0,
        )
        .event
    )
    batch = Batch(batch_id=UUID(int=10), events=[event])
    ack = Ack(batch_id=batch.batch_id, accepted_ids=[event.event_id], rejected=[])
    mixed_events = [event] + [
        Event.model_validate(
            event.model_dump(mode="python") | {"event_id": UUID(int=number), "source_seq": number}
        )
        for number in (11, 12)
    ]
    mixed_batch = Batch(batch_id=UUID(int=13), events=mixed_events)
    mixed_ack = Ack(
        batch_id=mixed_batch.batch_id,
        accepted_ids=[event.event_id],
        rejected=[
            RejectedEvent(
                index=1,
                event_id=mixed_events[1].event_id,
                code="EVENT_ID_CONFLICT",
                retryable=False,
            ),
            RejectedEvent(
                index=2,
                event_id=mixed_events[2].event_id,
                code="TEMPORARILY_UNAVAILABLE",
                retryable=True,
            ),
        ],
    )
    permanent_ack = Ack(
        batch_id=batch.batch_id,
        accepted_ids=[],
        rejected=[
            RejectedEvent(
                index=0, event_id=event.event_id, code="DEPLOYMENT_MISMATCH", retryable=False
            )
        ],
    )
    invalid_id_ack = Ack(
        batch_id=batch.batch_id,
        accepted_ids=[],
        rejected=[RejectedEvent(index=0, event_id=None, code="INVALID_EVENT", retryable=False)],
    )
    # Структурно правильный, но чужой ID: отрицательный пример для проверки batch.
    unknown_id_ack = Ack(batch_id=batch.batch_id, accepted_ids=[UUID(int=99)], rejected=[])
    ui_event = event.model_dump(mode="json") | {"received_at": "2026-10-09T12:00:01Z"}
    return {
        "agent-event-v1.schema.json": Event.model_json_schema(),
        "agent-batch-v1.schema.json": Batch.model_json_schema(),
        "agent-ack-v1.schema.json": Ack.model_json_schema(),
        "fixtures/telemetry/agent-event.json": event.model_dump(mode="json"),
        "fixtures/telemetry/batch.json": batch.model_dump(mode="json"),
        "fixtures/telemetry/ack.json": ack.model_dump(mode="json"),
        "fixtures/telemetry/batch-mixed.json": mixed_batch.model_dump(mode="json"),
        "fixtures/telemetry/ack-mixed.json": mixed_ack.model_dump(mode="json"),
        "fixtures/telemetry/ack-permanent.json": permanent_ack.model_dump(mode="json"),
        "fixtures/telemetry/ack-invalid-event-id.json": invalid_id_ack.model_dump(mode="json"),
        "fixtures/telemetry/ack-unknown-event-id.json": unknown_id_ack.model_dump(mode="json"),
        "fixtures/telemetry/ui-event.json": ui_event,
        "fixtures/telemetry/ws-hello.json": {"type": "hello", "watermark": "0"},
        "fixtures/telemetry/ws-event.json": {"type": "event", "stream_seq": "1", "event": ui_event},
        "fixtures/telemetry/replay-chunk.json": {
            "seq": 0,
            "offset_ms": 0,
            "direction": "out",
            "data_base64": base64.b64encode(b"demo terminal\r\n").decode("ascii"),
            "truncated": False,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Экспорт контрактов телеметрии")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    expected = {
        name: json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        for name, value in build_examples().items()
    }
    expected["fixtures/telemetry/low.jsonl"] = example_line().decode("utf-8")
    for name, text in expected.items():
        path = ROOT / "contracts" / name
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                raise SystemExit(f"Контракт устарел: {path}")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
    print("Контракты и примеры актуальны")


if __name__ == "__main__":
    main()
