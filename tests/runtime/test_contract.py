"""Регрессии восстановления контракта агента и дрейфа относительно OpenAPI центра."""

import copy
import json
from dataclasses import fields
from pathlib import Path
from uuid import uuid4

import pytest

from agent.runtime.contract import (
    AgentConfig,
    ChannelSpec,
    ConfigParser,
    ContractError,
    DeploymentSpec,
    HighSpec,
    LoggingSpec,
    ProfileSpec,
    Secret,
    ServiceSpec,
)

ROOT = Path(__file__).resolve().parents[2]


def fixture() -> dict[str, object]:
    """Использует опубликованную центром конфигурацию из реальной схемы claim."""
    command = json.loads((ROOT / "contracts/fixtures/commands/claim-response.json").read_text())[
        "command"
    ]
    command["config"]["credentials"] = [{"username": "demo", "password": "synthetic-secret"}]
    return {
        "host_id": str(uuid4()),
        "deployments": [
            {
                "deployment_id": command["deployment_id"],
                "profile_id": command["profile_id"],
                "revision": command["revision"],
                "state": "deploying",
                "generation": command["generation"],
                "applied_revision": None,
                "instance_id": None,
                "config": command["config"],
            }
        ],
    }


def test_current_center_fixture_and_secret_redaction() -> None:
    raw = fixture()
    parsed = ConfigParser.parse(raw)
    assert not parsed.rejected
    dep = parsed.deployments[0]
    assert dep.generation == 1 and dep.state == "deploying"
    password = dep.config.credentials[0].password
    assert password.reveal()
    assert password.reveal() not in repr(parsed)
    assert str(password) == "***"
    assert repr(Secret("private-value")) == "Secret(***)"


@pytest.mark.parametrize(
    "state", ["planned", "deploying", "running", "stopping", "stopped", "failed", "unknown"]
)
def test_all_center_states_are_accepted(state: str) -> None:
    raw = fixture()
    raw["deployments"][0]["state"] = state
    assert ConfigParser.parse(raw).deployments[0].state == state


def test_bad_item_does_not_block_good_and_does_not_leak_password() -> None:
    raw = fixture()
    bad = copy.deepcopy(raw["deployments"][0])
    bad["deployment_id"] = str(uuid4())
    bad["config"]["credentials"][0]["password"] = "sensitive\nvalue"
    raw["deployments"].append(bad)
    result = ConfigParser.parse(raw)
    assert len(result.deployments) == len(result.rejected) == 1
    assert "credentials[0].password" in result.rejected[0].reason
    assert "sensitive" not in repr(result)


def test_all_duplicate_ids_are_rejected_even_if_one_is_bad() -> None:
    raw = fixture()
    bad = copy.deepcopy(raw["deployments"][0])
    bad["config"] = None
    raw["deployments"].append(bad)
    result = ConfigParser.parse(raw)
    assert not result.deployments and len(result.rejected) == 2


@pytest.mark.parametrize("field", ["generation", "instance_id", "applied_revision"])
def test_new_required_center_fields_cannot_be_silently_ignored(field: str) -> None:
    raw = fixture()
    del raw["deployments"][0][field]
    assert len(ConfigParser.parse(raw).rejected) == 1


@pytest.mark.parametrize("generation", [True, -1, 1.0, "1"])
def test_generation_is_a_strict_nonnegative_integer(generation: object) -> None:
    raw = fixture()
    raw["deployments"][0]["generation"] = generation
    assert ConfigParser.parse(raw).rejected


def test_unknown_fields_are_forward_compatible() -> None:
    raw = fixture()
    raw["deployments"][0]["future_field"] = {"anything": 42}
    assert not ConfigParser.parse(raw).rejected


def test_malformed_top_level_is_fatal_without_echoing_input() -> None:
    with pytest.raises(ContractError, match="host_id"):
        ConfigParser.parse({"host_id": "private-value", "deployments": []})


def test_dataclass_fields_and_states_match_current_openapi() -> None:
    schemas = json.loads((ROOT / "contracts/openapi.json").read_text())["components"]["schemas"]
    mappings = {
        AgentConfig: "AgentConfigRead",
        DeploymentSpec: "AgentDeployment",
        ProfileSpec: "AgentProfileConfig",
        ServiceSpec: "ServiceConfig",
        LoggingSpec: "LoggingConfig",
        ChannelSpec: "ChannelConfig",
        HighSpec: "HighConfig",
    }
    for model, name in mappings.items():
        actual = {f.name for f in fields(model)} - {"rejected"}
        assert actual == set(schemas[name]["properties"])
    assert set(schemas["AgentDeployment"]["required"]) == {f.name for f in fields(DeploymentSpec)}
    for model, name in [
        (LoggingSpec, "LoggingConfig"),
        (ChannelSpec, "ChannelConfig"),
        (HighSpec, "HighConfig"),
    ]:
        for f in fields(model):
            assert f.default == schemas[name]["properties"][f.name]["default"]
