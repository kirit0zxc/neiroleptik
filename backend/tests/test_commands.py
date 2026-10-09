"""Контракт команд на настоящей PostgreSQL: повторы, сроки, права и гонки."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest
from alembic import command as migration
from alembic.config import Config
from conftest import ROOT, login, profile_payload, register_host
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from honeyforge.api.command_schemas import CommandAck, CommandCreate
from honeyforge.common import DomainError, utcnow
from honeyforge.control.commands import CommandService
from honeyforge.control.registry import RegistryService
from honeyforge.main import create_app
from honeyforge.models import AuditEvent, Command, Deployment, RuntimeInstance


class RuntimeHarness:
    """Имитирует только HTTP-клиента агента; БД и приложение остаются настоящими."""

    def __init__(self, admin, engine, level="low"):
        self.api = admin
        self.engine = engine
        self.host = register_host(admin)
        self.headers = {"Authorization": "Bearer " + self.host["agent_token"]}
        profile = admin.post(
            "/api/v1/profiles", json=profile_payload(level, name=f"test-{uuid4()}")
        ).json()
        response = admin.post(
            "/api/v1/deployments",
            json={
                "host_id": self.host["host_id"],
                "profile_id": profile["id"],
                "revision": 1,
            },
        )
        assert response.status_code == 201, response.text
        self.deployment = response.json()

    def state(self):
        return self.api.get(f"/api/v1/deployments/{self.deployment['id']}").json()

    def request(self, action="start", generation=None, key=None):
        return self.api.post(
            f"/api/v1/deployments/{self.deployment['id']}/commands",
            json={
                "command": action,
                "idempotency_key": key or str(uuid4()),
                "expected_generation": self.state()["generation"]
                if generation is None
                else generation,
            },
        )

    def claim(self, command_id=None):
        response = self.api.post(
            "/agent/v1/commands/claim",
            headers=self.headers,
            json={"command_id": command_id} if command_id else {},
        )
        assert response.status_code == 200, response.text
        return response.json()["command"]

    def receipt(self, command, instance=None):
        state = self.state()
        stop = command["action"] == "stop"
        return {
            "lease_token": command["lease_token"],
            "deployment_id": self.deployment["id"],
            "generation": command["generation"],
            "applied_revision": state["applied_revision"] if stop else command["revision"],
            "instance_id": state["instance_id"] if stop else (instance or str(uuid4())),
            "outcome": "succeeded",
            "runtime_state": "stopped" if stop else "running",
            "error": None,
        }

    def ack(self, command, payload=None):
        return self.api.post(
            f"/agent/v1/commands/{command['command_id']}/ack",
            headers=self.headers,
            json=payload or self.receipt(command),
        )

    def start(self):
        assert self.request().status_code == 202
        command = self.claim()
        receipt = self.receipt(command)
        assert self.ack(command, receipt).status_code == 200
        return command, receipt

    def expire(self, command_id, **values):
        with Session(self.engine) as db:
            db.execute(update(Command).where(Command.id == UUID(command_id)).values(**values))
            db.commit()


@pytest.fixture
def runtime(admin, engine):
    return RuntimeHarness(admin, engine)


def test_start_stop_start_and_job_privacy(runtime):
    initial = runtime.state()
    assert initial["state"] == "planned" and initial["applied_revision"] is None
    assert runtime.request().status_code == 202
    assert runtime.state()["state"] == "planned"
    command = runtime.claim()
    assert command["generation"] == 1 and command["expected_generation"] == 0
    assert runtime.state()["state"] == "deploying"
    response = runtime.api.post(
        f"/agent/v1/commands/{command['command_id']}/renew",
        headers=runtime.headers,
        json={"lease_token": command["lease_token"]},
    )
    assert response.status_code == 200
    job = runtime.api.get(f"/api/v1/jobs/{command['command_id']}").json()
    assert job["state"] == "running" and runtime.state()["state"] == "deploying"
    assert "lease_token" not in job and "config" not in job
    receipt = runtime.receipt(command)
    assert runtime.ack(command, receipt).json()["state"] == "succeeded"
    running = runtime.state()
    assert running["state"] == "running" and running["applied_revision"] == 1
    assert running["instance_id"] == receipt["instance_id"]
    assert runtime.request("stop").status_code == 202
    stop = runtime.claim()
    assert runtime.state()["state"] == "stopping"
    assert runtime.ack(stop).status_code == 200
    assert runtime.state()["state"] == "stopped"
    assert runtime.state()["instance_id"] == receipt["instance_id"]
    assert runtime.request().status_code == 202
    restart = runtime.claim()
    assert (
        runtime.ack(
            restart, {**runtime.receipt(restart), "instance_id": receipt["instance_id"]}
        ).json()["code"]
        == "ACK_MISMATCH"
    )
    assert runtime.ack(restart).status_code == 200
    assert runtime.state()["instance_id"] != receipt["instance_id"]
    assert runtime.state()["generation"] == 3


def test_request_retry_after_completion_and_changed_fingerprint(runtime):
    key = str(uuid4())
    first = runtime.request(key=key, generation=0)
    assert runtime.request(key=key, generation=0).json() == first.json()
    command = runtime.claim()
    assert runtime.ack(command).status_code == 200
    assert runtime.request(key=key, generation=0).json() == first.json()
    assert runtime.request(key=key, generation=1).json()["code"] == "IDEMPOTENCY_CONFLICT"
    assert runtime.request("stop", key=key, generation=0).json()["code"] == "IDEMPOTENCY_CONFLICT"
    assert runtime.request("stop", generation=0).json()["code"] == "STALE_GENERATION"


def test_busy_and_reset_restrictions(runtime):
    assert runtime.request("stop").json()["code"] == "INVALID_STATE"
    runtime.start()
    assert runtime.request("reset").json()["code"] == "UNSUPPORTED_COMMAND"
    assert runtime.request("stop").status_code == 202
    assert runtime.request("stop").json()["code"] == "COMMAND_BUSY"


def test_high_reset_replaces_instance_and_uses_separate_execution_ttl(admin, engine):
    runtime = RuntimeHarness(admin, engine, "high")
    _, receipt = runtime.start()
    assert runtime.request("reset").status_code == 202
    command = runtime.claim()
    duration = (
        datetime.fromisoformat(command["execution_expires_at"].replace("Z", "+00:00")) - utcnow()
    ).total_seconds()
    assert 890 < duration <= 900
    assert (
        runtime.ack(
            command, {**runtime.receipt(command), "instance_id": receipt["instance_id"]}
        ).json()["code"]
        == "ACK_MISMATCH"
    )
    assert runtime.ack(command).status_code == 200
    assert runtime.state()["instance_id"] != receipt["instance_id"]


def test_claim_retry_and_ack_retry_survive_new_application(runtime, settings):
    runtime.request()
    command = runtime.claim()
    assert runtime.claim() == command
    assert runtime.claim(command["command_id"]) == command
    receipt = runtime.receipt(command)
    with TestClient(create_app(settings)) as restarted:
        assert (
            restarted.post("/agent/v1/commands/claim", json={}, headers=runtime.headers).json()[
                "command"
            ]
            == command
        )
        response = restarted.post(
            f"/agent/v1/commands/{command['command_id']}/ack", json=receipt, headers=runtime.headers
        )
        assert response.status_code == 200
    first = runtime.ack(command, receipt)
    runtime.expire(command["command_id"], lease_expires_at=utcnow() - timedelta(seconds=1))
    assert runtime.ack(command, receipt).json() == first.json()
    changed = {**receipt, "instance_id": str(uuid4())}
    assert runtime.ack(command, changed).json()["code"] == "ACK_CONFLICT"
    with Session(runtime.engine) as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "command.acknowledged")
            )
            == 1
        )


def test_lease_expiry_reclaims_same_command_and_fences_old_receipt(runtime):
    runtime.request()
    old = runtime.claim()
    old_receipt = runtime.receipt(old)
    runtime.expire(old["command_id"], lease_expires_at=utcnow() - timedelta(seconds=1))
    assert runtime.ack(old, old_receipt).json()["code"] == "LEASE_EXPIRED"
    assert runtime.state()["state"] == "unknown"
    assert runtime.request().json()["code"] == "COMMAND_BUSY"
    renewed = runtime.claim()
    assert renewed["command_id"] == old["command_id"]
    assert renewed["lease_token"] != old["lease_token"]
    assert renewed["execution_expires_at"] == old["execution_expires_at"]
    assert runtime.ack(old, old_receipt).json()["code"] == "LEASE_MISMATCH"
    # Долговечный результат исполнителя повторно подтверждается под новым lease.
    assert (
        runtime.ack(renewed, {**old_receipt, "lease_token": renewed["lease_token"]}).status_code
        == 200
    )


def test_hard_deadline_unknown_requires_stop_reconciliation(runtime):
    runtime.request()
    old = runtime.claim()
    receipt = runtime.receipt(old)
    runtime.expire(old["command_id"], execution_expires_at=utcnow() - timedelta(seconds=1))
    assert runtime.api.get(f"/api/v1/jobs/{old['command_id']}").json()["state"] == "expired"
    assert runtime.state()["state"] == "unknown"
    assert runtime.request().json()["code"] == "INVALID_STATE"
    assert runtime.request("stop").status_code == 202
    stop = runtime.claim()
    assert stop["reconcile"] is True
    # Старт мог реально завершиться до потери ACK: идентичность узнаём при очистке.
    ack = {**runtime.receipt(stop), "instance_id": receipt["instance_id"], "applied_revision": 1}
    assert runtime.ack(stop, ack).status_code == 200
    assert runtime.state()["state"] == "stopped"
    assert runtime.ack(old, receipt).json()["code"] == "LEASE_EXPIRED"
    assert runtime.request().status_code == 202


def test_queue_expiry_keeps_actual_state_and_generation_is_monotonic(runtime):
    runtime.request()
    with Session(runtime.engine) as db:
        command = db.scalar(select(Command))
        command.delivery_expires_at = utcnow() - timedelta(seconds=1)
        command_id = str(command.id)
        db.commit()
    assert runtime.claim() is None
    assert runtime.api.get(f"/api/v1/jobs/{command_id}").json()["error_code"] == "DELIVERY_EXPIRED"
    assert runtime.state()["state"] == "planned"
    assert runtime.request(generation=1).status_code == 202
    assert runtime.claim()["generation"] == 2


def test_renew_cannot_extend_execution_deadline(runtime):
    runtime.request()
    command = runtime.claim()
    deadline = utcnow() + timedelta(seconds=25)
    runtime.expire(command["command_id"], execution_expires_at=deadline)
    response = runtime.api.post(
        f"/agent/v1/commands/{command['command_id']}/renew",
        headers=runtime.headers,
        json={"lease_token": command["lease_token"]},
    )
    assert response.status_code == 200
    assert response.json()["lease_expires_at"] == response.json()["execution_expires_at"]


@pytest.mark.parametrize(
    "patch,code",
    [
        ({"deployment_id": str(uuid4())}, "ACK_MISMATCH"),
        ({"generation": 999}, "STALE_GENERATION"),
        ({"applied_revision": 2}, "ACK_MISMATCH"),
        ({"runtime_state": "stopped"}, "ACK_MISMATCH"),
        ({"lease_token": "hf_lease_" + "0" * 64}, "LEASE_MISMATCH"),
    ],
)
def test_invalid_receipt_does_not_apply_revision(runtime, patch, code):
    runtime.request()
    command = runtime.claim()
    response = runtime.ack(command, {**runtime.receipt(command), **patch})
    assert response.status_code == 409 and response.json()["code"] == code
    assert runtime.state()["state"] == "deploying"
    assert runtime.state()["applied_revision"] is None


@pytest.mark.parametrize(
    "patch",
    [
        {"outcome": "failed"},
        {"instance_id": None},
        {"applied_revision": None},
        {"generation": True},
        {"error": {"code": "BAD", "message": "ошибка"}},
        {"error": {"code": "BAD", "message": "\x00"}},
    ],
)
def test_malformed_receipt_is_422(runtime, patch):
    runtime.request()
    command = runtime.claim()
    assert runtime.ack(command, {**runtime.receipt(command), **patch}).status_code == 422


def test_failed_start_and_failed_stop_report_real_state(runtime):
    runtime.request()
    command = runtime.claim()
    failed = {
        **runtime.receipt(command),
        "outcome": "failed",
        "runtime_state": "failed",
        "applied_revision": None,
        "instance_id": None,
        "error": {"code": "PORT_BUSY", "message": "Порт занят"},
    }
    assert runtime.ack(command, failed).json()["error_code"] == "PORT_BUSY"
    assert runtime.state()["state"] == "failed" and runtime.state()["applied_revision"] is None
    runtime.start()
    runtime.request("stop")
    stop = runtime.claim()
    fail_stop = {
        **runtime.receipt(stop),
        "outcome": "failed",
        "runtime_state": "running",
        "error": {"code": "STOP_FAILED", "message": "Процесс ещё работает"},
    }
    assert runtime.ack(stop, fail_stop).json()["state"] == "failed"
    assert runtime.state()["state"] == "running"


def test_stop_cannot_change_instance_or_revision(runtime):
    runtime.start()
    runtime.request("stop")
    command = runtime.claim()
    for patch in [{"instance_id": str(uuid4())}, {"applied_revision": None}]:
        assert (
            runtime.ack(command, {**runtime.receipt(command), **patch}).json()["code"]
            == "ACK_MISMATCH"
        )


def test_ownership_user_roles_and_csrf(runtime, settings):
    runtime.request()
    command = runtime.claim()
    other = register_host(runtime.api, "other")
    foreign = {"Authorization": "Bearer " + other["agent_token"]}
    for path, payload in [
        ("/agent/v1/commands/claim", {"command_id": command["command_id"]}),
        (
            f"/agent/v1/commands/{command['command_id']}/renew",
            {"lease_token": command["lease_token"]},
        ),
        (f"/agent/v1/commands/{command['command_id']}/ack", runtime.receipt(command)),
    ]:
        assert runtime.api.post(path, json=payload, headers=foreign).status_code == 404
        assert runtime.api.post(path, json=payload).status_code == 401
    with TestClient(create_app(settings), headers={"Origin": "http://localhost:5173"}) as reader:
        login(reader, "viewer")
        assert reader.get(f"/api/v1/jobs/{command['command_id']}").status_code == 200
        body = {"command": "start", "idempotency_key": str(uuid4()), "expected_generation": 1}
        assert (
            reader.post(
                f"/api/v1/deployments/{runtime.deployment['id']}/commands", json=body
            ).status_code
            == 403
        )
        login(reader, "operator")
        del reader.headers["X-CSRF-Token"]
        assert (
            reader.post(
                f"/api/v1/deployments/{runtime.deployment['id']}/commands", json=body
            ).status_code
            == 403
        )
        reader.cookies.clear()
        assert (
            reader.get(f"/api/v1/jobs/{command['command_id']}", headers=runtime.headers).status_code
            == 401
        )


def test_revocation_is_rechecked_after_cached_authentication(runtime, settings):
    runtime.request()
    with Session(runtime.engine, expire_on_commit=False) as db:
        agent = RegistryService(db, settings).authenticate(runtime.host["agent_token"])
        runtime.api.post(f"/api/v1/hosts/{runtime.host['host_id']}/revoke")
        with pytest.raises(DomainError, match="отозван"):
            CommandService(db, settings).claim(agent, None)
    assert runtime.request().status_code == 409


def test_concurrent_create_same_key_has_one_job(runtime, settings, actors):
    payload = CommandCreate(command="start", idempotency_key=uuid4(), expected_generation=0)

    def submit(_):
        with Session(runtime.engine, expire_on_commit=False) as db:
            return (
                CommandService(db, settings)
                .create(UUID(runtime.deployment["id"]), payload, actors["admin"])
                .job_id
            )

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(submit, range(3)))
    assert len(set(results)) == 1 and runtime.state()["generation"] == 1


def test_concurrent_claim_and_ack_are_idempotent(runtime, settings):
    runtime.request()

    def claim(_):
        with Session(runtime.engine, expire_on_commit=False) as db:
            agent = RegistryService(db, settings).authenticate(runtime.host["agent_token"])
            return (
                CommandService(db, settings).claim(agent, None).model_dump(mode="json")["command"]
            )

    with ThreadPoolExecutor(max_workers=3) as pool:
        commands = list(pool.map(claim, range(3)))
    assert commands[0] == commands[1] == commands[2]
    receipt = CommandAck.model_validate(runtime.receipt(commands[0]))

    def acknowledge(_):
        with Session(runtime.engine, expire_on_commit=False) as db:
            agent = RegistryService(db, settings).authenticate(runtime.host["agent_token"])
            return (
                CommandService(db, settings)
                .ack(UUID(commands[0]["command_id"]), receipt, agent)
                .model_dump(mode="json")
            )

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(acknowledge, range(3)))
    assert results[0] == results[1] == results[2]
    with Session(runtime.engine) as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "command.acknowledged")
            )
            == 1
        )


def test_ack_commit_failure_rolls_back_all_state(runtime, monkeypatch):
    runtime.request()
    command = runtime.claim()
    receipt = runtime.receipt(command)

    def fail_commit(self):
        self.flush()
        raise RuntimeError("Тестовый отказ commit")

    with monkeypatch.context() as patch:
        patch.setattr(Session, "commit", fail_commit)
        with pytest.raises(RuntimeError, match="отказ commit"):
            runtime.ack(command, receipt)
    with Session(runtime.engine) as db:
        stored = db.get(Command, UUID(command["command_id"]))
        assert stored.state == "leased" and stored.ack_digest is None
        deployment = db.get(Deployment, UUID(runtime.deployment["id"]))
        assert deployment.state == "deploying" and deployment.applied_revision_id is None
        assert (
            db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "command.acknowledged")
            )
            == 0
        )


def test_profile_revision_is_pinned_and_secrets_only_go_to_owner(admin, engine):
    runtime = RuntimeHarness(admin, engine, "medium")
    profile_id = runtime.deployment["profile_id"]
    admin.patch(
        f"/api/v1/profiles/{profile_id}",
        json={"expected_revision": 1, "config": profile_payload("medium", 2223)["config"]},
    )
    runtime.request()
    command = runtime.claim()
    assert command["revision"] == 1 and command["config"]["services"][0]["port"] == 8080
    assert command["config"]["credentials"][0]["password"] == "decoy-secret-value"
    for path in [f"/api/v1/jobs/{command['command_id']}", "/api/v1/deployments", "/api/v1/audit"]:
        text = admin.get(path).text
        assert "decoy-secret-value" not in text and command["lease_token"] not in text


def test_archived_profile_cannot_start_but_running_can_stop(runtime):
    runtime.start()
    runtime.api.delete(f"/api/v1/profiles/{runtime.deployment['profile_id']}")
    assert runtime.request("stop").status_code == 202
    command = runtime.claim()
    assert runtime.ack(command).status_code == 200
    assert runtime.request().json()["code"] == "PROFILE_ARCHIVED"


@pytest.mark.parametrize("outcome", ["succeeded", "failed"])
def test_reset_cannot_reuse_any_older_instance(admin, engine, outcome):
    runtime = RuntimeHarness(admin, engine, "high")
    _, first = runtime.start()
    runtime.request("reset")
    reset = runtime.claim()
    assert runtime.ack(reset).status_code == 200
    runtime.request("reset")
    reset = runtime.claim()
    reused = {**runtime.receipt(reset), "instance_id": first["instance_id"]}
    if outcome == "failed":
        reused.update(
            outcome="failed",
            runtime_state="failed",
            applied_revision=None,
            error={"code": "RESET_FAILED", "message": "Ошибка сброса"},
        )
    assert runtime.ack(reset, reused).json()["code"] == "INSTANCE_REUSED"
    assert runtime.ack(reset).status_code == 200


def test_failed_instance_is_not_reused_for_future_start(runtime):
    runtime.request()
    command = runtime.claim()
    identity = str(uuid4())
    failed = {
        **runtime.receipt(command, identity),
        "outcome": "failed",
        "runtime_state": "failed",
        "applied_revision": None,
        "error": {"code": "FAILED", "message": "Не запустилась"},
    }
    assert runtime.ack(command, failed).status_code == 200
    # Следующий неуспешный запуск может не создать экземпляр; история первого остаётся.
    runtime.request()
    command = runtime.claim()
    failed = {
        **failed,
        "lease_token": command["lease_token"],
        "generation": command["generation"],
        "instance_id": None,
    }
    assert runtime.ack(command, failed).status_code == 200
    runtime.request()
    command = runtime.claim()
    assert (
        runtime.ack(command, runtime.receipt(command, identity)).json()["code"] == "INSTANCE_REUSED"
    )


def test_instance_uuid_cannot_belong_to_two_deployments(admin, engine):
    first = RuntimeHarness(admin, engine)
    second = RuntimeHarness(admin, engine, "medium")
    _, receipt = first.start()
    second.request()
    command = second.claim()
    assert (
        second.ack(command, second.receipt(command, receipt["instance_id"])).json()["code"]
        == "INSTANCE_CONFLICT"
    )


def test_concurrent_hosts_cannot_register_same_instance(admin, engine, settings):
    first = RuntimeHarness(admin, engine)
    second = RuntimeHarness(admin, engine, "medium")
    identity = str(uuid4())
    prepared = []
    for runtime in [first, second]:
        runtime.request()
        command = runtime.claim()
        prepared.append(
            (runtime, command, CommandAck.model_validate(runtime.receipt(command, identity)))
        )

    def acknowledge(data):
        runtime, command, payload = data
        with Session(engine, expire_on_commit=False) as db:
            agent = RegistryService(db, settings).authenticate(runtime.host["agent_token"])
            try:
                CommandService(db, settings).ack(UUID(command["command_id"]), payload, agent)
                return 200
            except DomainError as error:
                assert error.code == "INSTANCE_CONFLICT"
                return error.status

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(acknowledge, prepared)) == [200, 409]
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(RuntimeInstance)) == 1


def test_different_concurrent_requests_have_one_winner(runtime, settings, actors):
    def submit(_):
        with Session(runtime.engine, expire_on_commit=False) as db:
            payload = CommandCreate(command="start", idempotency_key=uuid4(), expected_generation=0)
            try:
                CommandService(db, settings).create(
                    UUID(runtime.deployment["id"]), payload, actors["admin"]
                )
                return 202
            except DomainError as error:
                return error.status

    with ThreadPoolExecutor(max_workers=3) as pool:
        assert sorted(pool.map(submit, range(3))) == [202, 409, 409]
    assert runtime.state()["generation"] == 1


def test_migration_roundtrip_preserves_existing_planned(runtime):
    configuration = Config(str(ROOT / "backend/alembic.ini"))
    migration.downgrade(configuration, "f7e4884e151c")
    migration.upgrade(configuration, "head")
    migration.check(configuration)
    assert runtime.state()["generation"] == 0
    assert runtime.state()["state"] == "planned"
    assert runtime.state()["instance_id"] is None


def test_migration_refuses_downgrade_after_use(runtime):
    runtime.start()
    configuration = Config(str(ROOT / "backend/alembic.ini"))
    with pytest.raises(RuntimeError, match="Откат запрещён"):
        migration.downgrade(configuration, "f7e4884e151c")
    assert runtime.state()["state"] == "running"
    with Session(runtime.engine) as db:
        assert db.scalar(select(func.count()).select_from(RuntimeInstance)) == 1
