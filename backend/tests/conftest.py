"""Интеграционные фикстуры используют только отдельную PostgreSQL honeyforge_test."""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet
from dotenv import dotenv_values
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from honeyforge.auth.security import Security
from honeyforge.config import Settings, get_settings
from honeyforge.main import create_app
from honeyforge.models import User

ROOT = Path(__file__).resolve().parents[2]
TEST_PASSWORD = "test-only-password-12345"


@pytest.fixture(scope="session")
def settings() -> Settings:
    local = dotenv_values(ROOT / ".env")
    url = os.environ.get("HF_TEST_DATABASE_URL") or local.get("HF_TEST_DATABASE_URL")
    if not url or make_url(url).database != "honeyforge_test":
        pytest.fail("Задайте HF_TEST_DATABASE_URL для отдельной БД с именем honeyforge_test")
    values = {
        "HF_DATABASE_URL": url,
        "HF_SESSION_SECRET": "test-only-session-secret-00000000000000000000",
        "HF_CREDENTIAL_KEY": Fernet.generate_key().decode(),
        "HF_ENVIRONMENT": "test",
        "HF_COOKIE_SECURE": "false",
        "HF_ALLOWED_ORIGINS": '["http://localhost:5173"]',
    }
    os.environ.update(values)
    get_settings.cache_clear()
    return Settings()


@pytest.fixture(scope="session")
def engine(settings: Settings) -> Iterator[Engine]:
    configuration = Config(str(ROOT / "backend/alembic.ini"))
    command.upgrade(configuration, "head")
    engine = create_engine(
        settings.database_url.get_secret_value(), connect_args={"connect_timeout": 5}
    )
    yield engine
    engine.dispose()


@pytest.fixture(autouse=True)
def clean_database(engine: Engine) -> None:
    # Имена таблиц заданы собственной метаинформацией; URL проверен до очистки.
    names = ", ".join('"' + table.name + '"' for table in User.metadata.sorted_tables)
    with engine.begin() as connection:
        connection.execute(text(f"TRUNCATE {names} CASCADE"))


@pytest.fixture(scope="session")
def password_hash(settings: Settings) -> str:
    return Security(settings).hash_password(TEST_PASSWORD)


@pytest.fixture
def actors(engine: Engine, password_hash: str) -> dict[str, User]:
    with Session(engine, expire_on_commit=False) as session:
        users = {
            role: User(username=role, role=role, password_hash=password_hash)
            for role in ["admin", "operator", "viewer"]
        }
        session.add_all(users.values())
        session.commit()
        return users


@pytest.fixture
def client(settings: Settings, actors: dict[str, User]) -> Iterator[TestClient]:
    with TestClient(create_app(settings), headers={"Origin": "http://localhost:5173"}) as client:
        yield client


def login(client: TestClient, username: str = "admin") -> dict:
    response = client.post(
        "/api/v1/auth/login", json={"username": username, "password": TEST_PASSWORD}
    )
    assert response.status_code == 200, response.text
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return response.json()


@pytest.fixture
def admin(client: TestClient) -> TestClient:
    login(client)
    return client


def profile_payload(level: str = "low", port: int = 8080, name: str = "test-profile") -> dict:
    config = {
        "interaction_level": level,
        "hostname": "node-01",
        "services": [
            {"protocol": "http" if level == "low" else "ssh", "port": port, "banner": "demo"}
        ],
    }
    if level != "low":
        config["credentials"] = [{"username": "backup", "password": "decoy-secret-value"}]
    if level == "high":
        config["high"] = {"template": "ubuntu-ssh-v1"}
    return {"name": name, "description": "Тестовый профиль", "config": config}


def register_host(
    admin: TestClient, name: str = "runtime-01", capabilities: list[str] | None = None
) -> dict:
    token = admin.post("/api/v1/enrollments").json()["enrollment_token"]
    response = admin.post(
        "/agent/v1/enroll",
        json={
            "enrollment_token": token,
            "name": name,
            "capabilities": capabilities or ["low", "medium", "high"],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()
