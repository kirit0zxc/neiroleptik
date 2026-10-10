"""Исполнение Low с долговечным ACK и отдельным разрешением на мутации Docker."""

from __future__ import annotations

import asyncio
import math
import time
from datetime import UTC, datetime

from agent.runtime.drivers.base import DriverError, DriverState
from agent.runtime.drivers.containers import (
    LABEL_MANAGED,
    BackendError,
    ContainerBackend,
    ContainerObservation,
    ContainerSpec,
)
from agent.runtime.drivers.low import (
    LABEL_DEPLOYMENT,
    LABEL_INSTANCE,
    LABEL_LEVEL,
    LABEL_REVISION,
    LowDriver,
    LowSettings,
)
from agent.runtime.executor.client import ClientError, CommandClient
from agent.runtime.executor.protocol import ClaimedCommand, LeaseReply
from agent.runtime.executor.store import ExecutionStore, StoredCommand


class ExecutionError(Exception):
    """Безопасная причина отказа, пригодная для локальной диагностики."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class LeaseGate:
    """Срок по двум часам: перевод системных часов назад не продлевает разрешение."""

    def __init__(self, command: ClaimedCommand) -> None:
        self.lost = False
        self.execution_expires_at = command.execution_expires_at
        self.lease_expires_at = command.lease_expires_at
        now = time.monotonic()
        duration = (command.execution_expires_at - datetime.now(UTC)).total_seconds()
        maximum = 900.0 if command.config.interaction_level == "high" else 300.0
        self._execution_deadline = now + max(0.0, min(maximum, duration))
        self._deadline = 0.0
        self.update(command.lease_expires_at, command.execution_expires_at, now)

    def update(self, lease: datetime, execution: datetime, request_started: float) -> None:
        """Hard deadline не меняется; задержка сети уменьшает доступное время."""
        if execution != self.execution_expires_at:
            self.lost = True
            raise ExecutionError("EXECUTION_DEADLINE_CHANGED")
        self.lease_expires_at = min(lease, execution)
        seconds = (self.lease_expires_at - datetime.now(UTC)).total_seconds()
        self._deadline = min(
            self._execution_deadline, request_started + max(0.0, min(60.0, seconds))
        )

    @property
    def valid(self) -> bool:
        return (
            not self.lost
            and time.monotonic() < self._deadline
            and datetime.now(UTC) < self.lease_expires_at
        )

    def require(self) -> None:
        """Закрытая аренда запрещает следующую мутацию, но не отменяет начатую."""
        if not self.valid:
            self.lost = True
            raise BackendError("LEASE_LOST")


class LeasedBackend:
    """Проверяет lease и идентичность перед каждой отдельной мутацией backend."""

    def __init__(
        self,
        backend: ContainerBackend,
        gate: LeaseGate,
        deployment_id: str,
        allowed_instances: set[str],
        revision: int,
    ) -> None:
        self._backend = backend
        self._gate = gate
        self._deployment_id = deployment_id
        self._allowed_instances = allowed_instances
        self._revision = revision

    def _authorize(self, labels: dict[str, str]) -> None:
        self._gate.require()
        if (
            labels.get(LABEL_MANAGED) != "true"
            or labels.get(LABEL_DEPLOYMENT) != self._deployment_id
            or labels.get(LABEL_LEVEL) != "low"
            or labels.get(LABEL_INSTANCE) not in self._allowed_instances
            or labels.get(LABEL_REVISION) != str(self._revision)
        ):
            raise BackendError("INSTANCE_MISMATCH")

    async def run(self, spec: ContainerSpec) -> None:
        """Backend не получает запускающий вызов после закрытия разрешения."""
        self._authorize(dict(spec.labels))
        await self._backend.run(spec)

    async def inspect(self, name: str) -> ContainerObservation | None:
        """Наблюдение не требует lease и не меняет Docker."""
        return await self._backend.inspect(name)

    async def stop(self, name: str | ContainerObservation, timeout: int) -> None:
        """Executor принимает только наблюдение, сохраняющее неизменяемый ID."""
        if not isinstance(name, ContainerObservation):
            raise BackendError("INSTANCE_MISMATCH")
        self._authorize(dict(name.labels))
        await self._backend.stop(name, timeout)

    async def remove(self, name: str | ContainerObservation) -> None:
        """Новая проверка не разрешает remove после потери lease во время stop."""
        if not isinstance(name, ContainerObservation):
            raise BackendError("INSTANCE_MISMATCH")
        self._authorize(dict(name.labels))
        await self._backend.remove(name)


class CommandExecutor:
    """Один владелец durable store, одна физическая операция и один итог на команду."""

    def __init__(
        self,
        client: CommandClient,
        store: ExecutionStore,
        backend: ContainerBackend,
        settings: LowSettings | None = None,
        *,
        renew_interval: float = 20.0,
    ) -> None:
        if (
            isinstance(renew_interval, bool)
            or not math.isfinite(renew_interval)
            or not 0 < renew_interval <= 20
        ):
            raise ValueError("renew_interval должен быть от 0 до 20 секунд")
        self._client = client
        self._store = store
        self._backend = backend
        self._settings = settings or LowSettings()
        self._renew_interval = renew_interval
        self._lock = asyncio.Lock()

    async def run_once(self) -> bool:
        """Обрабатывает сохранённую операцию раньше следующей команды центра."""
        async with self._lock:
            # Отмена внешнего polling не даёт другому run_once обогнать Docker.
            task = asyncio.create_task(self._run_once())
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if not task.cancelled():
                    task.exception()
                raise

    async def _claim(self, command_id: str | None) -> ClaimedCommand | None:
        try:
            return await self._client.claim(command_id)
        except ClientError as error:
            if command_id is not None and error.code == "COMMAND_FINISHED":
                self._store.abandon(command_id)
                return None
            raise

    async def _run_once(self) -> bool:
        record = self._store.pending()
        if record is not None and record.result is not None:
            return await self._deliver(record)
        command = await self._claim(record.command_id if record is not None else None)
        if command is None:
            return False
        if not self._store.has_history(command.deployment_id):
            config = await self._client.config()
            deployment = next(
                (d for d in config.deployments if d.deployment_id == command.deployment_id), None
            )
            if (
                deployment is None
                or deployment.instance_id != command.expected_instance_id
                or deployment.revision != command.revision
                or deployment.generation != command.generation
            ):
                raise ExecutionError("CONFIG_MISMATCH")
            self._store.seed_history(
                command.deployment_id,
                command.expected_generation,
                deployment.instance_id,
                deployment.applied_revision,
            )
        record = self._store.accept(command.identity(), command.fingerprint, command.lease_token)
        self._store.update_lease(record.command_id, command.lease_token)
        record = self._store.get(record.command_id)
        assert record is not None
        if record.result is not None:
            return await self._deliver(record)
        return await self._execute(command, record)

    def _validate_renew(self, command: ClaimedCommand, reply: LeaseReply) -> None:
        if reply.command_id != command.command_id:
            raise ExecutionError("LEASE_RESPONSE_MISMATCH")

    async def _renew_loop(self, command: ClaimedCommand, gate: LeaseGate) -> None:
        try:
            while True:
                # Срок может быть короче обычного интервала в конце hard deadline.
                remaining = (gate.lease_expires_at - datetime.now(UTC)).total_seconds()
                await asyncio.sleep(max(0.001, min(self._renew_interval, remaining / 3)))
                gate.require()
                started = time.monotonic()
                reply = await self._client.renew(command.command_id, command.lease_token)
                self._validate_renew(command, reply)
                gate.update(reply.lease_expires_at, reply.execution_expires_at, started)
        except (ClientError, ExecutionError, BackendError):
            gate.lost = True

    async def _execute(self, command: ClaimedCommand, record: StoredCommand) -> bool:
        gate = LeaseGate(command)
        if not gate.valid:
            return False
        started = time.monotonic()
        reply = await self._client.renew(command.command_id, command.lease_token)
        self._validate_renew(command, reply)
        gate.update(reply.lease_expires_at, reply.execution_expires_at, started)
        worker = asyncio.create_task(self._renew_loop(command, gate))
        try:
            result = await self._perform(command, record, gate)
            if not gate.valid:
                # Оставляем executing для сверки после новой выдачи, без старого ACK.
                return False
            record = self._store.complete(record.command_id, result)
            return await self._deliver(record, reclaim=False)
        finally:
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass

    async def _perform(
        self, command: ClaimedCommand, record: StoredCommand, gate: LeaseGate
    ) -> dict[str, object]:
        history = self._store.history(command.deployment_id)
        result: dict[str, object] = {
            "deployment_id": command.deployment_id,
            "generation": command.generation,
            "applied_revision": history.applied_revision,
            "instance_id": history.instance_id,
            "outcome": "failed",
            "runtime_state": "failed",
            "error": {"code": "UNSUPPORTED_COMMAND", "message": "Поддерживаются start/stop Low"},
        }
        if command.config.interaction_level != "low" or command.action not in ("start", "stop"):
            return result
        allowed = {i for i in (history.instance_id, record.attempt_instance_id) if i is not None}
        leased = LeasedBackend(
            self._backend, gate, command.deployment_id, allowed, command.revision
        )
        driver = LowDriver(
            command.deployment_id,
            leased,
            self._settings,
            new_instance_id=lambda: record.attempt_instance_id or "",
        )
        try:
            observed = await driver.observe()
            if observed is not None and observed.labels[LABEL_REVISION] != str(command.revision):
                raise ExecutionError("INSTANCE_MISMATCH")
            if command.action == "stop" and command.reconcile and observed is not None:
                instance = observed.labels[LABEL_INSTANCE]
                allowed.add(instance)
                result["instance_id"] = instance
                result["applied_revision"] = int(observed.labels[LABEL_REVISION])
            elif (
                not (command.action == "stop" and command.reconcile)
                and history.instance_id != command.expected_instance_id
            ):
                raise ExecutionError("INSTANCE_MISMATCH")
            if observed is not None and observed.labels[LABEL_INSTANCE] not in allowed:
                raise ExecutionError("INSTANCE_MISMATCH")
            gate.require()
            if command.action == "start":
                if record.phase == "executing":
                    status = await driver.recover(command.deployment(), record.attempt_instance_id)
                    if status.state != DriverState.RUNNING or not status.healthy:
                        raise ExecutionError("RECOVERY_UNCERTAIN")
                else:
                    self._store.mark_executing(record.command_id)
                    status = await driver.start(command.deployment())
                confirmed = await driver.observe()
                if (
                    confirmed is None
                    or not confirmed.running
                    or confirmed.labels[LABEL_INSTANCE] != record.attempt_instance_id
                    or confirmed.labels[LABEL_REVISION] != str(command.revision)
                ):
                    raise ExecutionError("START_UNCONFIRMED")
                result["instance_id"] = status.instance_id
                result["applied_revision"] = command.revision
                result["runtime_state"] = "running"
            else:
                self._store.mark_executing(record.command_id)
                await driver.stop()
                if await driver.observe() is not None:
                    raise ExecutionError("STOP_UNCONFIRMED")
                result["runtime_state"] = "stopped"
            result["outcome"] = "succeeded"
            result["error"] = None
            return result
        except (DriverError, BackendError, ExecutionError) as error:
            code = (
                error.code.value
                if isinstance(error, DriverError)
                else (error.code if isinstance(error, ExecutionError) else "LEASE_LOST")
            )
            result["error"] = {"code": code, "message": "Операция Low не подтверждена"}
            try:
                observed = await driver.observe()
            except DriverError:
                observed = None
            if observed is not None:
                instance = observed.labels[LABEL_INSTANCE]
                if (
                    command.action == "stop"
                    and instance == result["instance_id"]
                    and result["applied_revision"] is not None
                    and observed.labels[LABEL_REVISION] == str(command.revision)
                ):
                    result["runtime_state"] = "running" if observed.running else "failed"
                elif (
                    instance == command.expected_instance_id
                    and observed.running
                    and result["applied_revision"] is not None
                    and observed.labels[LABEL_REVISION] == str(command.revision)
                ):
                    result["runtime_state"] = "running"
                elif instance == record.attempt_instance_id:
                    # Неуспешная попытка известна, но не стирает историю применения.
                    # Без прежнего успешного запуска applied_revision остаётся null.
                    result["instance_id"] = instance
            return result

    async def _deliver(self, record: StoredCommand, *, reclaim: bool = True) -> bool:
        if record.result is None:
            raise ExecutionError("MISSING_RESULT")
        try:
            await self._client.ack(
                record.command_id, {**record.result, "lease_token": record.lease_token}
            )
        except ClientError as error:
            if reclaim and error.code in ("LEASE_EXPIRED", "LEASE_MISMATCH"):
                command = await self._claim(record.command_id)
                if command is None:
                    return False
                # Смена token не разрешает заменить сохранённое тело ACK.
                accepted = self._store.accept(
                    command.identity(), command.fingerprint, command.lease_token
                )
                self._store.update_lease(record.command_id, command.lease_token)
                updated = self._store.get(accepted.command_id)
                assert updated is not None
                return await self._deliver(updated, reclaim=False)
            raise
        self._store.acknowledge(record.command_id)
        return True
