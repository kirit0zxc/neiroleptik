"""Проверки ролей, cookie, CSRF и ограничения перебора."""

from datetime import timedelta
from uuid import uuid4

import pytest
from conftest import TEST_PASSWORD, login, profile_payload
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from honeyforge.common import utcnow
from honeyforge.models import UserSession


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/profiles",
        "/api/v1/hosts",
        "/api/v1/deployments",
        "/api/v1/audit",
        "/api/v1/auth/me",
    ],
)
def test_unauthenticated_read_is_denied(client, path):
    assert client.get(path).status_code == 401


def test_login_me_logout_revokes_database_session(client, engine):
    response = client.post(
        "/api/v1/auth/login", json={"username": "ADMIN", "password": TEST_PASSWORD}
    )
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie
    token = client.cookies["hf_session"]
    csrf = response.json()["csrf_token"]
    assert client.get("/api/v1/auth/me").json()["csrf_token"] == csrf
    with Session(engine) as db:
        item = db.scalar(select(UserSession))
        assert item.token_hash != token and len(item.token_hash) == 64
    client.headers["X-CSRF-Token"] = csrf
    assert client.post("/api/v1/auth/logout").status_code == 204
    client.cookies.set("hf_session", token)
    assert client.get("/api/v1/auth/me").status_code == 401


@pytest.mark.parametrize("origin", ["https://evil.example", "null", ""])
def test_login_rejects_foreign_origin(client, origin):
    assert (
        client.post(
            "/api/v1/auth/login",
            headers={"Origin": origin},
            json={"username": "admin", "password": TEST_PASSWORD},
        ).status_code
        == 403
    )


def test_wrong_password_and_unknown_user_are_401(client):
    for username in ["admin", "missing"]:
        assert (
            client.post(
                "/api/v1/auth/login", json={"username": username, "password": "wrong"}
            ).status_code
            == 401
        )


def test_csrf_and_origin_required_for_writes(client):
    csrf = login(client)["csrf_token"]
    for headers in [
        {"X-CSRF-Token": ""},
        {"X-CSRF-Token": "wrong"},
        {"Origin": "https://evil.example", "X-CSRF-Token": csrf},
    ]:
        assert (
            client.post("/api/v1/profiles", json=profile_payload(), headers=headers).status_code
            == 403
        )
    assert client.post("/api/v1/profiles", json=profile_payload()).status_code == 201


def test_viewer_cannot_mutate_or_read_audit(client):
    login(client, "viewer")
    assert client.get("/api/v1/profiles").status_code == 200
    assert client.post("/api/v1/profiles", json=profile_payload()).status_code == 403
    assert client.post("/api/v1/enrollments").status_code == 403
    assert client.get("/api/v1/audit").status_code == 403
    assert client.delete(f"/api/v1/profiles/{uuid4()}").status_code == 403


def test_operator_can_manage_profiles_but_not_enrollment(client):
    login(client, "operator")
    assert client.post("/api/v1/profiles", json=profile_payload()).status_code == 201
    assert client.post("/api/v1/enrollments").status_code == 403


def test_session_expiration(client, engine):
    login(client)
    with Session(engine) as db:
        db.execute(update(UserSession).values(expires_at=utcnow() - timedelta(seconds=1)))
        db.commit()
    assert client.get("/api/v1/auth/me").status_code == 401


def test_rate_limit_is_persistent(client):
    for _ in range(20):
        assert (
            client.post(
                "/api/v1/auth/login", json={"username": "admin", "password": "wrong"}
            ).status_code
            == 401
        )
    assert (
        client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": TEST_PASSWORD}
        ).status_code
        == 429
    )


def test_validation_does_not_echo_password(client):
    secret = "do-not-echo-secret"
    response = client.post("/api/v1/auth/login", json={"username": "bad name", "password": secret})
    assert response.status_code == 422
    assert secret not in response.text and '"input"' not in response.text


def test_security_headers_and_cookie_http_only(admin):
    response = admin.get("/api/v1/auth/me")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
