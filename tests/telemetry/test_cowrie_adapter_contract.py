"""Контракт чистого адаптера; реальные Cowrie файлы не подменяются."""

import importlib
import json
import traceback
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

import jsonschema
import pytest

from agent.telemetry.schemas import Event

ROOT = Path(__file__).resolve().parents[2]
AGENT = UUID("00000000-0000-4000-8000-000000000000")
DEPLOYMENT = UUID("00000000-0000-4000-8000-000000000001")
INSTANCE = UUID("00000000-0000-4000-8000-000000000002")
JOURNAL = UUID("00000000-0000-4000-8000-000000000003")


def identity(*parts):
    """Эталон из текста контракта, без вызова production UUID helper."""
    return uuid5(NAMESPACE_URL, json.dumps(parts, ensure_ascii=False, separators=(",", ":")))


def record(**changes):
    return {
        "eventid": "cowrie.command.input",
        "session": "session-a",
        "timestamp": "2026-10-15T09:00:00.123456Z",
        "src_ip": "192.0.2.10",
        "src_port": 40000,
        "input": "echo привет",
        **changes,
    }


def line(value):
    return json.dumps(value, ensure_ascii=False).encode() + b"\n"


def expected_event():
    return {
        "schema_version": 1,
        "agent_id": str(AGENT),
        "deployment_id": str(DEPLOYMENT),
        "instance_id": str(INSTANCE),
        "profile_revision": 7,
        "event_id": str(
            identity(
                str(AGENT), str(DEPLOYMENT), str(INSTANCE), "cowrie-jsonl/1", str(JOURNAL), 512
            )
        ),
        "session_id": str(
            identity(str(AGENT), str(DEPLOYMENT), str(INSTANCE), "cowrie-session/1", "session-a")
        ),
        "source_seq": 512,
        "occurred_at": "2026-10-15T09:00:00.123456Z",
        "source": {"ip": "192.0.2.10", "port": 40000},
        "service": {"protocol": "ssh", "port": 2222},
        "type": "session.command",
        "data": {"input": "echo привет"},
        "artifact_ids": [],
    }


def test_expected_vector_fits_existing_agent_schema():
    """Проверка совместимости ожидаемого DTO, а не готовности адаптера."""
    value = expected_event()
    Event.model_validate(value)
    schema = json.loads((ROOT / "contracts/agent-event-v1.schema.json").read_text("utf-8"))
    jsonschema.Draft202012Validator(schema).validate(value)


@pytest.fixture
def api():
    try:
        return importlib.import_module("agent.telemetry.adapters.cowrie")
    except ModuleNotFoundError as exc:
        if exc.name != "agent.telemetry.adapters.cowrie":
            raise
        pytest.fail("TDD: требуется реализация agent.telemetry.adapters.cowrie", pytrace=False)


@pytest.fixture
def binding(api):
    return api.CowrieBinding(
        agent_id=AGENT,
        deployment_id=DEPLOYMENT,
        instance_id=INSTANCE,
        profile_revision=7,
        service_port=2222,
    )


def mapped(api, binding, value=None, *, raw=None, offset=512, journal_id=JOURNAL):
    return api.CowrieAdapter().map_record(
        line(record() if value is None else value) if raw is None else raw,
        binding=binding,
        journal_id=journal_id,
        offset=offset,
    )


def test_exact_command_projection(api, binding):
    assert mapped(api, binding).model_dump(mode="json") == expected_event()


@pytest.mark.parametrize(
    "eventid,extra,kind,data",
    [
        ("cowrie.session.connect", {}, "connection.opened", {}),
        (
            "cowrie.login.failed",
            {"username": "root", "password": ""},
            "auth.attempt",
            {"username": "root", "password": "", "success": False},
        ),
        (
            "cowrie.login.success",
            {"username": "root", "password": "SECRET"},
            "auth.attempt",
            {"username": "root", "password": "SECRET", "success": True},
        ),
        ("cowrie.command.input", {"input": ""}, "session.command", {"input": ""}),
        ("cowrie.session.closed", {}, "session.closed", {}),
    ],
)
def test_supported_types(api, binding, eventid, extra, kind, data):
    result = mapped(api, binding, record(eventid=eventid, **extra))
    assert result.type == kind
    assert result.data == data


def test_unknown_valid_type_is_ignored(api, binding):
    assert mapped(api, binding, record(eventid="cowrie.client.version")) is None


def test_untrusted_metadata_does_not_override_binding_or_leak(api, binding):
    result = mapped(
        api,
        binding,
        record(
            agent_id=str(JOURNAL),
            deployment_id=str(JOURNAL),
            instance_id=str(JOURNAL),
            profile_revision=999,
            service={"protocol": "tcp", "port": 1},
            password="SECRET",
            ttylog="../../SECRET",
            message="SECRET",
        ),
    )
    assert result.model_dump(mode="json") == expected_event()


def test_retry_rotation_offset_and_session_identity(api, binding):
    first = mapped(api, binding)
    assert mapped(api, binding).canonical_json() == first.canonical_json()
    next_position = mapped(api, binding, offset=513)
    rotated = mapped(api, binding, journal_id=UUID(int=9))
    another_session = mapped(api, binding, record(session="session-b"))
    assert len({first.event_id, next_position.event_id, rotated.event_id}) == 3
    assert first.session_id == next_position.session_id == rotated.session_id
    assert another_session.session_id != first.session_id


def test_new_instance_separates_event_and_session(api, binding):
    other = api.CowrieBinding(
        agent_id=AGENT,
        deployment_id=DEPLOYMENT,
        instance_id=UUID(int=99),
        profile_revision=7,
        service_port=2222,
    )
    assert mapped(api, binding).event_id != mapped(api, other).event_id
    assert mapped(api, binding).session_id != mapped(api, other).session_id


@pytest.mark.parametrize("pair", [{}, {"src_ip": None, "src_port": None}])
def test_unknown_source_is_explicit_null(api, binding, pair):
    value = record()
    del value["src_ip"], value["src_port"]
    value.update(pair)
    assert mapped(api, binding, value).source is None


def test_ipv6_normalization(api, binding):
    result = mapped(api, binding, record(src_ip="2001:0db8:0000::1", src_port=0))
    assert result.source.model_dump() == {"ip": "2001:db8::1", "port": 0}


@pytest.mark.parametrize(
    "changes",
    [
        {"src_ip": None},
        {"src_port": None},
        {"src_ip": "no-ip"},
        {"src_port": True},
        {"src_port": "22"},
        {"src_port": -1},
        {"src_port": 65536},
        {"session": ""},
        {"session": 9},
        {"session": "я" * 65},
        {"eventid": None},
        {"timestamp": "2026-10-15T09:00:00"},
        {"timestamp": 1},
        {"timestamp": "9999-12-31T23:59:59Z"},
        {"input": {"SECRET": "hidden"}},
        {"input": None},
        {"eventid": "cowrie.login.failed", "username": "root"},
        {"eventid": "unknown", "session": ""},
    ],
)
def test_invalid_fields_are_not_silently_coerced(api, binding, changes):
    with pytest.raises(api.CowrieMappingError) as caught:
        mapped(api, binding, record(**changes))
    assert caught.value.code == "INVALID_RECORD"
    assert "SECRET" not in str(caught.value)
    assert "SECRET" not in repr(caught.value)


@pytest.mark.parametrize(
    "raw",
    [
        b"{}",
        b"{}\n{}\n",
        b"[]\n",
        b"\xff\n",
        b'{"a":NaN}\n',
        b'{"a":1,"a":2}\n',
        b'{"nested":{"a":1,"a":2}}\n',
        b'{"session":"\\ud800"}\n',
    ],
)
def test_malformed_jsonl(api, binding, raw):
    with pytest.raises(api.CowrieMappingError) as caught:
        mapped(api, binding, raw=raw)
    assert caught.value.code == "INVALID_RECORD"


@pytest.mark.parametrize("offset", [-1, True, "1", 9007199254740992])
def test_position_rejected(api, binding, offset):
    with pytest.raises(api.CowrieMappingError) as caught:
        mapped(api, binding, offset=offset)
    assert caught.value.code == "INVALID_POSITION"


def test_record_limit_before_parsing(api, binding):
    with pytest.raises(api.CowrieMappingError) as caught:
        mapped(api, binding, raw=b" " * 65536 + b"\n")
    assert caught.value.code == "RECORD_LIMIT"


def test_data_limit_counts_utf8_and_preserves_exact_boundary(api, binding):
    # {"input":""} занимает 12 bytes, ещё 8180 доступны для содержимого.
    text = "я" * 4090
    assert mapped(api, binding, record(input=text)).data == {"input": text}
    with pytest.raises(api.CowrieMappingError) as caught:
        mapped(api, binding, record(input=text + "a"))
    assert caught.value.code == "DATA_LIMIT"


@pytest.mark.parametrize(
    "field,value",
    [
        ("profile_revision", True),
        ("profile_revision", 0),
        ("service_port", True),
        ("service_port", 0),
        ("service_port", 65536),
    ],
)
def test_binding_validation(api, field, value):
    values = dict(
        agent_id=AGENT,
        deployment_id=DEPLOYMENT,
        instance_id=INSTANCE,
        profile_revision=7,
        service_port=2222,
    )
    values[field] = value
    with pytest.raises(ValueError):
        api.CowrieBinding(**values)


@pytest.mark.parametrize("field", ["agent_id", "deployment_id", "instance_id"])
@pytest.mark.parametrize("value", [None, True, "not-uuid", str(AGENT)])
def test_binding_requires_uuid_objects(api, binding, field, value):
    with pytest.raises(ValueError):
        replace(binding, **{field: value})


def test_binding_is_frozen_and_revision_does_not_change_identity(api, binding):
    with pytest.raises(FrozenInstanceError):
        binding.profile_revision = 8
    first = mapped(api, binding)
    other = mapped(api, replace(binding, profile_revision=8, service_port=22))
    assert other.event_id == first.event_id and other.session_id == first.session_id
    assert other.profile_revision == 8 and other.service.port == 22


@pytest.mark.parametrize("offset", [0, 9007199254740991])
def test_position_inclusive_boundaries(api, binding, offset):
    assert mapped(api, binding, offset=offset).source_seq == offset


@pytest.mark.parametrize("value", [None, True, str(JOURNAL)])
def test_journal_requires_uuid(api, binding, value):
    with pytest.raises(api.CowrieMappingError, match="^INVALID_POSITION$"):
        mapped(api, binding, journal_id=value)


@pytest.mark.parametrize("field", ["src_ip", "src_port"])
def test_single_missing_null_source_field_is_invalid(api, binding, field):
    value = record(src_ip=None, src_port=None)
    del value[field]
    with pytest.raises(api.CowrieMappingError, match="^INVALID_RECORD$"):
        mapped(api, binding, value)


@pytest.mark.parametrize(
    "changes",
    [
        {"timestamp": "2026-10-15T09:00:00+0300"},
        {"timestamp": "2026-10-15T09:00:00.1234567Z"},
        {"timestamp": "2026-02-30T09:00:00Z"},
        {"timestamp": None},
        {"eventid": "я" * 65},
        {"session": ""},
        {"src_port": True},
    ],
)
def test_unknown_event_must_validate_common_fields(api, binding, changes):
    with pytest.raises(api.CowrieMappingError, match="^INVALID_RECORD$"):
        mapped(api, binding, record(**{"eventid": "unknown", **changes}))


def test_timezone_normalization_and_session_utf8_boundary(api, binding):
    result = mapped(
        api, binding, record(timestamp="2026-10-15T12:00:00.123456+03:00", session="я" * 64)
    )
    assert result.model_dump(mode="json")["occurred_at"] == expected_event()["occurred_at"]


@pytest.mark.parametrize(
    "extra",
    [
        b'"extra":Infinity',
        b'"extra":-Infinity',
        b'"extra":1e999',
        b'"extra":[{"a":1,"a":2}]',
        b'"extra":"\\ud800"',
        b'"\\udfff":0',
        b'"extra":' + b"[" * 20000 + b"0" + b"]" * 20000,
    ],
    ids=["infinity", "negative-infinity", "overflow", "duplicate", "surrogate", "key", "depth"],
)
def test_invalid_unused_fields_are_rejected(api, binding, extra):
    raw = line(record()).rstrip(b"\n")[:-1] + b"," + extra + b"}\n"
    with pytest.raises(api.CowrieMappingError, match="^INVALID_RECORD$"):
        mapped(api, binding, raw=raw)


def test_record_exact_size_boundary(api, binding):
    raw = line(record())
    padded = raw[:-1] + b" " * (65536 - len(raw)) + b"\n"
    assert mapped(api, binding, raw=padded).model_dump(mode="json") == expected_event()


@pytest.mark.parametrize(
    "value",
    [
        record(input={"SECRET": "token"}),
        record(timestamp="SECRET"),
        record(eventid="cowrie.login.failed", username="root", password={"SECRET": "token"}),
    ],
)
def test_exception_chain_and_traceback_do_not_disclose_input(api, binding, value):
    with pytest.raises(api.CowrieMappingError) as caught:
        mapped(api, binding, value)
    error = caught.value
    assert error.__context__ is None and error.__cause__ is None
    assert "SECRET" not in "".join(traceback.format_exception(error))


def test_private_auth_payload_is_exact_and_schema_compatible(api, binding):
    result = mapped(
        api, binding, record(eventid="cowrie.login.failed", username="я" * 128, password="SECRET")
    )
    assert result.data == {"username": "я" * 128, "password": "SECRET", "success": False}
    schema = json.loads((ROOT / "contracts/agent-event-v1.schema.json").read_text("utf-8"))
    jsonschema.Draft202012Validator(schema).validate(result.model_dump(mode="json"))
