"""OpenAPI и схемы доступны frontend и не расходятся с HTTP-приложением."""

import json
from pathlib import Path
from uuid import uuid4

import pytest
from conftest import profile_payload
from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from pydantic import ValidationError as SettingsError

from honeyforge.cli import ControlCLI
from honeyforge.config import Settings

ROOT = Path(__file__).resolve().parents[2]


def test_openapi_is_current_and_has_auth_schemes():
    assert ControlCLI.export_openapi(ROOT / "contracts/openapi.json", check=True) == 0
    schema = json.loads((ROOT / "contracts/openapi.json").read_text(encoding="utf-8"))
    assert schema["components"]["securitySchemes"]["APIKeyCookie"]["name"] == "hf_session"
    assert schema["components"]["securitySchemes"]["HTTPBearer"]["scheme"] == "bearer"


def test_event_schema_accepts_fixture_and_rejects_ambiguous_fields():
    schema = json.loads((ROOT / "contracts/event-v1.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    event = {
        "schema_version": 1,
        "event_id": str(uuid4()),
        "agent_id": str(uuid4()),
        "deployment_id": str(uuid4()),
        "instance_id": str(uuid4()),
        "session_id": None,
        "source_seq": 1,
        "occurred_at": "2026-10-09T12:00:00Z",
        "received_at": "2026-10-09T12:00:01Z",
        "type": "connection.opened",
        "source": {"ip": "192.0.2.1", "port": 40000},
        "service": {"protocol": "ssh", "port": 2222},
        "data": {},
        "artifact_ids": [],
        "profile_revision": 1,
    }
    validator.validate(event)
    with pytest.raises(ValidationError):
        validator.validate({**event, "mystery": "value"})
    with pytest.raises(ValidationError):
        validator.validate({**event, "session_id": "not-a-uuid"})


def test_replay_schema_validates_direction_and_time():
    schema = json.loads(
        (ROOT / "contracts/replay-chunk-v1.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    chunk = {
        "seq": 0,
        "offset_ms": 10,
        "direction": "out",
        "data_base64": "aGkK",
        "truncated": False,
    }
    validator.validate(chunk)
    with pytest.raises(ValidationError):
        validator.validate({**chunk, "offset_ms": -1})
    with pytest.raises(ValidationError):
        validator.validate({**chunk, "direction": "both"})


def test_production_disallows_insecure_cookie(settings):
    values = settings.model_dump()
    with pytest.raises(SettingsError):
        Settings(**{**values, "environment": "production", "cookie_secure": False})
    with pytest.raises(SettingsError):
        Settings(**{**values, "database_url": "sqlite:///bad.db"})
    with pytest.raises(SettingsError):
        Settings(**{**values, "allowed_origins": ["*"]})


def test_health_and_ready_probe(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").json() == {"status": "ready"}


def test_openapi_error_schema_covers_business_and_validation_errors(admin):
    document = admin.get("/openapi.json").json()
    ref = document["paths"]["/api/v1/profiles"]["post"]["responses"]["422"]["content"][
        "application/json"
    ]["schema"]
    validator = Draft202012Validator({**ref, "components": document["components"]})
    payload = profile_payload("medium", 2222)
    payload["config"]["credentials"] = []
    business = admin.post("/api/v1/profiles", json=payload)
    validation = admin.post("/api/v1/profiles", json={})
    assert business.status_code == validation.status_code == 422
    assert isinstance(business.json()["detail"], str)
    assert isinstance(validation.json()["detail"], list)
    validator.validate(business.json())
    validator.validate(validation.json())
