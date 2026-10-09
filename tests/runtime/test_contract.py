"""Разбор ответа центра и сверка границ с общим контрактом contracts/openapi.json."""

from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path
from typing import Any, get_args

import pytest

from agent.runtime import contract
from agent.runtime.contract import (
    AgentConfig,
    AgentConfigParser,
    ChannelSpec,
    ContractError,
    CredentialSpec,
    DeploymentSpec,
    HighSpec,
    Level,
    LoggingSpec,
    ProfileSpec,
    Secret,
    ServiceProtocol,
    ServiceSpec,
)

DEPLOYMENT_ID = "11111111-1111-4111-8111-111111111111"
PROFILE_ID = "22222222-2222-4222-8222-222222222222"
HOST_ID = "33333333-3333-4333-8333-333333333333"
PASSWORD = "p@ss-w0rd-NOT-FOR-LOGS"
OPENAPI = Path(__file__).resolve().parents[2] / "contracts" / "openapi.json"


def low_config() -> dict[str, Any]:
    return {
        "interaction_level": "low",
        "hostname": "edge-1",
        "services": [
            {"protocol": "tcp", "port": 2222, "banner": "SSH-2.0-OpenSSH_9.6"},
            {"protocol": "http", "port": 8080, "banner": "Welcome"},
        ],
        "logging": {"max_payload_bytes": 4096},
        "channel": {"flush_interval_ms": 500, "batch_size": 50},
        "high": None,
        "credentials": [],
    }


def medium_config() -> dict[str, Any]:
    config = low_config()
    config.update(
        interaction_level="medium",
        services=[{"protocol": "ssh", "port": 22, "banner": ""}],
        credentials=[{"username": "admin", "password": PASSWORD}],
    )
    return config


def high_config() -> dict[str, Any]:
    config = medium_config()
    config.update(
        interaction_level="high",
        high={"template": "ubuntu-ssh-v1", "ttl_seconds": 900, "cpu": 2, "memory_mb": 2048},
    )
    return config


def deployment(config: dict[str, Any] | None = None, **changes: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "deployment_id": DEPLOYMENT_ID,
        "profile_id": PROFILE_ID,
        "revision": 3,
        "state": "planned",
        "config": config if config is not None else low_config(),
    }
    item.update(changes)
    return item


def response(*deployments: dict[str, Any]) -> dict[str, Any]:
    return {"host_id": HOST_ID, "deployments": list(deployments)}


def parse(payload: object) -> AgentConfig:
    return AgentConfigParser().parse(payload)


def test_parses_complete_low_deployment() -> None:
    result = parse(response(deployment()))

    assert result.host_id == HOST_ID
    assert result.rejected == ()
    [item] = result.deployments
    assert (item.deployment_id, item.profile_id, item.revision) == (DEPLOYMENT_ID, PROFILE_ID, 3)
    profile = item.config
    assert profile.interaction_level == "low"
    assert profile.hostname == "edge-1"
    assert profile.services == (
        ServiceSpec("tcp", 2222, "SSH-2.0-OpenSSH_9.6"),
        ServiceSpec("http", 8080, "Welcome"),
    )
    assert profile.logging == LoggingSpec(4096)
    assert profile.channel == ChannelSpec(500, 50)
    assert profile.high is None
    assert profile.credentials == ()


def test_omitted_optional_fields_use_center_defaults() -> None:
    config = {
        "interaction_level": "low",
        "services": [{"protocol": "tcp", "port": 2222}],
        "credentials": [],
    }
    [item] = parse(response(deployment(config))).deployments

    assert item.config.hostname == contract.DEFAULT_HOSTNAME
    assert item.config.services[0].banner == ""
    assert item.config.logging.max_payload_bytes == contract.DEFAULT_MAX_PAYLOAD_BYTES
    assert item.config.channel == ChannelSpec(
        contract.DEFAULT_FLUSH_INTERVAL_MS, contract.DEFAULT_BATCH_SIZE
    )
    assert item.config.high is None


def test_high_profile_with_defaults_for_vm_parameters() -> None:
    config = high_config()
    config["high"] = {}
    [item] = parse(response(deployment(config))).deployments

    assert item.config.high == HighSpec(
        "ubuntu-ssh-v1",
        contract.DEFAULT_TTL_SECONDS,
        contract.DEFAULT_CPU,
        contract.DEFAULT_MEMORY_MB,
    )


def test_unknown_additional_fields_are_ignored_for_forward_compatibility() -> None:
    payload = response(deployment())
    payload["new_top_level"] = 1
    payload["deployments"][0]["new_field"] = {"x": 1}
    payload["deployments"][0]["config"]["new_option"] = True
    payload["deployments"][0]["config"]["services"][0]["tls"] = False

    result = parse(payload)

    assert len(result.deployments) == 1
    assert result.rejected == ()


def test_passwords_never_appear_in_repr_str_or_error_text() -> None:
    result = parse(response(deployment(medium_config())))
    [item] = result.deployments
    [credential] = item.config.credentials

    assert credential.password.reveal() == PASSWORD
    for rendered in (repr(result), str(result), repr(credential), f"{credential.password}"):
        assert PASSWORD not in rendered
    assert repr(credential.password) == "Secret(***)"
    assert Secret("a") == Secret("a")
    assert Secret("a") != Secret("b")

    broken = medium_config()
    broken["credentials"] = [{"username": "admin", "password": PASSWORD + "\n"}]
    rejected = parse(response(deployment(broken))).rejected
    assert len(rejected) == 1
    assert PASSWORD not in rejected[0].reason
    assert "password" in rejected[0].reason


def mutated(change: Any) -> dict[str, Any]:
    """Применяет изменение к образцу назначения и возвращает ответ центра с ним."""
    item = deployment()
    change(item)
    return response(item)


INVALID_DEPLOYMENTS = {
    "state-not-planned": lambda d: d.update(state="running"),
    "revision-zero": lambda d: d.update(revision=0),
    "revision-bool": lambda d: d.update(revision=True),
    "revision-float": lambda d: d.update(revision=1.0),
    "deployment-id-not-uuid": lambda d: d.update(deployment_id="nope"),
    "profile-id-missing": lambda d: d.pop("profile_id"),
    "config-not-object": lambda d: d.update(config=[]),
    "unknown-level": lambda d: d["config"].update(interaction_level="extreme"),
    "hostname-invalid": lambda d: d["config"].update(hostname="-bad"),
    "hostname-too-long": lambda d: d["config"].update(hostname="a" * 64),
    "no-services": lambda d: d["config"].update(services=[]),
    "too-many-services": lambda d: d["config"].update(
        services=[{"protocol": "tcp", "port": 1000 + i} for i in range(9)]
    ),
    "services-not-list": lambda d: d["config"].update(services={}),
    "port-zero": lambda d: d["config"]["services"][0].update(port=0),
    "port-too-large": lambda d: d["config"]["services"][0].update(port=65536),
    "port-bool": lambda d: d["config"]["services"][0].update(port=True),
    "unknown-protocol": lambda d: d["config"]["services"][0].update(protocol="ftp"),
    "ssh-in-low": lambda d: d["config"]["services"][0].update(protocol="ssh"),
    "duplicate-ports": lambda d: d["config"]["services"][1].update(port=2222),
    "banner-too-long": lambda d: d["config"]["services"][0].update(banner="a" * 201),
    "banner-crlf": lambda d: d["config"]["services"][0].update(banner="a\r\nb"),
    "banner-nul": lambda d: d["config"]["services"][0].update(banner="a\x00b"),
    "banner-not-str": lambda d: d["config"]["services"][0].update(banner=5),
    "banner-lone-surrogate": lambda d: d["config"]["services"][0].update(banner="\ud800"),
    "payload-too-small": lambda d: d["config"]["logging"].update(max_payload_bytes=255),
    "payload-too-large": lambda d: d["config"]["logging"].update(max_payload_bytes=8193),
    "flush-too-fast": lambda d: d["config"]["channel"].update(flush_interval_ms=99),
    "batch-too-large": lambda d: d["config"]["channel"].update(batch_size=101),
    "high-in-low": lambda d: d["config"].update(high={"template": "ubuntu-ssh-v1"}),
    "credentials-missing": lambda d: d["config"].pop("credentials"),
    "credentials-not-list": lambda d: d["config"].update(credentials={}),
    "too-many-credentials": lambda d: d["config"].update(
        credentials=[{"username": f"u{i}", "password": "x"} for i in range(9)]
    ),
    "duplicate-usernames": lambda d: d["config"].update(
        credentials=[{"username": "a", "password": "x"}, {"username": "a", "password": "y"}]
    ),
    "username-with-colon": lambda d: d["config"].update(
        credentials=[{"username": "a:b", "password": "x"}]
    ),
    "empty-password": lambda d: d["config"].update(credentials=[{"username": "a", "password": ""}]),
}


@pytest.mark.parametrize("name", sorted(INVALID_DEPLOYMENTS))
def test_invalid_deployment_is_rejected_without_hiding_valid_neighbours(name: str) -> None:
    good = deployment(deployment_id="44444444-4444-4444-8444-444444444444")
    bad = deployment()
    INVALID_DEPLOYMENTS[name](bad)

    result = parse(response(bad, good))

    assert [d.deployment_id for d in result.deployments] == [good["deployment_id"]]
    assert len(result.rejected) == 1
    assert result.rejected[0].reason.startswith("$.deployments[0]")


def test_rejected_entry_carries_deployment_id_only_when_it_is_valid() -> None:
    known = deployment(revision=0)
    unknown = deployment(deployment_id="nope", revision=0)

    result = parse(response(known, unknown, "not-an-object"))

    assert [r.deployment_id for r in result.rejected] == [DEPLOYMENT_ID, None, None]


def test_repeated_deployment_ids_reject_every_occurrence() -> None:
    """Повтор id неоднозначен: ни одна из записей с ним не принимается."""
    first = deployment(revision=1)
    second = deployment(revision=2)

    result = parse(response(first, second))

    assert result.deployments == ()
    assert [r.deployment_id for r in result.rejected] == [DEPLOYMENT_ID, DEPLOYMENT_ID]
    assert all("повторяется" in r.reason for r in result.rejected)


def test_repeated_id_is_not_both_accepted_and_rejected() -> None:
    """Регрессия: первая запись отклонена как некорректная, вторая с тем же id принималась."""
    broken = deployment(revision=0)
    valid = deployment(revision=2)

    result = parse(response(broken, valid))

    assert result.deployments == ()
    assert {r.deployment_id for r in result.rejected} == {DEPLOYMENT_ID}


def test_medium_and_high_consistency_rules() -> None:
    no_vm = high_config()
    no_vm["high"] = None
    two_services = high_config()
    two_services["services"] = [
        {"protocol": "ssh", "port": 22},
        {"protocol": "ssh", "port": 2200},
    ]
    wrong_template = high_config()
    wrong_template["high"] = {"template": "windows-1"}
    too_much_memory = high_config()
    too_much_memory["high"] = {"memory_mb": 8192}

    ok = parse(
        response(deployment(medium_config()), deployment(high_config(), deployment_id=HOST_ID))
    )
    assert len(ok.deployments) == 2

    for broken in (no_vm, two_services, wrong_template, too_much_memory):
        assert len(parse(response(deployment(broken))).rejected) == 1


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        "text",
        {},
        {"host_id": HOST_ID},
        {"deployments": []},
        {"host_id": "nope", "deployments": []},
        {"host_id": HOST_ID, "deployments": {}},
        {"host_id": HOST_ID, "deployments": "x"},
    ],
    ids=[
        "none",
        "list",
        "string",
        "empty-object",
        "no-deployments",
        "no-host",
        "bad-host-uuid",
        "deployments-object",
        "deployments-string",
    ],
)
def test_top_level_violation_raises_contract_error(payload: object) -> None:
    with pytest.raises(ContractError):
        parse(payload)


def test_empty_assignment_list_is_valid() -> None:
    result = parse(response())

    assert result.deployments == () and result.rejected == ()


# --- Сверка с общим контрактом contracts/openapi.json --------------------------------


def schemas() -> dict[str, Any]:
    return json.loads(OPENAPI.read_text(encoding="utf-8"))["components"]["schemas"]


DATACLASS_TO_SCHEMA = [
    ("AgentConfigRead", AgentConfig, {"rejected"}),
    ("AgentDeployment", DeploymentSpec, set()),
    ("AgentProfileConfig", ProfileSpec, set()),
    ("ServiceConfig", ServiceSpec, set()),
    ("AgentCredential", CredentialSpec, set()),
    ("LoggingConfig", LoggingSpec, set()),
    ("ChannelConfig", ChannelSpec, set()),
    ("HighConfig", HighSpec, set()),
]


@pytest.mark.parametrize(
    ("schema_name", "model", "local_only"),
    DATACLASS_TO_SCHEMA,
    ids=[row[0] for row in DATACLASS_TO_SCHEMA],
)
def test_fields_match_center_schema(schema_name: str, model: type, local_only: set[str]) -> None:
    """Изменение набора полей в центре без обновления агента делает тест красным."""
    expected = set(schemas()[schema_name]["properties"])
    actual = {field.name for field in fields(model)} - local_only
    assert actual == expected


LIMITS = [
    ("ServiceConfig", "port", "minimum", contract.MIN_PORT),
    ("ServiceConfig", "port", "maximum", contract.MAX_PORT),
    ("ServiceConfig", "banner", "maxLength", contract.MAX_BANNER_LENGTH),
    ("AgentProfileConfig", "services", "minItems", contract.MIN_SERVICES),
    ("AgentProfileConfig", "services", "maxItems", contract.MAX_SERVICES),
    ("LoggingConfig", "max_payload_bytes", "minimum", contract.MIN_PAYLOAD_BYTES),
    ("LoggingConfig", "max_payload_bytes", "maximum", contract.MAX_PAYLOAD_BYTES),
    ("LoggingConfig", "max_payload_bytes", "default", contract.DEFAULT_MAX_PAYLOAD_BYTES),
    ("ChannelConfig", "flush_interval_ms", "minimum", contract.MIN_FLUSH_INTERVAL_MS),
    ("ChannelConfig", "flush_interval_ms", "maximum", contract.MAX_FLUSH_INTERVAL_MS),
    ("ChannelConfig", "flush_interval_ms", "default", contract.DEFAULT_FLUSH_INTERVAL_MS),
    ("ChannelConfig", "batch_size", "minimum", contract.MIN_BATCH_SIZE),
    ("ChannelConfig", "batch_size", "maximum", contract.MAX_BATCH_SIZE),
    ("ChannelConfig", "batch_size", "default", contract.DEFAULT_BATCH_SIZE),
    ("HighConfig", "ttl_seconds", "minimum", contract.MIN_TTL_SECONDS),
    ("HighConfig", "ttl_seconds", "maximum", contract.MAX_TTL_SECONDS),
    ("HighConfig", "ttl_seconds", "default", contract.DEFAULT_TTL_SECONDS),
    ("HighConfig", "cpu", "minimum", contract.MIN_CPU),
    ("HighConfig", "cpu", "maximum", contract.MAX_CPU),
    ("HighConfig", "cpu", "default", contract.DEFAULT_CPU),
    ("HighConfig", "memory_mb", "minimum", contract.MIN_MEMORY_MB),
    ("HighConfig", "memory_mb", "maximum", contract.MAX_MEMORY_MB),
    ("HighConfig", "memory_mb", "default", contract.DEFAULT_MEMORY_MB),
]


@pytest.mark.parametrize(
    ("schema_name", "field", "keyword", "expected"),
    LIMITS,
    ids=[f"{row[0]}.{row[1]}.{row[2]}" for row in LIMITS],
)
def test_limits_match_center_schema(
    schema_name: str, field: str, keyword: str, expected: int
) -> None:
    assert int(schemas()[schema_name]["properties"][field][keyword]) == expected


def test_enumerations_and_constants_match_center_schema() -> None:
    known = schemas()

    assert tuple(known["AgentProfileConfig"]["properties"]["interaction_level"]["enum"]) == (
        get_args(Level)
    )
    assert tuple(known["ServiceConfig"]["properties"]["protocol"]["enum"]) == get_args(
        ServiceProtocol
    )
    assert known["AgentDeployment"]["properties"]["state"]["const"] == "planned"
    assert known["HighConfig"]["properties"]["template"]["const"] == contract.HIGH_TEMPLATE
    assert known["AgentProfileConfig"]["properties"]["hostname"]["default"] == (
        contract.DEFAULT_HOSTNAME
    )


REQUIRED_PATHS = [
    ("AgentConfigRead", ()),
    ("AgentDeployment", ("deployments", 0)),
    ("AgentProfileConfig", ("deployments", 0, "config")),
    ("ServiceConfig", ("deployments", 0, "config", "services", 0)),
    ("AgentCredential", ("deployments", 0, "config", "credentials", 0)),
]


@pytest.mark.parametrize(
    ("schema_name", "location"), REQUIRED_PATHS, ids=[row[0] for row in REQUIRED_PATHS]
)
def test_every_field_required_by_center_is_required_by_agent(
    schema_name: str, location: tuple[str | int, ...]
) -> None:
    """Агент не должен принимать ответ, в котором нет поля, обязательного по контракту."""
    required = schemas()[schema_name]["required"]
    assert required, "в схеме не осталось обязательных полей — проверьте контракт"
    for name in required:
        payload = response(deployment(medium_config()))
        target: Any = payload
        for step in location:
            target = target[step]
        del target[name]
        if location:
            assert len(parse(payload).rejected) == 1, f"{schema_name}.{name} не обязателен"
        else:
            with pytest.raises(ContractError):
                parse(payload)


# --- Пароли и ограничения, взятые из входной схемы центра -----------------------------------


@pytest.mark.parametrize(
    "password",
    ["with\ttab", "x" * contract.MAX_PASSWORD_LENGTH, "пароль", "a b"],
    ids=["tab", "max-length", "unicode", "space"],
)
def test_passwords_accepted_by_the_center_are_accepted(password: str) -> None:
    config = medium_config()
    config["credentials"] = [{"username": "admin", "password": password}]

    [item] = parse(response(deployment(config))).deployments

    assert item.config.credentials[0].password.reveal() == password


@pytest.mark.parametrize(
    "password",
    ["", "x" * (contract.MAX_PASSWORD_LENGTH + 1), "two\nlines", "carriage\rreturn"],
    ids=["empty", "too-long", "newline", "carriage-return"],
)
def test_empty_long_or_multiline_passwords_are_rejected(password: str) -> None:
    config = medium_config()
    config["credentials"] = [{"username": "admin", "password": password}]

    assert len(parse(response(deployment(config))).rejected) == 1


def test_username_longer_than_the_center_limit_is_rejected() -> None:
    config = medium_config()
    config["credentials"] = [{"username": "u" * 65, "password": "x"}]

    assert len(parse(response(deployment(config))).rejected) == 1


AGENT_REQUIRED = {
    "AgentConfigRead": {"host_id", "deployments"},
    "AgentDeployment": {"deployment_id", "profile_id", "revision", "state", "config"},
    "AgentProfileConfig": {"interaction_level", "services", "credentials"},
    "ServiceConfig": {"protocol", "port"},
    "AgentCredential": {"username", "password"},
    "HighConfig": set(),
    "LoggingConfig": set(),
    "ChannelConfig": set(),
}


@pytest.mark.parametrize("schema_name", sorted(AGENT_REQUIRED))
def test_required_fields_match_the_center_in_both_directions(schema_name: str) -> None:
    """Агент требует ровно то, что обязательно у центра: не больше и не меньше."""
    assert set(schemas()[schema_name].get("required", [])) == AGENT_REQUIRED[schema_name]


def test_patterns_nullability_and_input_limits_match_the_center() -> None:
    known = schemas()
    profile = known["AgentProfileConfig"]["properties"]
    credential = known["CredentialInput"]["properties"]
    credentials = known["ProfileConfigInput"]["properties"]["credentials"]["anyOf"][0]

    assert profile["hostname"]["pattern"] == f"^{contract.HOSTNAME_PATTERN}$"
    assert "$ref" in profile["logging"] and "anyOf" not in profile["logging"]
    assert "$ref" in profile["channel"] and "anyOf" not in profile["channel"]
    assert {"type": "null"} in profile["high"]["anyOf"]
    assert known["ServiceConfig"]["properties"]["banner"]["default"] == ""
    assert credential["username"]["pattern"] == f"^{contract.USERNAME_PATTERN}$"
    assert credential["username"]["maxLength"] == contract.MAX_USERNAME_LENGTH
    assert credential["password"]["maxLength"] == contract.MAX_PASSWORD_LENGTH
    assert credentials["maxItems"] == contract.MAX_CREDENTIALS
