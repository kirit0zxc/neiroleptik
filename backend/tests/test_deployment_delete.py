"""Удаление освобождает порт после ACK stop и сохраняет доказательства."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from conftest import login
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from test_commands import runtime  # noqa: F401

from honeyforge.api.command_schemas import CommandCreate
from honeyforge.common import DomainError
from honeyforge.control.commands import CommandService
from honeyforge.control.deployments import DeploymentService
from honeyforge.models import AuditEvent, Command, Deployment, PortReservation, RuntimeInstance


def stop(runtime):  # noqa: F811
    """Настоящий обмен API: start, затем stop с подтверждением исполнителя."""
    runtime.start()
    assert runtime.request("stop").status_code == 202
    command = runtime.claim()
    receipt = runtime.receipt(command)
    assert runtime.ack(command, receipt).status_code == 200
    return command, receipt


def test_delete_preserves_history_and_releases_port(runtime):  # noqa: F811
    command, receipt = stop(runtime)
    api, identity = runtime.api, runtime.deployment["id"]
    path = f"/api/v1/deployments/{identity}"
    payload = {k: runtime.deployment[k] for k in ("host_id", "profile_id", "revision")}
    assert api.post("/api/v1/deployments", json=payload).status_code == 409
    assert api.delete(path).status_code == 204
    assert api.delete(path).status_code == 404
    assert api.get(path).status_code == 404
    assert identity not in [d["id"] for d in api.get("/api/v1/deployments").json()]
    config = api.get("/agent/v1/config", headers=runtime.headers).json()
    assert identity not in [d["deployment_id"] for d in config["deployments"]]
    assert api.post("/api/v1/deployments", json=payload).status_code == 201
    # Потерянный ответ финального ACK не ломает повтор после удаления.
    assert runtime.ack(command, receipt).status_code == 200
    assert api.get("/api/v1/jobs/" + command["command_id"]).status_code == 200
    assert runtime.request("start", generation=2).status_code == 404
    with Session(runtime.engine) as db:
        item = db.get(Deployment, UUID(identity))
        assert item.deleted_at is not None
        assert db.get(RuntimeInstance, UUID(receipt["instance_id"])) is not None
        assert db.scalar(select(AuditEvent).where(AuditEvent.action == "deployment.deleted"))
    event_id = str(uuid4())
    event = dict(
        schema_version=1,
        event_id=event_id,
        agent_id=runtime.host["host_id"],
        deployment_id=identity,
        instance_id=receipt["instance_id"],
        profile_revision=1,
        session_id=None,
        source_seq=1,
        occurred_at="2026-10-15T12:00:00Z",
        type="connection.opened",
        source=None,
        service={"protocol": "http", "port": 8080},
        data={},
        artifact_ids=[],
    )
    response = api.post(
        "/agent/v1/events",
        headers=runtime.headers,
        json={"schema_version": 1, "batch_id": str(uuid4()), "events": [event]},
    )
    assert response.status_code == 200, response.text
    assert event_id in response.json()["accepted_ids"]


@pytest.mark.parametrize("state", ["planned", "running", "failed", "unknown", "stopped"])
def test_delete_without_confirmed_stop_keeps_reservation(runtime, state):  # noqa: F811
    with Session(runtime.engine) as db:
        db.execute(
            update(Deployment)
            .where(Deployment.id == UUID(runtime.deployment["id"]))
            .values(state=state)
        )
        db.commit()
    assert runtime.api.delete("/api/v1/deployments/" + runtime.deployment["id"]).status_code == 409
    with Session(runtime.engine) as db:
        assert db.scalar(
            select(PortReservation).where(
                PortReservation.deployment_id == UUID(runtime.deployment["id"])
            )
        )


def test_active_command_blocks_delete(runtime):  # noqa: F811
    stop(runtime)
    assert runtime.request("start").status_code == 202
    response = runtime.api.delete("/api/v1/deployments/" + runtime.deployment["id"])
    assert response.status_code == 409 and response.json()["code"] == "COMMAND_BUSY"


def test_delete_requires_role_csrf_and_origin(runtime):  # noqa: F811
    stop(runtime)
    api, path = runtime.api, "/api/v1/deployments/" + runtime.deployment["id"]
    assert api.delete(path, headers={"X-CSRF-Token": "bad"}).status_code == 403
    assert api.delete(path, headers={"Origin": "https://invalid.example"}).status_code == 403
    login(api, "viewer")
    assert api.delete(path).status_code == 403
    login(api, "operator")
    assert api.delete(path).status_code == 204


def test_delete_and_start_serialize_on_same_host(runtime, settings, actors):  # noqa: F811
    stop(runtime)
    identity = UUID(runtime.deployment["id"])
    gate = Barrier(2)

    def execute(action):
        with Session(runtime.engine) as db:
            gate.wait(timeout=5)
            try:
                if action == "delete":
                    DeploymentService(db, settings).delete(identity, actors["admin"])
                else:
                    CommandService(db, settings).create(
                        identity,
                        CommandCreate(
                            command="start", idempotency_key=uuid4(), expected_generation=2
                        ),
                        actors["admin"],
                    )
                return "ok"
            except DomainError as error:
                db.rollback()
                return error.status

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(execute, ["delete", "start"]))
    assert results.count("ok") == 1, results
    with Session(runtime.engine) as db:
        item = db.get(Deployment, identity)
        active = db.scalar(
            select(Command).where(Command.deployment_id == identity, Command.state == "requested")
        )
        reserved = db.scalar(
            select(PortReservation).where(PortReservation.deployment_id == identity)
        )
        assert (item.deleted_at is not None and active is None and reserved is None) or (
            item.deleted_at is None and active is not None and reserved is not None
        )
