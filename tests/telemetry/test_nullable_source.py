"""Обязательный source различает неизвестный источник и пропущенное поле."""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from agent.telemetry.adapters.low import LowAdapter
from agent.telemetry.schemas import Event

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("source", [None, {"ip": "2001:0db8::1", "port": 0}])
def test_source_roundtrip_schemas_and_adapter(source):
    original = json.loads((ROOT / "contracts/fixtures/telemetry/agent-event.json").read_text())
    original["source"] = source
    event = Event.model_validate(original)
    public = event.model_dump(mode="json") | {"received_at": original["occurred_at"]}
    for name, value in (
        ("agent-event-v1.schema.json", event.model_dump(mode="json")),
        ("event-v1.schema.json", public),
    ):
        Draft202012Validator(json.loads((ROOT / "contracts" / name).read_text())).validate(value)
    line = {
        key: original[key]
        for key in ("source", "occurred_at", "service", "data", "type", "artifact_ids")
    }
    adapted = LowAdapter(
        original["agent_id"], original["deployment_id"], original["instance_id"], 1
    ).parse_line((json.dumps(line) + "\n").encode(), source_id="nullable-test", offset=0)
    assert adapted.event.source == event.source
    assert Event.model_validate_json(event.canonical_json()).source == event.source


@pytest.mark.parametrize(
    "source", ["missing", {}, {"ip": "bad", "port": 1}, {"ip": "192.0.2.1", "port": True}]
)
def test_source_invalid_or_absent(source):
    value = json.loads((ROOT / "contracts/fixtures/telemetry/agent-event.json").read_text())
    if source == "missing":
        del value["source"]
    else:
        value["source"] = source
    with pytest.raises(ValidationError):
        Event.model_validate(value)


@pytest.mark.parametrize(
    "stamp", ["9999-12-31T23:59:00Z", "9999-12-31T23:59:59Z", "0001-01-01T00:00:00+23:59"]
)
def test_time_must_allow_representable_utc_minute(stamp):
    value = json.loads((ROOT / "contracts/fixtures/telemetry/agent-event.json").read_text())
    value["occurred_at"] = stamp
    with pytest.raises(ValidationError):
        Event.model_validate(value)
