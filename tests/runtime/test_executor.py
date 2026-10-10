"""Команды и сбои доставки с настоящим LowService на подмене контейнерного backend."""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import signal
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from test_low_driver import InProcessBackend, free_port

from agent.runtime.contract import AgentConfig
from agent.runtime.drivers.containers import BackendError
from agent.runtime.drivers.low import LowDriver, LowSettings
from agent.runtime.executor import __main__ as executor_cli
from agent.runtime.executor import engine as executor_engine
from agent.runtime.executor.client import ClientError
from agent.runtime.executor.engine import CommandExecutor, LeaseGate
from agent.runtime.executor.protocol import ClaimedCommand, LeaseReply
from agent.runtime.executor.store import ExecutionStore


class Center:
    """Подмена транспорта; правила окончательного ACK проверяются отдельным backend тестом."""

    def __init__(self) -> None:
        self.deployment_id = str(uuid4())
        self.profile_id = str(uuid4())
        self.port = free_port()
        self.command: ClaimedCommand | None = None
        self.generation = 0
        self.instance: str | None = None
        self.applied: int | None = None
        self.results: dict[str, dict[str, object]] = {}
        self.acks = 0
        self.renews = 0
        self.lose_ack = False
        self.lose_lease = False
        self.finished = False

    def request(self, action: str = "start", *, reconcile: bool = False) -> ClaimedCommand:
        now = datetime.now(UTC)
        expected = self.generation
        self.generation += 1
        self.finished = False
        self.command = ClaimedCommand.parse(
            {
                "command_id": str(uuid4()),
                "deployment_id": self.deployment_id,
                "profile_id": self.profile_id,
                "revision": 1,
                "action": action,
                "expected_generation": expected,
                "generation": self.generation,
                "expected_instance_id": self.instance,
                "reconcile": reconcile,
                "config": {
                    "interaction_level": "low",
                    "hostname": "edge",
                    "services": [{"protocol": "http", "port": self.port, "banner": "demo"}],
                    "credentials": [{"username": "admin", "password": "secret-no-disk"}],
                },
                "lease_token": "hf_lease_" + "a" * 64,
                "lease_expires_at": (now + timedelta(seconds=60)).isoformat(),
                "execution_expires_at": (now + timedelta(seconds=300)).isoformat(),
                "delivery_expires_at": (now + timedelta(seconds=300)).isoformat(),
            }
        )
        return self.command

    async def claim(self, command_id: str | None = None) -> ClaimedCommand | None:
        if self.finished and command_id:
            raise ClientError("COMMAND_FINISHED", 409)
        if self.finished:
            return None
        return self.command

    async def config(self) -> AgentConfig:
        assert self.command
        deployment = dataclasses.replace(
            self.command.deployment(), instance_id=self.instance, applied_revision=self.applied
        )
        return AgentConfig(str(uuid4()), (deployment,))

    async def renew(self, command_id: str, lease_token: str) -> LeaseReply:
        self.renews += 1
        if self.lose_lease and self.renews > 1:
            raise ClientError("LEASE_EXPIRED", 409)
        assert self.command and command_id == self.command.command_id
        return LeaseReply(
            command_id, self.command.lease_expires_at, self.command.execution_expires_at
        )

    async def ack(self, command_id: str, payload: dict[str, object]) -> dict[str, object]:
        self.acks += 1
        normalized = {k: v for k, v in payload.items() if k != "lease_token"}
        previous = self.results.get(command_id)
        if previous is not None:
            assert previous == normalized
        else:
            self.results[command_id] = normalized
            self.instance = normalized["instance_id"]  # type: ignore[assignment]
            self.applied = normalized["applied_revision"]  # type: ignore[assignment]
            self.finished = True
        if self.lose_ack:
            self.lose_ack = False
            raise ClientError("NETWORK_ERROR")
        return {"job_id": command_id, "state": normalized["outcome"]}


def test_start_stop_actual_low_and_historical_revision(tmp_path: Path) -> None:
    async def scenario() -> None:
        center = Center()
        backend = InProcessBackend()
        try:
            with ExecutionStore(tmp_path / "private" / "state.sqlite3") as store:
                executor = CommandExecutor(center, store, backend)
                start = center.request()
                assert await executor.run_once()
                reader, writer = await asyncio.open_connection("127.0.0.1", center.port)
                writer.write(b"GET / HTTP/1.0\r\n\r\n")
                await writer.drain()
                assert b"200 OK" in await reader.read()
                writer.close()
                await writer.wait_closed()
                first = center.results[start.command_id]
                assert first["runtime_state"] == "running"
                stop = center.request("stop")
                assert await executor.run_once()
                last = center.results[stop.command_id]
                assert last["runtime_state"] == "stopped"
                assert last["instance_id"] == first["instance_id"]
                assert last["applied_revision"] == 1
                assert store.history(center.deployment_id).generation == 2
                assert not backend.containers
        finally:
            await backend.close()

    asyncio.run(scenario())


def test_lost_ack_restart_and_repeated_delivery_never_relaunch(tmp_path: Path) -> None:
    async def scenario() -> None:
        center = Center()
        backend = InProcessBackend()
        command = center.request()
        try:
            with ExecutionStore(tmp_path / "private" / "state.sqlite3") as store:
                center.lose_ack = True
                with pytest.raises(ClientError):
                    await CommandExecutor(center, store, backend).run_once()
                assert store.pending().result is not None
            with ExecutionStore(tmp_path / "private" / "state.sqlite3") as reopened:
                executor = CommandExecutor(center, reopened, backend)
                assert await executor.run_once()
                center.finished = False
                assert await executor.run_once()
                assert reopened.get(command.command_id).acked
                assert [c[0] for c in backend.calls].count("run") == 1
                assert center.acks == 3
        finally:
            await backend.close()

    asyncio.run(scenario())


def test_lease_loss_late_run_restart_reconciles_without_second_run(tmp_path: Path) -> None:
    async def scenario() -> None:
        center = Center()
        command = center.request()
        center.lose_lease = True
        backend = InProcessBackend(run_delay=0.08)
        try:
            with ExecutionStore(tmp_path / "private" / "state.sqlite3") as store:
                assert not await CommandExecutor(
                    center, store, backend, renew_interval=0.01
                ).run_once()
                assert center.acks == 0
                assert store.pending().phase == "executing"
                assert [c[0] for c in backend.calls].count("run") == 1
                assert not backend.mutation_targets
            center.lose_lease = False
            center.command = dataclasses.replace(command, lease_token="hf_lease_" + "b" * 64)
            with ExecutionStore(tmp_path / "private" / "state.sqlite3") as store:
                assert await CommandExecutor(center, store, backend).run_once()
                assert center.results[command.command_id]["runtime_state"] == "running"
                assert [c[0] for c in backend.calls].count("run") == 1
        finally:
            await backend.close()

    asyncio.run(scenario())


def test_executing_start_without_container_fails_without_retry(tmp_path: Path) -> None:
    async def scenario() -> None:
        center = Center()
        command = center.request()
        backend = InProcessBackend()
        with ExecutionStore(tmp_path / "private" / "state.sqlite3") as store:
            store.seed_history(center.deployment_id, 0, None, None)
            store.accept(command.identity(), command.fingerprint, command.lease_token)
            store.mark_executing(command.command_id)
        with ExecutionStore(tmp_path / "private" / "state.sqlite3") as store:
            assert await CommandExecutor(center, store, backend).run_once()
            result = center.results[command.command_id]
            assert result["error"]["code"] == "RECOVERY_UNCERTAIN"
            assert store.history(center.deployment_id).generation == 1
            assert all(call[0] != "run" for call in backend.calls)

    asyncio.run(scenario())


def test_failed_stop_reports_actual_running_and_retains_revision(tmp_path: Path) -> None:
    async def scenario() -> None:
        center = Center()
        backend = InProcessBackend()
        try:
            with ExecutionStore(tmp_path / "private" / "state.sqlite3") as store:
                executor = CommandExecutor(center, store, backend)
                center.request()
                assert await executor.run_once()
                stop = center.request("stop")
                backend.stop_error = BackendError("private-secret")
                backend.remove_error = BackendError("private-secret")
                assert await executor.run_once()
                result = center.results[stop.command_id]
                assert result["outcome"] == "failed"
                assert result["runtime_state"] == "running"
                assert result["applied_revision"] == 1
                assert "private-secret" not in str(result)
                assert store.history(center.deployment_id).generation == 2
        finally:
            backend.remove_error = None
            await backend.close()

    asyncio.run(scenario())


def test_expired_start_then_reconcile_stop_cleans_unknown(tmp_path: Path) -> None:
    async def scenario() -> None:
        center = Center()
        command = center.request()
        center.lose_lease = True
        backend = InProcessBackend(run_delay=0.08)
        try:
            with ExecutionStore(tmp_path / "private" / "state.sqlite3") as store:
                executor = CommandExecutor(center, store, backend, renew_interval=0.01)
                assert not await executor.run_once()
                center.finished = True
                assert not await executor.run_once()
                assert store.get(command.command_id).phase == "abandoned"
                center.lose_lease = False
                stop = center.request("stop", reconcile=True)
                assert await executor.run_once()
                result = center.results[stop.command_id]
                assert result["runtime_state"] == "stopped"
                assert result["instance_id"] is not None
                assert result["applied_revision"] == 1
                assert not backend.containers
        finally:
            await backend.close()

    asyncio.run(scenario())


def test_loss_during_stop_blocks_remove_until_reclaim(tmp_path: Path) -> None:
    class SlowStop(InProcessBackend):
        async def stop(self, target, timeout):
            await super().stop(target, timeout)
            await asyncio.sleep(0.08)

    async def scenario() -> None:
        center = Center()
        backend = SlowStop()
        try:
            with ExecutionStore(tmp_path / "private" / "state.sqlite3") as store:
                executor = CommandExecutor(center, store, backend, renew_interval=0.01)
                center.request()
                assert await executor.run_once()
                center.request("stop")
                center.renews = 0
                center.lose_lease = True
                assert not await executor.run_once()
                assert [c[0] for c in backend.calls].count("remove") == 0
                center.lose_lease = False
                assert await executor.run_once()
                assert not backend.containers
        finally:
            await backend.close()

    asyncio.run(scenario())


def test_backward_clock_jump_never_extends_monotonic_lease_or_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = Center().request()
    wall = datetime.now(UTC)
    monotonic = 100.0

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return wall

    monkeypatch.setattr(executor_engine, "datetime", Clock)
    monkeypatch.setattr(executor_engine.time, "monotonic", lambda: monotonic)
    gate = LeaseGate(command)
    original_execution = gate._execution_deadline
    wall -= timedelta(hours=1)
    gate.update(command.lease_expires_at, command.execution_expires_at, monotonic)
    assert gate._deadline <= 160.0
    monotonic = 161.0
    assert not gate.valid
    gate.update(command.lease_expires_at, command.execution_expires_at, monotonic)
    assert gate._execution_deadline == original_execution
    monotonic = original_execution + 1
    gate.update(command.lease_expires_at, command.execution_expires_at, monotonic)
    assert not gate.valid


def test_reconcile_stop_with_missing_container_preserves_known_history(tmp_path: Path) -> None:
    async def scenario() -> None:
        center = Center()
        center.generation = 1
        command = center.request("stop", reconcile=True)
        previous = str(uuid4())
        backend = InProcessBackend()
        with ExecutionStore(tmp_path / "private" / "state.sqlite3") as store:
            store.seed_history(center.deployment_id, 1, previous, 1)
            assert await CommandExecutor(center, store, backend).run_once()
            result = center.results[command.command_id]
            assert result["outcome"] == "succeeded"
            assert result["runtime_state"] == "stopped"
            assert result["instance_id"] == previous
            assert result["applied_revision"] == 1
            assert not backend.mutation_targets

    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["start", "stop"])
def test_failed_operation_on_unapplied_known_instance_has_valid_ack(
    tmp_path: Path, action: str
) -> None:
    async def scenario() -> None:
        center = Center()
        center.generation = 1
        center.instance = str(uuid4())
        command = center.request(action)
        backend = InProcessBackend()
        try:
            await LowDriver(
                center.deployment_id, backend, new_instance_id=lambda: center.instance
            ).start(command.deployment())
            backend.stop_error = backend.remove_error = BackendError("synthetic failure")
            with ExecutionStore(tmp_path / "private" / "state.sqlite3") as store:
                store.seed_history(center.deployment_id, 1, center.instance, None)
                assert await CommandExecutor(center, store, backend).run_once()
                result = center.results[command.command_id]
                assert result["outcome"] == "failed"
                assert result["runtime_state"] == "failed"
                assert result["instance_id"] == center.instance
                assert result["applied_revision"] is None
        finally:
            backend.remove_error = None
            await backend.close()

    asyncio.run(scenario())


def test_cli_sigterm_waits_for_started_operation_and_durable_ack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        center = Center()
        command = center.request()
        backend = InProcessBackend(run_delay=0.1)
        args = argparse.Namespace(
            center="http://127.0.0.1",
            allow_http_loopback=True,
            image="honeyforge-low:local",
            bind_address="127.0.0.1",
            network=None,
            journal_root=None,
            state=tmp_path / "private" / "state.sqlite3",
            once=False,
        )
        monkeypatch.setattr(executor_cli, "HttpCommandClient", lambda *a, **k: center)
        monkeypatch.setattr(executor_cli, "DockerCli", lambda: backend)
        original = signal.getsignal(signal.SIGTERM)
        asyncio.get_running_loop().call_later(0.02, signal.raise_signal, signal.SIGTERM)
        try:
            with pytest.raises(asyncio.CancelledError):
                await executor_cli.run(args, "test-only-token")
            assert signal.getsignal(signal.SIGTERM) == original
            assert [c[0] for c in backend.calls].count("run") == 1
            assert center.acks == 1
            with ExecutionStore(args.state) as store:
                assert store.get(command.command_id).acked
        finally:
            await backend.close()

    asyncio.run(scenario())


def test_failed_new_instance_keeps_revision_from_previous_success(tmp_path: Path) -> None:
    async def scenario() -> None:
        center = Center()
        backend = InProcessBackend()
        settings = LowSettings(start_timeout=0.05, probe_interval=0.01, probe_timeout=0.01)
        try:
            with ExecutionStore(tmp_path / "private" / "state.sqlite3") as store:
                executor = CommandExecutor(center, store, backend)
                center.request()
                assert await executor.run_once()
                assert center.applied == 1
                first_instance = center.instance
                center.request("stop")
                assert await executor.run_once()
                backend.listen = False
                backend.stop_error = backend.remove_error = BackendError(
                    "synthetic cleanup failure"
                )
                command = center.request()
                assert await CommandExecutor(center, store, backend, settings).run_once()
                result = center.results[command.command_id]
                assert result["outcome"] == "failed" and result["runtime_state"] == "failed"
                assert result["instance_id"] not in (None, first_instance)
                assert result["applied_revision"] == 1
                assert store.history(center.deployment_id).applied_revision == 1
        finally:
            backend.remove_error = None
            await backend.close()

    asyncio.run(scenario())
