"""Командный центр и настоящий LowService вместе; Docker заменён локальным backend."""

import asyncio
import sys
import threading
from pathlib import Path
from uuid import uuid4

import pytest
import uvicorn
from conftest import ROOT, profile_payload, register_host
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

# Агент собирается отдельным проектом, но общий тест проверяет оба компонента в CI
# backend. Фикстуры этого каталога уже загружены; helper Low не меняет conftest.
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests/runtime"))

from agent.runtime.contract import AgentConfig, ConfigParser  # noqa: E402
from agent.runtime.drivers.containers import (  # noqa: E402
    BackendError,
    ContainerObservation,
    ContainerSpec,
)
from agent.runtime.drivers.low import (  # noqa: E402
    LABEL_INSTANCE,
    HealthCheckFailed,
    LowHealthCheck,
)
from agent.runtime.executor.client import ClientError, HostRead, HttpCommandClient  # noqa: E402
from agent.runtime.executor.engine import CommandExecutor  # noqa: E402
from agent.runtime.executor.enroll import EnrollmentClient  # noqa: E402
from agent.runtime.executor.protocol import ClaimedCommand, LeaseReply, validate_job  # noqa: E402
from agent.runtime.executor.store import ExecutionStore  # noqa: E402
from test_low_driver import InProcessBackend, free_port  # noqa: E402
from test_low_driver import settings as low_settings


class TestClientCommandClient:
    """Подмена HTTP-транспорта вызывает настоящий FastAPI и проверяет wire-ответы."""

    __test__ = False

    def __init__(self, api: TestClient, token: str) -> None:
        self.api = api
        self.headers = {"Authorization": "Bearer " + token}
        self.lose_next_ack = False
        self.ack_attempts = 0

    async def _send(self, method: str, path: str, payload: dict | None = None) -> dict:
        response = await asyncio.to_thread(
            self.api.request, method, path, headers=self.headers, json=payload
        )
        if response.status_code != 200:
            raise ClientError(response.json()["code"], response.status_code)
        return response.json()

    async def claim(self, command_id: str | None = None) -> ClaimedCommand | None:
        response = await self._send(
            "POST", "/agent/v1/commands/claim", {"command_id": command_id} if command_id else {}
        )
        assert set(response) == {"command"}
        command = response["command"]
        return None if command is None else ClaimedCommand.parse(command)

    async def renew(self, command_id: str, lease_token: str) -> LeaseReply:
        response = await self._send(
            "POST", f"/agent/v1/commands/{command_id}/renew", {"lease_token": lease_token}
        )
        return LeaseReply.parse(response)

    async def ack(self, command_id: str, payload: dict[str, object]) -> dict[str, object]:
        self.ack_attempts += 1
        response = await self._send("POST", f"/agent/v1/commands/{command_id}/ack", payload)
        if self.lose_next_ack:
            self.lose_next_ack = False
            # Commit центра уже произошёл, но исполнитель не получил его ответ.
            raise ClientError("NETWORK_ERROR")
        return validate_job(response, command_id, payload)

    async def config(self) -> AgentConfig:
        return ConfigParser.parse(await self._send("GET", "/agent/v1/config"))

    async def heartbeat(self) -> HostRead:
        return HostRead.parse(await self._send("POST", "/agent/v1/heartbeat"))


class RuntimeIntegration:
    """Создаёт команду через операторский API и наблюдает состояние центра."""

    def __init__(self, api: TestClient, host: dict | None = None, http_text: str = "demo") -> None:
        self.api = api
        self.port = free_port()
        self.host = host if host is not None else register_host(api, capabilities=["low"])
        payload = profile_payload("low", self.port, name=f"runtime-integration-{uuid4()}")
        payload["config"]["services"][0]["banner"] = http_text
        profile_response = api.post("/api/v1/profiles", json=payload)
        assert profile_response.status_code == 201, profile_response.text
        profile = profile_response.json()
        response = api.post(
            "/api/v1/deployments",
            json={"host_id": self.host["host_id"], "profile_id": profile["id"], "revision": 1},
        )
        assert response.status_code == 201, response.text
        self.deployment_id = response.json()["id"]
        self.client = TestClientCommandClient(api, self.host["agent_token"])

    def state(self) -> dict:
        response = self.api.get(f"/api/v1/deployments/{self.deployment_id}")
        assert response.status_code == 200, response.text
        return response.json()

    def request(self, action: str) -> str:
        response = self.api.post(
            f"/api/v1/deployments/{self.deployment_id}/commands",
            json={
                "command": action,
                "idempotency_key": str(uuid4()),
                "expected_generation": self.state()["generation"],
            },
        )
        assert response.status_code == 202, response.text
        return response.json()["job_id"]

    def job(self, command_id: str) -> dict:
        response = self.api.get(f"/api/v1/jobs/{command_id}")
        assert response.status_code == 200, response.text
        return response.json()

    async def probe_http(self) -> None:
        """Проверяет доступность реального сокета Low после подтверждения running."""
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        try:
            writer.write(b"GET / HTTP/1.1\r\nHost: runtime.test\r\n\r\n")
            await writer.drain()
            async with asyncio.timeout(2):
                response = await reader.read()
            assert response.startswith(b"HTTP/1.1 200")
            assert response.endswith(b"demo\n")
        finally:
            writer.close()
            await writer.wait_closed()


def test_center_start_actual_low_ack_running_stop_preserves_history(
    admin: TestClient, tmp_path: Path
) -> None:
    runtime = RuntimeIntegration(admin)

    async def scenario() -> None:
        backend = InProcessBackend()
        try:
            with ExecutionStore(tmp_path / "private" / "executor.sqlite") as store:
                executor = CommandExecutor(runtime.client, store, backend, low_settings())
                start = runtime.request("start")
                assert runtime.state()["state"] == "planned"
                assert await executor.run_once()
                running = runtime.state()
                assert runtime.job(start)["state"] == "succeeded"
                assert running["state"] == "running"
                assert running["generation"] == 1 and running["applied_revision"] == 1
                assert running["instance_id"] is not None
                await runtime.probe_http()

                stop = runtime.request("stop")
                assert await executor.run_once()

                stopped = runtime.state()
                assert runtime.job(stop)["state"] == "succeeded"
                assert stopped["state"] == "stopped" and stopped["generation"] == 2
                assert stopped["applied_revision"] == 1
                assert stopped["instance_id"] == running["instance_id"]
                assert store.history(runtime.deployment_id).applied_revision == 1
                assert not backend.containers
                with pytest.raises(OSError):
                    await asyncio.open_connection("127.0.0.1", runtime.port)
        finally:
            await backend.close()

    asyncio.run(scenario())


def test_lost_ack_restarted_agent_replays_terminal_result_without_another_run(
    admin: TestClient, tmp_path: Path
) -> None:
    runtime = RuntimeIntegration(admin)

    async def scenario() -> None:
        backend = InProcessBackend()
        path = tmp_path / "private" / "executor.sqlite"
        try:
            with ExecutionStore(path) as store:
                executor = CommandExecutor(runtime.client, store, backend, low_settings())
                command_id = runtime.request("start")
                runtime.client.lose_next_ack = True
                with pytest.raises(ClientError) as failure:
                    await executor.run_once()
                assert failure.value.code == "NETWORK_ERROR"
                assert runtime.state()["state"] == "running"
                assert runtime.job(command_id)["state"] == "succeeded"
                pending = store.pending()
                assert pending is not None and pending.result is not None and not pending.acked
                identity = runtime.state()["instance_id"]

            with ExecutionStore(path) as restored:
                restarted = CommandExecutor(runtime.client, restored, backend, low_settings())
                assert await restarted.run_once()
                assert restored.pending() is None
                assert restored.history(runtime.deployment_id).instance_id == identity
                assert runtime.state()["instance_id"] == identity
                assert runtime.client.ack_attempts == 2
                assert [action for action, _ in backend.calls].count("run") == 1
                await runtime.probe_http()

                runtime.request("stop")
                assert await restarted.run_once()
                assert runtime.state()["state"] == "stopped"
                assert not backend.containers
        finally:
            await backend.close()

    asyncio.run(scenario())


def test_failed_stop_reports_actual_running_and_keeps_applied_revision(
    admin: TestClient, tmp_path: Path
) -> None:
    runtime = RuntimeIntegration(admin)

    async def scenario() -> None:
        backend = InProcessBackend()
        try:
            with ExecutionStore(tmp_path / "private" / "executor.sqlite") as store:
                executor = CommandExecutor(runtime.client, store, backend, low_settings())
                runtime.request("start")
                assert await executor.run_once()
                identity = runtime.state()["instance_id"]
                backend.stop_error = BackendError("не удалось остановить")
                backend.remove_error = BackendError("не удалось удалить")
                stop = runtime.request("stop")

                assert await executor.run_once()

                state = runtime.state()
                assert runtime.job(stop)["state"] == "failed"
                assert runtime.job(stop)["error_code"] == "CLEANUP_FAILED"
                assert state["state"] == "running" and state["generation"] == 2
                assert state["instance_id"] == identity and state["applied_revision"] == 1
                await runtime.probe_http()
                backend.stop_error = backend.remove_error = None
                runtime.request("stop")
                assert await executor.run_once()
                assert runtime.state()["state"] == "stopped"
        finally:
            backend.stop_error = backend.remove_error = None
            await backend.close()

    asyncio.run(scenario())


class LateCreationBackend(InProcessBackend):
    """docker run вернул ошибку (таймаут CLI), а демон создал контейнер позже."""

    pending: ContainerSpec | None = None

    async def run(self, spec: ContainerSpec) -> None:
        self.calls.append(("run", spec.name))
        self.pending = spec
        raise BackendError("docker run: истёк таймаут клиента")

    async def late_create(self) -> None:
        spec, self.pending = self.pending, None
        assert spec is not None
        await InProcessBackend.run(self, spec)


def test_failed_start_reports_attempt_so_center_can_stop_late_container(
    admin: TestClient, tmp_path: Path
) -> None:
    """Регрессия R1-H1 на настоящем центре: без UUID попытки stop получал INSTANCE_MISMATCH."""
    runtime = RuntimeIntegration(admin)

    async def scenario() -> None:
        backend = LateCreationBackend()
        try:
            with ExecutionStore(tmp_path / "private" / "executor.sqlite") as store:
                executor = CommandExecutor(runtime.client, store, backend, low_settings())
                start = runtime.request("start")
                assert await executor.run_once()
                failed = runtime.state()
                attempt = store.get(start).attempt_instance_id
                assert runtime.job(start)["state"] == "failed"
                assert failed["state"] == "failed" and failed["instance_id"] == attempt
                assert failed["applied_revision"] is None

                await backend.late_create()
                await runtime.probe_http()

                stop = runtime.request("stop")
                assert await executor.run_once()
                stopped = runtime.state()
                assert runtime.job(stop)["state"] == "succeeded", runtime.job(stop)
                assert stopped["state"] == "stopped" and stopped["instance_id"] == attempt
                assert not backend.containers
        finally:
            await backend.close()

    asyncio.run(scenario())


class ExpiringAck:
    """Настоящий HTTP-клиент; перед первым ACK центр сам истекает lease команды."""

    def __init__(self, client: HttpCommandClient, engine: Engine) -> None:
        self._client = client
        self._engine = engine
        self.expired = False

    async def claim(self, command_id: str | None = None) -> ClaimedCommand | None:
        return await self._client.claim(command_id)

    async def renew(self, command_id: str, lease_token: str) -> LeaseReply:
        return await self._client.renew(command_id, lease_token)

    async def config(self) -> AgentConfig:
        return await self._client.config()

    async def ack(self, command_id: str, payload: dict[str, object]) -> dict[str, object]:
        if not self.expired:
            self.expired = True
            with self._engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE commands SET lease_expires_at = now() - interval '1 second' "
                        "WHERE id = :command_id"
                    ),
                    {"command_id": command_id},
                )
        return await self._client.ack(command_id, payload)


def test_real_http_client_reclaims_expired_lease_and_acks_once(
    admin: TestClient, engine: Engine, tmp_path: Path
) -> None:
    """HttpCommandClient (urllib) ↔ uvicorn ↔ PostgreSQL: проводной путь, а не подмена."""
    runtime = RuntimeIntegration(admin)
    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(admin.app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    async def scenario() -> None:
        async with asyncio.timeout(10):
            while not server.started:
                await asyncio.sleep(0.02)
        http = HttpCommandClient(
            f"http://127.0.0.1:{port}", runtime.host["agent_token"], allow_http=True
        )
        client = ExpiringAck(http, engine)
        backend = InProcessBackend()
        try:
            with ExecutionStore(tmp_path / "private" / "executor.sqlite") as store:
                executor = CommandExecutor(client, store, backend, low_settings())
                start = runtime.request("start")
                with pytest.raises(ClientError) as expired:
                    await executor.run_once()
                assert (expired.value.code, expired.value.status) == ("LEASE_EXPIRED", 409)
                assert runtime.job(start)["state"] != "succeeded"

                # Политика CLI: LEASE_EXPIRED повторяется; ACK тот же, без нового run.
                assert await executor.run_once()
                assert runtime.job(start)["state"] == "succeeded"
                assert runtime.state()["state"] == "running"
                assert [action for action, _ in backend.calls].count("run") == 1
                await runtime.probe_http()

                stop = runtime.request("stop")
                assert await executor.run_once()
                assert runtime.job(stop)["state"] == "succeeded"
                assert runtime.state()["state"] == "stopped"
                assert not backend.containers
        finally:
            await backend.close()

    try:
        asyncio.run(scenario())
    finally:
        server.should_exit = True
        thread.join(timeout=5)


class BlindCleanupBackend(InProcessBackend):
    """Docker продолжает исполнять Low, но inspect временно недоступен после отказа rm."""

    blind = False
    blind_on_failure = False

    async def inspect(self, name: str) -> ContainerObservation | None:
        if self.blind:
            raise BackendError("private inspect failure", unavailable=True)
        return await super().inspect(name)

    async def remove(self, target: str | ContainerObservation) -> None:
        try:
            await super().remove(target)
        except BackendError:
            if self.blind_on_failure:
                self.blind = True
            raise


@pytest.mark.parametrize("previous", ["exited", "running-applied", "running-unapplied"])
def test_center_failed_start_before_run_preserves_old_instance_and_next_stop_removes_it(
    admin: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, previous: str
) -> None:
    """Центр принимает исторический UUID, а обычный stop сохраняет управление старым слотом."""
    runtime = RuntimeIntegration(admin)

    async def scenario() -> None:
        backend = BlindCleanupBackend()
        healthy = LowHealthCheck.wait_until_healthy
        health_failures: list[bool] = []

        async def unhealthy(self, targets) -> None:
            health_failures.append(True)
            raise HealthCheckFailed("private health failure")

        try:
            with ExecutionStore(tmp_path / "private" / "executor.sqlite") as store:
                executor = CommandExecutor(runtime.client, store, backend, low_settings())
                first = runtime.request("start")
                if previous == "running-unapplied":
                    backend.stop_error = backend.remove_error = BackendError("private cleanup")
                    monkeypatch.setattr(LowHealthCheck, "wait_until_healthy", unhealthy)
                assert await executor.run_once()
                monkeypatch.setattr(LowHealthCheck, "wait_until_healthy", healthy)
                known = runtime.state()["instance_id"]
                assert known is not None
                if previous == "running-unapplied":
                    assert health_failures == [True]
                    assert runtime.job(first)["state"] == "failed"
                    assert runtime.state()["applied_revision"] is None
                else:
                    backend.remove_error = BackendError("private cleanup")
                    if previous == "running-applied":
                        # Итог stop остаётся failed, потому что агент потерял inspect.
                        # Поэтому start разрешён центром, хотя физический X ещё работает.
                        backend.stop_error = BackendError("private cleanup")
                        backend.blind_on_failure = True
                    first_stop = runtime.request("stop")
                    assert await executor.run_once()
                    assert runtime.job(first_stop)["state"] == "failed"
                    assert runtime.state()["state"] == "failed"
                    backend.blind = backend.blind_on_failure = False

                container = next(iter(backend.containers.values()))
                assert container.labels[LABEL_INSTANCE] == known
                assert container.running == (previous != "exited")
                run_count = [action for action, _ in backend.calls].count("run")
                start = runtime.request("start")
                assert await executor.run_once()
                failed = runtime.state()
                assert runtime.job(start)["state"] == "failed"
                assert runtime.job(start)["error_code"] == "CLEANUP_FAILED"
                assert failed["instance_id"] == known
                assert failed["state"] == ("running" if previous == "running-applied" else "failed")
                assert failed["applied_revision"] == (
                    None if previous == "running-unapplied" else 1
                )
                assert store.get(start).attempt_instance_id != known
                assert [action for action, _ in backend.calls].count("run") == run_count
                assert "private cleanup" not in str(runtime.job(start))
                if previous == "running-applied":
                    await runtime.probe_http()

                backend.stop_error = backend.remove_error = None
                stop = runtime.request("stop")
                assert await executor.run_once()
                stopped = runtime.state()
                assert runtime.job(stop)["state"] == "succeeded"
                assert stopped["state"] == "stopped" and stopped["instance_id"] == known
                assert not backend.containers
                with pytest.raises(ConnectionRefusedError):
                    await asyncio.open_connection("127.0.0.1", runtime.port)
        finally:
            backend.blind = backend.blind_on_failure = False
            backend.stop_error = backend.remove_error = None
            await backend.close()

    asyncio.run(scenario())


def test_center_blind_recovery_keeps_old_instance_controllable_after_restart(
    admin: TestClient, tmp_path: Path
) -> None:
    """Неизвестная новая попытка в ACK не лишает обычный stop доступа к durable прежнему X."""
    runtime = RuntimeIntegration(admin)

    async def scenario() -> None:
        backend = InProcessBackend()
        path = tmp_path / "private" / "executor.sqlite"
        try:
            with ExecutionStore(path) as store:
                executor = CommandExecutor(runtime.client, store, backend, low_settings())
                runtime.request("start")
                assert await executor.run_once()
                known = runtime.state()["instance_id"]
                backend.remove_error = BackendError("private cleanup")
                runtime.request("stop")
                assert await executor.run_once()
                start = runtime.request("start")
                command = await runtime.client.claim()
                assert command is not None and command.command_id == start
                record = store.accept(command.identity(), command.fingerprint, command.lease_token)
                store.mark_executing(start)
                attempt = record.attempt_instance_id
                assert attempt is not None and attempt != known

            backend.inspect_error = BackendError("private inspect", unavailable=True)
            with ExecutionStore(path) as store:
                executor = CommandExecutor(runtime.client, store, backend, low_settings())
                assert await executor.run_once()
                assert runtime.job(start)["state"] == "failed"
                assert runtime.state()["state"] == "failed"
                assert runtime.state()["instance_id"] == attempt
                assert store.known_instances(runtime.deployment_id) >= {known, attempt}
                assert next(iter(backend.containers.values())).labels[LABEL_INSTANCE] == known

                backend.inspect_error = backend.remove_error = None
                stop = runtime.request("stop")
                assert await executor.run_once()
                assert runtime.job(stop)["state"] == "succeeded"
                assert runtime.state()["state"] == "stopped"
                assert runtime.state()["instance_id"] == attempt
                removed = backend.mutation_targets[-1][1]
                assert isinstance(removed, ContainerObservation)
                assert removed.labels[LABEL_INSTANCE] == known
                assert not backend.containers
                assert [action for action, _ in backend.calls].count("run") == 1
        finally:
            backend.inspect_error = backend.remove_error = None
            await backend.close()

    asyncio.run(scenario())


def test_http_enroll_heartbeat_online_low_start_and_stop_releases_port(
    admin: TestClient, tmp_path: Path
) -> None:
    """Enrollment/heartbeat/commands проходят urllib, uvicorn, PostgreSQL и реальный LowService."""
    enrollment = admin.post("/api/v1/enrollments")
    assert enrollment.status_code == 201, enrollment.text
    token = enrollment.json()["enrollment_token"]
    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(admin.app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    async def scenario() -> None:
        async with asyncio.timeout(10):
            while not server.started:
                await asyncio.sleep(0.02)
        url = f"http://127.0.0.1:{port}"
        enrollment_client = EnrollmentClient(url, allow_http=True)
        enrolled = await enrollment_client.enroll(token, "runtime-wire-low")
        http = HttpCommandClient(url, enrolled.agent_token, allow_http=True)
        heartbeat = await http.heartbeat()
        assert heartbeat.id == enrolled.host_id
        assert heartbeat.status == "online" and heartbeat.capabilities == ("low",)
        assert heartbeat.last_seen is not None
        hosts = admin.get("/api/v1/hosts")
        assert hosts.status_code == 200, hosts.text
        host = next(host for host in hosts.json() if host["id"] == enrolled.host_id)
        assert host["status"] == "online" and host["capabilities"] == ["low"]
        assert host["last_seen"] is not None
        runtime = RuntimeIntegration(
            admin, {"host_id": enrolled.host_id, "agent_token": enrolled.agent_token}
        )
        backend = InProcessBackend()
        try:
            with ExecutionStore(tmp_path / "private" / "executor.sqlite") as store:
                executor = CommandExecutor(http, store, backend, low_settings())
                start = runtime.request("start")
                assert await executor.run_once()
                assert runtime.job(start)["state"] == "succeeded"
                running = runtime.state()
                assert running["state"] == "running" and running["applied_revision"] == 1
                await runtime.probe_http()
                stop = runtime.request("stop")
                assert await executor.run_once()
                stopped = runtime.state()
                assert runtime.job(stop)["state"] == "succeeded"
                assert stopped["state"] == "stopped"
                assert stopped["instance_id"] == running["instance_id"]
                assert stopped["applied_revision"] == 1
                assert not backend.containers
                async with asyncio.timeout(5):
                    with pytest.raises(ConnectionRefusedError):
                        await asyncio.open_connection("127.0.0.1", runtime.port)
        finally:
            await backend.close()

    try:
        asyncio.run(scenario())
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        assert not thread.is_alive()


def test_center_accepts_rejected_low_profile_start_and_its_stop(
    admin: TestClient, tmp_path: Path
) -> None:
    """TAB в тексте HTTP принимает центр, но не драйвер Low.

    Start отклоняется кодом UNSUPPORTED_PROFILE, а stop всё равно проходит.
    """
    runtime = RuntimeIntegration(admin, http_text="demo\tpage")

    async def scenario() -> None:
        backend = InProcessBackend()
        try:
            with ExecutionStore(tmp_path / "private" / "executor.sqlite") as store:
                executor = CommandExecutor(runtime.client, store, backend, low_settings())
                start = runtime.request("start")
                assert await executor.run_once()
                assert runtime.job(start)["state"] == "failed"
                assert runtime.job(start)["error_code"] == "UNSUPPORTED_PROFILE"
                assert runtime.state()["state"] == "failed"
                assert all(action != "run" for action, _ in backend.calls)

                stop = runtime.request("stop")
                assert await executor.run_once()
                assert runtime.job(stop)["state"] == "succeeded"
                assert runtime.state()["state"] == "stopped"
        finally:
            await backend.close()

    asyncio.run(scenario())
