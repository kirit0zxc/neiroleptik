"""Команды и сбои доставки с настоящим LowService на подмене контейнерного backend."""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import errno
import os
import signal
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from test_low_driver import InProcessBackend, free_port

from agent.runtime.contract import AgentConfig, ServiceSpec
from agent.runtime.drivers.base import DriverError, DriverErrorCode
from agent.runtime.drivers.containers import BackendError, ContainerObservation, ContainerSpec
from agent.runtime.drivers.low import (
    LABEL_INSTANCE,
    HealthCheckFailed,
    LowDriver,
    LowHealthCheck,
    LowProfileTranslator,
    LowSettings,
)
from agent.runtime.executor import __main__ as executor_cli
from agent.runtime.executor import engine as executor_engine
from agent.runtime.executor.client import ClientError, HostRead
from agent.runtime.executor.engine import CommandExecutor, ExecutionError, LeasedBackend, LeaseGate
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
        # Ошибки по номеру вызова renew и очередь отказов ACK до записи результата.
        self.renew_errors: dict[int, ClientError] = {}
        self.ack_errors: list[ClientError] = []
        self.level = "low"
        self.http_text = "demo"
        # Полный список сервисов профиля вместо одного HTTP по умолчанию.
        self.services: list[dict[str, object]] | None = None

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
                    "interaction_level": self.level,
                    "hostname": "edge",
                    "services": self.services
                    if self.services is not None
                    else [
                        {
                            "protocol": "http" if self.level == "low" else "ssh",
                            "port": self.port,
                            "banner": self.http_text,
                        }
                    ],
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

    async def heartbeat(self) -> HostRead:
        """Фоновый сигнал CLI не зависит от подмены получения команд."""
        now = datetime.now(UTC)
        return HostRead(self.deployment_id, "test-runtime", ("low",), now, "online", now)

    async def renew(self, command_id: str, lease_token: str) -> LeaseReply:
        self.renews += 1
        if self.renews in self.renew_errors:
            raise self.renew_errors.pop(self.renews)
        if self.lose_lease and self.renews > 1:
            raise ClientError("LEASE_EXPIRED", 409)
        assert self.command and command_id == self.command.command_id
        return LeaseReply(
            command_id, self.command.lease_expires_at, self.command.execution_expires_at
        )

    async def ack(self, command_id: str, payload: dict[str, object]) -> dict[str, object]:
        self.acks += 1
        if self.ack_errors:
            raise self.ack_errors.pop(0)
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
            # UUID прежней попытки сообщается центру: её контейнер мог появиться позже.
            attempt = store.get(command.command_id).attempt_instance_id
            assert attempt is not None and result["instance_id"] == attempt
            assert result["runtime_state"] == "failed"
            assert store.history(center.deployment_id).instance_id == attempt
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
        known = center.instance
        command = center.request(action)
        backend = InProcessBackend()
        try:
            await LowDriver(center.deployment_id, backend, new_instance_id=lambda: known).start(
                command.deployment()
            )
            backend.stop_error = backend.remove_error = BackendError("synthetic failure")
            with ExecutionStore(tmp_path / "private" / "state.sqlite3") as store:
                store.seed_history(center.deployment_id, 1, known, None)
                assert await CommandExecutor(center, store, backend).run_once()
                result = center.results[command.command_id]
                assert result["outcome"] == "failed"
                assert result["runtime_state"] == "failed"
                assert result["instance_id"] == known
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
            medium_state_root=None,
            medium_image="honeyforge-medium:local",
            medium_user="1000:1000",
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


# --- Регрессии по итогам цикла трёх субагентов ------------------------------------------------


class LateCreation(InProcessBackend):
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


class BlindAfterRun(InProcessBackend):
    """run создал и запустил контейнер, вернул ошибку, затем inspect недоступен."""

    blind = False

    async def run(self, spec: ContainerSpec) -> None:
        try:
            await super().run(spec)
        finally:
            self.blind = True

    async def inspect(self, name: str) -> ContainerObservation | None:
        if self.blind:
            self.calls.append(("inspect", name))
            raise BackendError("docker inspect: демон не ответил", unavailable=True)
        return await super().inspect(name)


@pytest.mark.parametrize("variant", ["late-creation", "blind-inspect"])
def test_failed_start_reports_attempt_so_its_container_stays_controllable(
    tmp_path: Path, variant: str
) -> None:
    """Регрессия R1-H1: failed ACK без UUID попытки делал её контейнер неуправляемым."""

    async def scenario() -> None:
        center = Center()
        backend: InProcessBackend
        if variant == "late-creation":
            backend = LateCreation()
        else:
            backend = BlindAfterRun(run_error_after_creation=BackendError("docker run: сбой"))
        try:
            with ExecutionStore(tmp_path / "private" / "state.sqlite3") as store:
                executor = CommandExecutor(center, store, backend)
                start = center.request()
                assert await executor.run_once()
                failed = center.results[start.command_id]
                attempt = store.get(start.command_id).attempt_instance_id
                assert attempt is not None
                assert failed["outcome"] == "failed" and failed["runtime_state"] == "failed"
                assert failed["instance_id"] == attempt
                assert failed["applied_revision"] is None
                if isinstance(backend, LateCreation):
                    await backend.late_create()
                elif isinstance(backend, BlindAfterRun):
                    backend.blind = False
                assert backend.containers, "контейнер попытки существует"

                stop = center.request("stop")
                assert await executor.run_once()
                stopped = center.results[stop.command_id]
                assert stopped["outcome"] == "succeeded", stopped["error"]
                assert stopped["runtime_state"] == "stopped"
                assert stopped["instance_id"] == attempt
                assert not backend.containers
        finally:
            if isinstance(backend, BlindAfterRun):
                backend.blind = False
            await backend.close()

    asyncio.run(scenario())


def test_transient_renew_error_does_not_close_the_lease(tmp_path: Path) -> None:
    """Регрессия R1-H3: одна сетевая ошибка renew превращала start в RECOVERY_UNCERTAIN."""

    async def scenario() -> None:
        center = Center()
        center.renew_errors[2] = ClientError("NETWORK_ERROR")
        backend = InProcessBackend(run_delay=0.1)
        try:
            with ExecutionStore(tmp_path / "private" / "state.sqlite3") as store:
                executor = CommandExecutor(center, store, backend, renew_interval=0.01)
                command = center.request()
                assert await executor.run_once()
                result = center.results[command.command_id]
                assert result["outcome"] == "succeeded" and result["runtime_state"] == "running"
                assert center.renews >= 2 and not center.renew_errors
                assert [c[0] for c in backend.calls].count("run") == 1
        finally:
            await backend.close()

    asyncio.run(scenario())


def test_lease_refusal_from_center_still_closes_the_gate(tmp_path: Path) -> None:
    """Отказ центра в renew (4xx) по-прежнему запрещает завершение без новой выдачи."""

    async def scenario() -> None:
        center = Center()
        center.renew_errors[2] = ClientError("LEASE_MISMATCH", 409)
        backend = InProcessBackend(run_delay=0.1)
        try:
            with ExecutionStore(tmp_path / "private" / "state.sqlite3") as store:
                executor = CommandExecutor(center, store, backend, renew_interval=0.01)
                center.request()
                assert not await executor.run_once()
                assert center.acks == 0
                assert store.pending().phase == "executing"
        finally:
            await backend.close()

    asyncio.run(scenario())


def test_network_error_code_in_http_403_does_not_extend_renew_permission(tmp_path: Path) -> None:
    """HTTP-статус отказа первичен: тело 403 не может объявить отказ сетевой ошибкой."""

    async def scenario() -> None:
        center = Center()
        center.renew_errors[2] = ClientError("NETWORK_ERROR", 403)
        backend = InProcessBackend(run_delay=0.1)
        try:
            with ExecutionStore(tmp_path / "private" / "state.sqlite3") as store:
                center.request()
                assert not await CommandExecutor(
                    center, store, backend, renew_interval=0.01
                ).run_once()
                assert center.acks == 0
                assert store.pending().phase == "executing"
        finally:
            await backend.close()

    asyncio.run(scenario())


def test_agent_clock_ahead_of_center_fails_loudly_without_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Регрессия R1-H2: при спешащих часах агент молча не исполнял команды."""

    class Ahead(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(seconds=90)

    monkeypatch.setattr(executor_engine, "datetime", Ahead)

    async def scenario() -> None:
        center = Center()
        command = center.request()
        backend = InProcessBackend()
        with ExecutionStore(tmp_path / "private" / "state.sqlite3") as store:
            with pytest.raises(ExecutionError) as error:
                await CommandExecutor(center, store, backend).run_once()
            assert error.value.code == "CLOCK_SKEW"
            assert store.get(command.command_id).phase == "prepared"
            assert not backend.calls and center.renews == 0 and center.acks == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("code", ["LEASE_EXPIRED", "LEASE_MISMATCH"])
def test_ack_after_lost_lease_is_reclaimed_and_delivered_without_second_run(
    tmp_path: Path, code: str
) -> None:
    async def scenario() -> None:
        center = Center()
        center.ack_errors.append(ClientError(code, 409))
        backend = InProcessBackend()
        try:
            with ExecutionStore(tmp_path / "private" / "state.sqlite3") as store:
                executor = CommandExecutor(center, store, backend)
                command = center.request()
                with pytest.raises(ClientError) as error:
                    await executor.run_once()
                assert error.value.code == code
                pending = store.pending()
                assert pending is not None and pending.result is not None
                assert await executor.run_once()
                assert center.results[command.command_id]["runtime_state"] == "running"
                assert store.get(command.command_id).acked
                assert [c[0] for c in backend.calls].count("run") == 1
        finally:
            await backend.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [403, 500])
@pytest.mark.parametrize("code", ["LEASE_EXPIRED", "LEASE_MISMATCH"])
def test_ack_non_409_lease_error_preserves_result_without_reclaim(
    tmp_path: Path, status: int, code: str
) -> None:
    """Произвольный код тела не разрешает claim после HTTP-отказа другого класса."""

    class CountingCenter(Center):
        claims = 0

        async def claim(self, command_id: str | None = None) -> ClaimedCommand | None:
            self.claims += 1
            return await super().claim(command_id)

    async def scenario() -> None:
        center = CountingCenter()
        center.ack_errors = [ClientError(code, status), ClientError(code, status)]
        backend = InProcessBackend()
        try:
            with ExecutionStore(tmp_path / "private" / "state.sqlite3") as store:
                center.request()
                executor = CommandExecutor(center, store, backend)
                for _ in range(2):
                    with pytest.raises(ClientError) as error:
                        await executor.run_once()
                    assert (error.value.code, error.value.status) == (code, status)
                assert center.claims == 1 and center.acks == 2
                assert store.pending().result is not None
                assert [action for action, _ in backend.calls].count("run") == 1
        finally:
            await backend.close()

    asyncio.run(scenario())


def test_unexpected_center_identity_is_refused_without_mutation(tmp_path: Path) -> None:
    async def scenario() -> None:
        center = Center()
        backend = InProcessBackend()
        try:
            with ExecutionStore(tmp_path / "private" / "state.sqlite3") as store:
                executor = CommandExecutor(center, store, backend)
                center.request()
                assert await executor.run_once()
                known = center.instance
                center.instance = str(uuid4())  # центр ждёт другой экземпляр
                stop = center.request("stop")
                mutations = len(backend.mutation_targets)
                assert await executor.run_once()
                result = center.results[stop.command_id]
                assert result["outcome"] == "failed"
                assert result["error"]["code"] == "INSTANCE_MISMATCH"
                assert result["instance_id"] == known
                assert len(backend.mutation_targets) == mutations
                assert backend.containers, "ловушка не тронута"
        finally:
            await backend.close()

    asyncio.run(scenario())


def test_unsupported_level_gets_failed_ack_without_docker(tmp_path: Path) -> None:
    async def scenario() -> None:
        center = Center()
        center.level = "medium"
        backend = InProcessBackend()
        command = center.request()
        with ExecutionStore(tmp_path / "private" / "state.sqlite3") as store:
            assert await CommandExecutor(center, store, backend).run_once()
            result = center.results[command.command_id]
            assert result["error"]["code"] == "UNSUPPORTED_COMMAND"
            assert result["outcome"] == "failed" and result["instance_id"] is None
            assert not backend.calls

    asyncio.run(scenario())


def test_cli_stops_on_protocol_violation_instead_of_retrying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Регрессия R1-H27: неверный ответ центра повторялся бесконечно."""

    class Broken(Center):
        async def claim(self, command_id: str | None = None) -> ClaimedCommand | None:
            raise ClientError("INVALID_RESPONSE")

    args = argparse.Namespace(
        center="http://127.0.0.1",
        allow_http_loopback=True,
        image="honeyforge-low:local",
        bind_address="127.0.0.1",
        network=None,
        journal_root=None,
        medium_state_root=None,
        medium_image="honeyforge-medium:local",
        medium_user="1000:1000",
        state=tmp_path / "private" / "state.sqlite3",
        once=False,
    )
    monkeypatch.setattr(executor_cli, "HttpCommandClient", lambda *a, **k: Broken())
    monkeypatch.setattr(executor_cli, "DockerCli", lambda: InProcessBackend())

    async def scenario() -> None:
        with pytest.raises(ClientError) as error:
            await asyncio.wait_for(executor_cli._poll(args, "test-only-token"), 5)
        assert error.value.code == "INVALID_RESPONSE"

    asyncio.run(scenario())


def test_cli_removes_token_from_environment_inherited_by_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Регрессия R2-H11: docker CLI наследовал HF_AGENT_TOKEN из окружения агента."""
    seen: dict[str, object] = {}

    async def fake_run(args: argparse.Namespace, token: str) -> None:
        seen["token"] = token
        seen["inherited"] = os.environ.get("HF_AGENT_TOKEN")

    monkeypatch.setattr(executor_cli, "run", fake_run)
    monkeypatch.setenv("HF_AGENT_TOKEN", "test-only-token")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "executor",
            "--center",
            "https://center.test",
            "--state",
            str(tmp_path / "private" / "state.sqlite3"),
            "--journal-root",
            str(tmp_path / "journal"),
        ],
    )
    executor_cli.main()
    assert seen == {"token": "test-only-token", "inherited": None}


class BlindAfterCleanupFailure(InProcessBackend):
    """После отказа очистки временно не позволяет определить состояние старого слота."""

    blind = False
    blind_on_failure = False

    async def inspect(self, name: str) -> ContainerObservation | None:
        if self.blind:
            raise BackendError("synthetic inspect failure", unavailable=True)
        return await super().inspect(name)

    async def remove(self, target: str | ContainerObservation) -> None:
        try:
            await super().remove(target)
        except BackendError:
            if self.blind_on_failure:
                self.blind = True
            raise


@pytest.mark.parametrize("previous", ["exited", "running-applied", "running-unapplied"])
def test_failed_start_before_run_keeps_previous_identity_and_stop_can_remove_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, previous: str
) -> None:
    """UUID ещё не отправленной попытки не вытесняет наблюдаемый прежний контейнер."""

    async def scenario() -> None:
        center = Center()
        backend = BlindAfterCleanupFailure()
        path = tmp_path / "private" / "state.sqlite3"
        healthy = LowHealthCheck.wait_until_healthy
        health_failures: list[bool] = []

        async def unhealthy(self, targets) -> None:
            health_failures.append(True)
            raise HealthCheckFailed("synthetic private health failure")

        try:
            with ExecutionStore(path) as store:
                executor = CommandExecutor(center, store, backend)
                first = center.request()
                if previous == "running-unapplied":
                    backend.stop_error = backend.remove_error = BackendError("private cleanup")
                    monkeypatch.setattr(LowHealthCheck, "wait_until_healthy", unhealthy)
                assert await executor.run_once()
                monkeypatch.setattr(LowHealthCheck, "wait_until_healthy", healthy)
                known = center.instance
                assert known is not None
                if previous == "running-unapplied":
                    assert health_failures == [True]
                    assert center.results[first.command_id]["error"]["code"] == "CLEANUP_FAILED"
                    assert center.applied is None
                else:
                    backend.remove_error = BackendError("private cleanup")
                    if previous == "running-applied":
                        # Центр получает failed: inspect отказал после неудачного stop,
                        # хотя прежняя ловушка продолжает работать. Новый start допустим.
                        backend.stop_error = BackendError("private cleanup")
                        backend.blind_on_failure = True
                    failed_stop = center.request("stop")
                    assert await executor.run_once()
                    assert center.results[failed_stop.command_id]["runtime_state"] == "failed"
                    backend.blind = backend.blind_on_failure = False
                old_container = next(iter(backend.containers.values()))
                assert old_container.labels[LABEL_INSTANCE] == known
                assert old_container.running == (previous != "exited")
                run_count = [action for action, _ in backend.calls].count("run")
                start = center.request()
                assert await executor.run_once()
                failed = center.results[start.command_id]
                assert failed["error"]["code"] == "CLEANUP_FAILED"
                assert failed["outcome"] == "failed" and failed["instance_id"] == known
                assert failed["runtime_state"] == (
                    "running" if previous == "running-applied" else "failed"
                )
                assert failed["applied_revision"] == (
                    None if previous == "running-unapplied" else 1
                )
                assert store.get(start.command_id).attempt_instance_id != known
                assert [action for action, _ in backend.calls].count("run") == run_count
                assert "private cleanup" not in str(failed)

                backend.stop_error = backend.remove_error = None
                stop = center.request("stop")
                assert await executor.run_once()
                assert center.results[stop.command_id]["outcome"] == "succeeded"
                assert center.results[stop.command_id]["instance_id"] == known
                assert not backend.containers
        finally:
            backend.blind = backend.blind_on_failure = False
            backend.stop_error = backend.remove_error = None
            await backend.close()

    asyncio.run(scenario())


def test_blind_executing_recovery_retains_old_instance_for_ordinary_stop(tmp_path: Path) -> None:
    """Даже после ACK неизвестной попытки durable история позволяет остановить старый X."""

    async def scenario() -> None:
        center = Center()
        backend = InProcessBackend()
        path = tmp_path / "private" / "state.sqlite3"
        try:
            with ExecutionStore(path) as store:
                executor = CommandExecutor(center, store, backend)
                center.request()
                assert await executor.run_once()
                known = center.instance
                backend.remove_error = BackendError("private cleanup")
                center.request("stop")
                assert await executor.run_once()
                command = center.request()
                prepared = store.accept(
                    command.identity(), command.fingerprint, command.lease_token
                )
                store.mark_executing(command.command_id)
                attempt = prepared.attempt_instance_id
                assert attempt is not None and attempt != known

            backend.inspect_error = BackendError("private inspect", unavailable=True)
            with ExecutionStore(path) as store:
                executor = CommandExecutor(center, store, backend)
                assert await executor.run_once()
                failed = center.results[command.command_id]
                assert failed["error"]["code"] == "BACKEND_UNAVAILABLE"
                assert failed["instance_id"] == attempt and failed["runtime_state"] == "failed"
                assert store.known_instances(center.deployment_id) >= {known, attempt}
                assert next(iter(backend.containers.values())).labels[LABEL_INSTANCE] == known

                backend.inspect_error = backend.remove_error = None
                stop = center.request("stop")
                assert await executor.run_once()
                assert center.results[stop.command_id]["instance_id"] == attempt
                assert center.results[stop.command_id]["outcome"] == "succeeded"
                removed = backend.mutation_targets[-1][1]
                assert isinstance(removed, ContainerObservation)
                assert removed.labels[LABEL_INSTANCE] == known
                assert not backend.containers
                assert [action for action, _ in backend.calls].count("run") == 1
        finally:
            backend.inspect_error = backend.remove_error = None
            await backend.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["prepared", "executing"])
def test_slightly_expired_claim_keeps_phase_without_clock_skew(tmp_path: Path, phase: str) -> None:
    """Повторный claim не продлевает lease; небольшое истечение требует новой доставки."""

    async def scenario() -> None:
        center = Center()
        original = center.request()
        center.command = dataclasses.replace(
            original, lease_expires_at=datetime.now(UTC) - timedelta(seconds=1)
        )
        command = center.command
        backend = InProcessBackend()
        with ExecutionStore(tmp_path / "private" / "state.sqlite3") as store:
            store.seed_history(center.deployment_id, 0, None, None)
            store.accept(command.identity(), command.fingerprint, command.lease_token)
            if phase == "executing":
                store.mark_executing(command.command_id)
            assert not await CommandExecutor(center, store, backend).run_once()
            pending = store.pending()
            assert pending is not None and pending.phase == phase and pending.result is None
            assert not backend.calls and center.renews == 0 and center.acks == 0

    asyncio.run(scenario())


def test_preparation_failure_without_run_does_not_report_phantom_attempt(tmp_path: Path) -> None:
    async def scenario() -> None:
        center = Center()
        original = center.request()
        center.command = dataclasses.replace(
            original,
            config=dataclasses.replace(
                original.config,
                services=(ServiceSpec("tcp", center.port, "не ASCII"),),
            ),
        )
        backend = InProcessBackend()
        with ExecutionStore(tmp_path / "private" / "state.sqlite3") as store:
            assert await CommandExecutor(center, store, backend).run_once()
            failed = center.results[original.command_id]
            assert failed["error"]["code"] == "UNSUPPORTED_PROFILE"
            assert store.get(original.command_id).attempt_instance_id is not None
            assert failed["instance_id"] is None and failed["applied_revision"] is None
            assert all(action != "run" for action, _ in backend.calls)

    asyncio.run(scenario())


def test_run_requested_marks_only_authorized_backend_invocation() -> None:
    """Закрытый lease не создаёт попытку; ошибка самого run уже делает её возможной."""

    async def scenario() -> None:
        command = Center().request()
        attempt = str(uuid4())
        gate = LeaseGate(command)
        backend = InProcessBackend(run_error=BackendError("synthetic run failure"))
        translator = LowProfileTranslator(LowSettings())
        spec = translator.container_spec(translator.blueprint(command.deployment()), attempt)
        leased = LeasedBackend(backend, gate, command.deployment_id, {attempt}, command.revision)
        gate.lost = True
        with pytest.raises(BackendError) as error:
            await leased.run(spec)
        assert str(error.value) == "LEASE_LOST"
        assert not leased.run_requested and not backend.calls
        gate.lost = False
        with pytest.raises(BackendError):
            await leased.run(spec)
        assert leased.run_requested
        assert [action for action, _ in backend.calls] == ["run"]

    asyncio.run(scenario())


def test_divergent_stop_preserves_history_even_if_center_instance_is_observed(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        center = Center()
        backend = InProcessBackend()
        try:
            with ExecutionStore(tmp_path / "private" / "state.sqlite3") as store:
                executor = CommandExecutor(center, store, backend)
                center.request()
                assert await executor.run_once()
                historical = center.instance
                center.instance = str(uuid4())
                container = next(iter(backend.containers.values()))
                container.spec = dataclasses.replace(
                    container.spec,
                    labels=tuple(
                        (key, center.instance if key == LABEL_INSTANCE else value)
                        for key, value in container.spec.labels
                    ),
                )
                stop = center.request("stop")
                count = len(backend.mutation_targets)
                assert await executor.run_once()
                failed = center.results[stop.command_id]
                assert failed["error"]["code"] == "INSTANCE_MISMATCH"
                assert failed["instance_id"] == historical and failed["runtime_state"] == "failed"
                assert store.history(center.deployment_id).instance_id == historical
                assert len(backend.mutation_targets) == count
        finally:
            await backend.close()

    asyncio.run(scenario())


def test_stop_with_absent_container_but_listening_port_is_failed(tmp_path: Path) -> None:
    """Чужой слушатель не получает данных и не превращает отсутствующий Docker в stopped."""

    async def scenario() -> None:
        center = Center()
        center.generation = 1
        center.instance = str(uuid4())
        center.applied = 1
        received: list[bytes] = []

        async def connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            received.append(await reader.read())
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(connection, "127.0.0.1", center.port)
        backend = InProcessBackend()
        try:
            command = center.request("stop")
            settings = LowSettings(start_timeout=0.1, probe_interval=0.01, probe_timeout=0.01)
            with ExecutionStore(tmp_path / "private" / "state.sqlite3") as store:
                assert await CommandExecutor(center, store, backend, settings).run_once()
                failed = center.results[command.command_id]
                assert failed["outcome"] == "failed" and failed["runtime_state"] == "failed"
                assert failed["error"]["code"] == "PORT_STILL_OPEN"
                assert "порт" in failed["error"]["message"]
                assert not backend.mutation_targets
                await asyncio.sleep(0)
                assert received and all(data == b"" for data in received)
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("code", "description"),
    [
        (DriverErrorCode.START_FAILED, "запустить"),
        (DriverErrorCode.HEALTHCHECK_FAILED, "проверку ответа"),
        (DriverErrorCode.CLEANUP_FAILED, "удалить"),
        (DriverErrorCode.BACKEND_UNAVAILABLE, "Docker недоступен"),
        (DriverErrorCode.UNSUPPORTED_PROFILE, "Профиль"),
        (DriverErrorCode.INVALID_STATE, "метки"),
    ],
)
def test_driver_error_ack_has_specific_safe_russian_description(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: DriverErrorCode, description: str
) -> None:
    async def fail(self, deployment):
        raise DriverError(code, "private-docker-output and private-profile-password")

    monkeypatch.setattr(LowDriver, "start", fail)

    async def scenario() -> None:
        center = Center()
        command = center.request()
        with ExecutionStore(tmp_path / "private" / "state.sqlite3") as store:
            assert await CommandExecutor(center, store, InProcessBackend()).run_once()
            failed = center.results[command.command_id]
            assert failed["error"]["code"] == code.value
            assert description in failed["error"]["message"]
            assert "private-" not in str(failed)
            assert failed["instance_id"] is None

    asyncio.run(scenario())


@pytest.mark.parametrize("network_failure", [TimeoutError(), OSError(errno.ENETUNREACH, "route")])
def test_stop_uncertain_port_check_does_not_claim_listener_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, network_failure: OSError
) -> None:
    """Сбой пробы не подтверждает stopped и не доказывает существование слушателя."""

    async def failed_probe(*args, **kwargs):
        raise network_failure

    monkeypatch.setattr(asyncio, "open_connection", failed_probe)

    async def scenario() -> None:
        center = Center()
        center.generation = 1
        center.instance = str(uuid4())
        center.applied = 1
        command = center.request("stop")
        backend = InProcessBackend()
        settings = LowSettings(start_timeout=0.03, probe_interval=0.005, probe_timeout=0.01)
        with ExecutionStore(tmp_path / "private" / "state.sqlite3") as store:
            assert await CommandExecutor(center, store, backend, settings).run_once()
            failed = center.results[command.command_id]
            assert failed["outcome"] == "failed" and failed["runtime_state"] == "failed"
            assert failed["error"]["code"] == "PORT_STILL_OPEN"
            assert "подтвердить освобождение" in failed["error"]["message"]
            assert "принимать соединения" not in failed["error"]["message"]
            assert not backend.mutation_targets

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [403, 500, 409])
def test_command_finished_abandons_pending_only_with_contract_status(
    tmp_path: Path, status: int
) -> None:
    """Текст тела HTTP не может заменить подтверждение завершения команды центра."""

    class FailedClaim(Center):
        async def claim(self, command_id: str | None = None) -> ClaimedCommand | None:
            assert self.command is not None and command_id == self.command.command_id
            raise ClientError("COMMAND_FINISHED", status)

    async def scenario() -> None:
        center = FailedClaim()
        command = center.request()
        backend = InProcessBackend()
        with ExecutionStore(tmp_path / "private" / "state.sqlite3") as store:
            store.seed_history(center.deployment_id, 0, None, None)
            store.accept(command.identity(), command.fingerprint, command.lease_token)
            executor = CommandExecutor(center, store, backend)
            if status == 409:
                assert not await executor.run_once()
                assert store.get(command.command_id).phase == "abandoned"
                assert store.pending() is None
            else:
                with pytest.raises(ClientError) as error:
                    await executor.run_once()
                assert error.value.status == status
                assert store.pending().phase == "prepared"
            assert not backend.calls and center.acks == 0

    asyncio.run(scenario())


def test_profile_rejected_at_start_can_still_be_stopped(tmp_path: Path) -> None:
    """Регрессия R3-M2: TAB в тексте HTTP допускают UI и центр, но не драйвер Low.

    Start честно отвечает UNSUPPORTED_PROFILE, а stop не должен зависеть от перевода
    профиля: иначе ловушку нельзя было бы перевести в stopped ни одной командой.
    """

    async def scenario() -> None:
        center = Center()
        center.http_text = "demo\tpage"
        backend = InProcessBackend()
        try:
            with ExecutionStore(tmp_path / "private" / "state.sqlite3") as store:
                executor = CommandExecutor(center, store, backend)
                start = center.request()
                assert await executor.run_once()
                rejected = center.results[start.command_id]
                assert rejected["outcome"] == "failed"
                assert rejected["error"]["code"] == "UNSUPPORTED_PROFILE"
                assert all(call[0] != "run" for call in backend.calls)

                stop = center.request("stop")
                assert await executor.run_once()
                stopped = center.results[stop.command_id]
                assert stopped["outcome"] == "succeeded", stopped["error"]
                assert stopped["runtime_state"] == "stopped"
                assert not backend.containers
        finally:
            await backend.close()

    asyncio.run(scenario())


def test_profile_with_two_http_services_is_rejected_at_start_and_still_stops(
    tmp_path: Path,
) -> None:
    """Регрессия: центр допускает два HTTP-сервиса, Low — нет; stop от состава не зависит.

    Что stop опрашивает каждый порт профиля, детерминированно проверяет
    test_release_probes_every_tcp_and_http_port_of_the_profile.
    """

    async def scenario() -> None:
        center = Center()
        center.services = [
            {"protocol": "http", "port": center.port, "banner": "a"},
            {"protocol": "http", "port": free_port(), "banner": "b"},
        ]
        backend = InProcessBackend()
        try:
            with ExecutionStore(tmp_path / "private" / "state.sqlite3") as store:
                executor = CommandExecutor(center, store, backend)
                start = center.request()
                assert await executor.run_once()
                assert center.results[start.command_id]["error"]["code"] == "UNSUPPORTED_PROFILE"
                assert all(call[0] != "run" for call in backend.calls)

                stop = center.request("stop")
                assert await executor.run_once()
                stopped = center.results[stop.command_id]
                assert stopped["outcome"] == "succeeded", stopped["error"]
                assert stopped["runtime_state"] == "stopped"
        finally:
            await backend.close()

    asyncio.run(scenario())
