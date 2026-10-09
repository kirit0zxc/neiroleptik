"""Контракт регистрации, разделение прав агента и резервирование портов."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import UUID, uuid4

from conftest import profile_payload, register_host
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from honeyforge.api.schemas import EnrollInput
from honeyforge.common import DomainError, utcnow
from honeyforge.control.registry import RegistryService
from honeyforge.models import AuditEvent, Enrollment, Host


def test_enrollment_one_time_and_expiration(admin, engine):
    token = admin.post("/api/v1/enrollments").json()["enrollment_token"]
    payload = {"name": "host", "capabilities": ["low"], "enrollment_token": token}
    response = admin.post("/agent/v1/enroll", json=payload)
    assert response.status_code == 201
    assert admin.post("/agent/v1/enroll", json=payload).status_code == 401
    token2 = admin.post("/api/v1/enrollments").json()["enrollment_token"]
    with Session(engine) as db:
        db.execute(update(Enrollment).values(expires_at=utcnow() - timedelta(seconds=1)))
        db.commit()
    payload["enrollment_token"] = token2
    assert admin.post("/agent/v1/enroll", json=payload).status_code == 401


def test_parallel_enrollment_has_one_winner(admin, engine, settings):
    token = admin.post("/api/v1/enrollments").json()["enrollment_token"]
    payload = EnrollInput(enrollment_token=token, name="parallel", capabilities=["low"])

    def enroll():
        with Session(engine, expire_on_commit=False) as db:
            try:
                RegistryService(db, settings).enroll(payload)
                return 201
            except DomainError as error:
                return error.status

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: enroll(), range(2)))
    assert sorted(results) == [201, 401]


def test_host_liveness_and_revoke(admin, engine):
    host = register_host(admin)
    header = {"Authorization": "Bearer " + host["agent_token"]}
    assert admin.get(f"/api/v1/hosts/{host['host_id']}").json()["status"] == "offline"
    assert admin.post("/agent/v1/heartbeat", headers=header).json()["status"] == "online"
    with Session(engine) as db:
        db.execute(update(Host).values(last_seen=utcnow() - timedelta(seconds=41)))
        db.commit()
    assert admin.get(f"/api/v1/hosts/{host['host_id']}").json()["status"] == "offline"
    assert admin.post(f"/api/v1/hosts/{host['host_id']}/revoke").json()["status"] == "revoked"
    assert admin.post("/agent/v1/heartbeat", headers=header).status_code == 401
    assert admin.get("/agent/v1/config", headers=header).status_code == 401


def test_agent_only_receives_own_assigned_revision(admin, engine):
    first = register_host(admin, "first")
    second = register_host(admin, "second")
    profile = admin.post("/api/v1/profiles", json=profile_payload("medium", 2222)).json()
    payload = {"host_id": first["host_id"], "profile_id": profile["id"], "revision": 1}
    deployment = admin.post("/api/v1/deployments", json=payload)
    assert deployment.status_code == 201 and deployment.json()["state"] == "planned"
    assert admin.post("/api/v1/deployments", json=payload).status_code == 409
    assert (
        admin.get(
            "/agent/v1/config", headers={"Authorization": "Bearer " + second["agent_token"]}
        ).json()["deployments"]
        == []
    )
    config = profile_payload("medium", 2223)["config"]
    assert (
        admin.patch(
            f"/api/v1/profiles/{profile['id']}", json={"expected_revision": 1, "config": config}
        ).status_code
        == 200
    )
    response = admin.get(
        "/agent/v1/config", headers={"Authorization": "Bearer " + first["agent_token"]}
    )
    assert response.json()["deployments"][0]["revision"] == 1
    assert response.json()["deployments"][0]["config"]["services"][0]["port"] == 2222
    admin.cookies.clear()
    assert (
        admin.get(
            "/api/v1/profiles", headers={"Authorization": "Bearer " + first["agent_token"]}
        ).status_code
        == 401
    )
    with Session(engine) as db:
        stored = db.get(Host, UUID(first["host_id"]))
        assert first["agent_token"] not in stored.token_hash


def test_unknown_or_incapable_host_is_rejected(admin):
    profile = admin.post("/api/v1/profiles", json=profile_payload("high", 2224)).json()
    host = register_host(admin, capabilities=["low"])
    for host_id, expected in [(str(uuid4()), 404), (host["host_id"], 409)]:
        assert (
            admin.post(
                "/api/v1/deployments",
                json={"host_id": host_id, "profile_id": profile["id"], "revision": 1},
            ).status_code
            == expected
        )


def test_audit_is_present_and_contains_no_secrets(admin, engine):
    admin.post("/api/v1/profiles", json=profile_payload("medium", 2222))
    host = register_host(admin)
    response = admin.get("/api/v1/audit")
    assert response.status_code == 200
    assert "profile.created" in response.text and "agent.enrolled" in response.text
    assert "decoy-secret-value" not in response.text and host["agent_token"] not in response.text
    with Session(engine) as db:
        assert (
            db.scalar(select(AuditEvent).where(AuditEvent.action == "profile.created")) is not None
        )


def test_invalid_pagination_and_agent_token(client, admin):
    assert admin.get("/api/v1/profiles?limit=0").status_code == 422
    assert admin.get("/api/v1/hosts?limit=101").status_code == 422
    assert (
        admin.get("/agent/v1/config", headers={"Authorization": "Bearer invalid"}).status_code
        == 401
    )


def test_nul_host_name_does_not_consume_enrollment(admin):
    token = admin.post("/api/v1/enrollments").json()["enrollment_token"]
    payload = {"name": "bad\x00host", "capabilities": ["low"], "enrollment_token": token}
    assert admin.post("/agent/v1/enroll", json=payload).status_code == 422
    payload["name"] = "valid-host"
    assert admin.post("/agent/v1/enroll", json=payload).status_code == 201
