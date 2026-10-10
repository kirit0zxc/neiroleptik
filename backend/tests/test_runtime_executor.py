"""Командный центр и настоящий LowService вместе; Docker заменён локальным backend."""

import asyncio
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from conftest import ROOT, profile_payload, register_host
from fastapi.testclient import TestClient

# Агент собирается отдельным проектом, но общий тест проверяет оба компонента в CI
# backend. Фикстуры этого каталога уже загружены; helper Low не меняет conftest.
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests/runtime"))

from agent.runtime.contract import AgentConfig, ConfigParser  # noqa: E402
from agent.runtime.drivers.containers import BackendError  # noqa: E402
from agent.runtime.executor.client import ClientError  # noqa: E402
from agent.runtime.executor.engine import CommandExecutor  # noqa: E402
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


class RuntimeIntegration:
    """Создаёт команду через операторский API и наблюдает состояние центра."""

    def __init__(self, api: TestClient) -> None:
        self.api = api
        self.port = free_port()
        self.host = register_host(api, capabilities=["low"])
        profile_response = api.post(
            "/api/v1/profiles",
            json=profile_payload("low", self.port, name=f"runtime-integration-{uuid4()}"),
        )
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
