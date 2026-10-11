"""Входная схема общего OpenAPI берётся из экспортированного agent Event."""

import json
from importlib.resources import files
from typing import Any


def add_agent_schema(document: dict[str, Any]) -> None:
    """Локальные JSON Schema refs переводятся в единое пространство OpenAPI."""
    source = json.loads(
        files("honeyforge.telemetry")
        .joinpath("agent-event-v1.schema.json")
        .read_text(encoding="utf-8")
    )
    definitions = source.pop("$defs", {})
    encoded = json.dumps(source).replace("#/$defs/", "#/components/schemas/Agent")
    schemas = document.setdefault("components", {}).setdefault("schemas", {})
    schemas["AgentEvent"] = json.loads(encoded)
    for name, schema in definitions.items():
        schemas["Agent" + name] = json.loads(
            json.dumps(schema).replace("#/$defs/", "#/components/schemas/Agent")
        )
    schemas["AgentBatch"] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "batch_id", "events"],
        "properties": {
            "schema_version": {"const": 1},
            "batch_id": {"type": "string", "format": "uuid"},
            "events": {
                "type": "array",
                "minItems": 1,
                "maxItems": 100,
                "items": {"$ref": "#/components/schemas/AgentEvent"},
            },
        },
    }
