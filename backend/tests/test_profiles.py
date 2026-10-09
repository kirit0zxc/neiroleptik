"""Поведение профилей, immutable revisions и сохранение секретов."""

import json
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

import pytest
from conftest import profile_payload, register_host
from sqlalchemy import select
from sqlalchemy.orm import Session

from honeyforge.api.schemas import ProfileUpdate
from honeyforge.common import DomainError
from honeyforge.control.profiles import ProfileService
from honeyforge.models import ProfileRevision


def test_create_read_update_keeps_old_revision(admin):
    initial = admin.post("/api/v1/profiles", json=profile_payload()).json()
    changed = profile_payload(port=8081)["config"]
    result = admin.patch(
        f"/api/v1/profiles/{initial['id']}", json={"expected_revision": 1, "config": changed}
    )
    assert result.status_code == 200 and result.json()["revision"] == 2
    versions = admin.get(f"/api/v1/profiles/{initial['id']}/revisions").json()
    assert [v["revision"] for v in versions] == [2, 1]
    assert versions[1]["config"]["services"][0]["port"] == 8080
    assert (
        admin.patch(
            f"/api/v1/profiles/{initial['id']}",
            json={"expected_revision": 1, "description": "late"},
        ).status_code
        == 409
    )


def test_name_casefold_conflict_and_unicode_length(admin):
    assert admin.post("/api/v1/profiles", json=profile_payload(name="Test")).status_code == 201
    assert admin.post("/api/v1/profiles", json=profile_payload(name="TEST")).status_code == 409
    assert admin.post("/api/v1/profiles", json=profile_payload(name="ß" * 80)).status_code == 201


def test_decoy_credentials_are_encrypted_and_not_in_read_api(admin, engine):
    result = admin.post("/api/v1/profiles", json=profile_payload("medium", 2222))
    assert result.status_code == 201
    assert "decoy-secret-value" not in result.text
    assert result.json()["config"]["credential_usernames"] == ["backup"]
    with Session(engine) as db:
        revision = db.scalar(select(ProfileRevision))
        assert "decoy-secret-value" not in revision.credentials_ciphertext
        assert "credentials" not in revision.config


def test_null_credentials_preserve_password_for_agent(admin):
    created = admin.post("/api/v1/profiles", json=profile_payload("medium", 2222)).json()
    config = profile_payload("medium", 2223)["config"]
    config["credentials"] = None
    assert (
        admin.patch(
            f"/api/v1/profiles/{created['id']}", json={"expected_revision": 1, "config": config}
        ).status_code
        == 200
    )
    host = register_host(admin)
    assert (
        admin.post(
            "/api/v1/deployments",
            json={"host_id": host["host_id"], "profile_id": created["id"], "revision": 2},
        ).status_code
        == 201
    )
    response = admin.get(
        "/agent/v1/config", headers={"Authorization": "Bearer " + host["agent_token"]}
    )
    assert (
        response.json()["deployments"][0]["config"]["credentials"][0]["password"]
        == "decoy-secret-value"
    )


@pytest.mark.parametrize(
    "change",
    ["duplicate-port", "invalid-protocol", "missing-high", "no-credentials", "extra-field"],
)
def test_invalid_configuration_rejected_without_secret_echo(admin, change):
    payload = profile_payload("medium", 2222)
    if change == "duplicate-port":
        payload["config"]["services"] *= 2
    elif change == "invalid-protocol":
        payload["config"]["services"][0]["protocol"] = "http"
    elif change == "missing-high":
        payload["config"]["interaction_level"] = "high"
    elif change == "no-credentials":
        payload["config"]["credentials"] = []
    else:
        payload["config"]["shell_command"] = "arbitrary"
    response = admin.post("/api/v1/profiles", json=payload)
    assert response.status_code == 422
    assert "decoy-secret-value" not in response.text


def test_archive_blocks_updates_and_new_deployments(admin):
    profile = admin.post("/api/v1/profiles", json=profile_payload()).json()
    host = register_host(admin)
    assert admin.delete(f"/api/v1/profiles/{profile['id']}").status_code == 204
    assert admin.get("/api/v1/profiles").json() == []
    assert len(admin.get("/api/v1/profiles?archived=true").json()) == 1
    assert (
        admin.patch(
            f"/api/v1/profiles/{profile['id']}", json={"expected_revision": 1, "name": "new"}
        ).status_code
        == 409
    )
    assert (
        admin.post(
            "/api/v1/deployments",
            json={"host_id": host["host_id"], "profile_id": profile["id"], "revision": 1},
        ).status_code
        == 409
    )


def test_parallel_revision_update_has_one_winner(admin, engine, settings, actors):
    profile = admin.post("/api/v1/profiles", json=profile_payload()).json()

    def update():
        with Session(engine, expire_on_commit=False) as db:
            try:
                ProfileService(db, settings).update(
                    UUID(profile["id"]),
                    ProfileUpdate(expected_revision=1, description="updated"),
                    actors["admin"],
                )
                return 200
            except DomainError as error:
                return error.status

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: update(), range(2)))
    assert sorted(outcomes) == [200, 409]


@pytest.mark.parametrize("field", ["name", "description"])
@pytest.mark.parametrize("bad_text", ["bad\x00text", "bad\ud800text"])
def test_invalid_text_is_validation_error_and_does_not_change_profile(admin, field, bad_text):
    payload = profile_payload()
    payload[field] = bad_text
    assert (
        admin.post(
            "/api/v1/profiles",
            content=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        ).status_code
        == 422
    )
    profile = admin.post("/api/v1/profiles", json=profile_payload()).json()
    response = admin.patch(
        f"/api/v1/profiles/{profile['id']}",
        content=json.dumps({"expected_revision": 1, field: bad_text}),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert admin.get(f"/api/v1/profiles/{profile['id']}").json()["revision"] == 1
