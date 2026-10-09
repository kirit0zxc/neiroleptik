"""Векторы канонизации входного события; серверные гонки требуют PostgreSQL."""

import json
from copy import deepcopy

import pytest
from pydantic import ValidationError

from agent.telemetry.adapters.low import LowAdapter
from agent.telemetry.export_contracts import AGENT_ID, DEPLOYMENT_ID, INSTANCE_ID, example_line
from agent.telemetry.schemas import Event


def payload() -> dict:
    """Берём текущий входной Event, а не UI DTO с received_at."""
    return (
        LowAdapter(AGENT_ID, DEPLOYMENT_ID, INSTANCE_ID, 1)
        .parse_line(
            example_line(),
            source_id="canonical:g1",
            offset=0,
        )
        .event.model_dump(mode="json")
    )


def canonical(value: dict) -> bytes:
    return Event.model_validate(value).canonical_json().encode("utf-8")


def test_nested_key_order_does_not_change_canonical_bytes() -> None:
    left = payload()
    left["data"] = {"outer": {"z": 1, "a": "данные"}, "list": [{"b": 2, "a": 1}]}
    right = dict(reversed(list(left.items())))
    right["data"] = {"list": [{"a": 1, "b": 2}], "outer": {"a": "данные", "z": 1}}
    assert canonical(left) == canonical(right)


def test_whitespace_and_unicode_escape_do_not_change_canonical_bytes() -> None:
    value = payload()
    value["data"] = {"text": "Привет, мир"}
    compact = json.loads(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    escaped = json.loads(json.dumps(value, ensure_ascii=True, indent=4))
    assert canonical(compact) == canonical(escaped)


def test_data_array_order_is_significant() -> None:
    left = payload()
    left["data"] = {"commands": ["whoami", "pwd"]}
    right = deepcopy(left)
    right["data"]["commands"].reverse()
    assert canonical(left) != canonical(right)


def test_artifact_array_order_is_significant() -> None:
    left = payload()
    left["artifact_ids"] = [
        "00000000-0000-4000-8000-000000000011",
        "00000000-0000-4000-8000-000000000012",
    ]
    right = deepcopy(left)
    right["artifact_ids"].reverse()
    assert canonical(left) != canonical(right)


def test_original_secrets_differ_even_if_ui_would_mask_both() -> None:
    left = payload()
    left["data"] = {"username": "demo", "password": "fake-secret-A"}
    right = deepcopy(left)
    right["data"]["password"] = "fake-secret-B"
    assert canonical(left) != canonical(right)
    # Одинаковый UI-результат не является основанием считать оригиналы равными.
    left["data"]["password"] = right["data"]["password"] = "***"
    assert canonical(left) == canonical(right)


def test_uuid_ip_and_timezone_normalize_identically() -> None:
    left = payload()
    left["source"]["ip"] = "2001:db8::1"
    right = deepcopy(left)
    right["event_id"] = right["event_id"].upper()
    right["source"]["ip"] = "2001:0db8:0000:0000:0000:0000:0000:0001"
    right["occurred_at"] = "2026-10-09T15:00:00+03:00"
    assert canonical(left) == canonical(right)
    assert json.loads(canonical(right))["occurred_at"] == "2026-10-09T12:00:00Z"


def test_fractional_timestamp_uses_six_utc_digits() -> None:
    value = payload()
    value["occurred_at"] = "2026-10-09T15:00:00.120+03:00"
    assert json.loads(canonical(value))["occurred_at"] == "2026-10-09T12:00:00.120000Z"


def test_default_fields_have_one_canonical_representation() -> None:
    full = payload()
    omitted = deepcopy(full)
    del omitted["schema_version"]
    del omitted["session_id"]
    assert canonical(full) == canonical(omitted)


@pytest.mark.parametrize("left_number,right_number", [(1, 1.0), (0.0, -0.0)])
def test_numeric_representation_is_explicitly_significant(left_number, right_number) -> None:
    left = payload()
    left["data"] = {"number": left_number}
    right = deepcopy(left)
    right["data"]["number"] = right_number
    assert canonical(left) != canonical(right)


@pytest.mark.parametrize(
    "name,value",
    [
        ("received_at", "2026-10-09T12:00:01Z"),
        ("stream_seq", "42"),
        ("batch_id", "00000000-0000-4000-8000-000000000099"),
    ],
)
def test_server_and_envelope_fields_are_not_input_event_fields(name, value) -> None:
    value_with_extra = payload() | {name: value}
    with pytest.raises(ValidationError):
        Event.model_validate(value_with_extra)
