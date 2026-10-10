"""Восстановление Low сверяет физическую идентичность, не повторяя мутации."""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Callable
from typing import Any
from uuid import uuid4

import pytest

from agent.runtime.drivers.base import DriverError, DriverErrorCode, DriverState
from agent.runtime.drivers.containers import LABEL_MANAGED, ContainerObservation
from agent.runtime.drivers.low import (
    LABEL_DEPLOYMENT,
    LABEL_INSTANCE,
    LABEL_LEVEL,
    LABEL_REVISION,
    HealthCheckFailed,
    HealthTargets,
    LowDriver,
    LowHealthCheck,
)
from tests.runtime.test_low_driver import (
    DEPLOYMENT_ID,
    NAME,
    OTHER_DEPLOYMENT_ID,
    InProcessBackend,
    deployment,
    free_port,
    settings,
)


def run_recovery(scenario: Callable[[InProcessBackend], Any]) -> None:
    """Каждая регрессия использует настоящий LowService через подмену Docker."""

    async def main() -> None:
        backend = InProcessBackend()
        try:
            await scenario(backend)
        finally:
            await backend.close()

    asyncio.run(main())


def test_recover_reuses_saved_instance_and_checks_actual_low_health() -> None:
    async def scenario(backend: InProcessBackend) -> None:
        planned_instance = str(uuid4())
        config = deployment(tcp_port=free_port(), revision=3)
        original = LowDriver(
            DEPLOYMENT_ID, backend, settings(), new_instance_id=lambda: planned_instance
        )
        await original.start(config)
        events_before = backend.streams[NAME].getvalue()
        backend.calls.clear()
        restarted = LowDriver(DEPLOYMENT_ID, backend, settings())

        restored = await restarted.recover(config, planned_instance)

        assert restored.state is DriverState.RUNNING and restored.healthy
        assert (restored.instance_id, restored.revision) == (planned_instance, 3)
        assert backend.streams[NAME].getvalue() != events_before
        assert [action for action, _ in backend.calls] == ["inspect", "inspect"]
        assert len(backend.containers) == 1

    run_recovery(scenario)


def test_recover_absence_and_exited_container_do_not_start_again() -> None:
    async def scenario(backend: InProcessBackend) -> None:
        config = deployment(tcp_port=free_port())
        instance_id = str(uuid4())
        fresh = LowDriver(DEPLOYMENT_ID, backend, settings())
        status = await fresh.recover(config, instance_id)
        assert status.state is DriverState.STOPPED and not status.healthy
        assert status.instance_id == instance_id and not backend.containers

        launched = LowDriver(
            DEPLOYMENT_ID, backend, settings(), new_instance_id=lambda: instance_id
        )
        await launched.start(config)
        await backend.crash(NAME, 137)
        backend.calls.clear()

        status = await fresh.recover(config, instance_id)

        assert status.state is DriverState.STOPPED and not status.healthy
        assert status.instance_id == instance_id
        assert [action for action, _ in backend.calls] == ["inspect"]

    run_recovery(scenario)


@pytest.mark.parametrize(
    ("label", "value"),
    [
        (LABEL_MANAGED, "false"),
        (LABEL_DEPLOYMENT, OTHER_DEPLOYMENT_ID),
        (LABEL_LEVEL, "medium"),
        (LABEL_INSTANCE, str(uuid4())),
        (LABEL_INSTANCE, "invalid-secret-must-not-appear"),
        (LABEL_REVISION, "2"),
        (LABEL_REVISION, "0"),
        (LABEL_REVISION, "١"),
        (LABEL_REVISION, "invalid-secret-must-not-appear"),
    ],
)
def test_recover_rejects_unexpected_labels_without_probing_or_mutating(
    label: str, value: str
) -> None:
    async def scenario(backend: InProcessBackend) -> None:
        config = deployment(tcp_port=free_port())
        original = LowDriver(DEPLOYMENT_ID, backend, settings())
        started = await original.start(config)
        container = backend.containers[NAME]
        labels = {**container.labels, label: value}
        container.spec = dataclasses.replace(container.spec, labels=tuple(labels.items()))
        events_before = backend.streams[NAME].getvalue()
        backend.calls.clear()
        restarted = LowDriver(DEPLOYMENT_ID, backend, settings())

        with pytest.raises(DriverError) as failure:
            await restarted.recover(config, started.instance_id)

        assert failure.value.code is DriverErrorCode.INVALID_STATE
        assert "invalid-secret" not in failure.value.message
        assert backend.streams[NAME].getvalue() == events_before
        assert [action for action, _ in backend.calls] == ["inspect"]
        assert container.running and backend.containers[NAME] is container

    run_recovery(scenario)


@pytest.mark.parametrize("container_id", [None, "", "a" * 63, "A" * 64])
def test_recover_rejects_missing_immutable_id(container_id: str | None) -> None:
    async def scenario(backend: InProcessBackend) -> None:
        config = deployment(tcp_port=free_port())
        original = LowDriver(DEPLOYMENT_ID, backend, settings())
        started = await original.start(config)
        inspect = backend.inspect

        async def malformed_inspect(name: str) -> ContainerObservation | None:
            observed = await inspect(name)
            return (
                None
                if observed is None
                else dataclasses.replace(observed, container_id=container_id)
            )

        backend.inspect = malformed_inspect  # type: ignore[method-assign]
        restarted = LowDriver(DEPLOYMENT_ID, backend, settings())
        with pytest.raises(DriverError) as failure:
            await restarted.recover(config, started.instance_id)
        assert failure.value.code is DriverErrorCode.INVALID_STATE
        assert backend.containers[NAME].running

    run_recovery(scenario)


def test_recovery_health_failure_leaves_resource_for_a_leased_followup() -> None:
    class FailedHealth(LowHealthCheck):
        async def wait_until_healthy(self, targets: HealthTargets) -> None:
            raise HealthCheckFailed("ловушка не отвечает по профилю")

    async def scenario(backend: InProcessBackend) -> None:
        config = deployment(tcp_port=free_port())
        original = LowDriver(DEPLOYMENT_ID, backend, settings())
        started = await original.start(config)
        backend.calls.clear()
        restarted = LowDriver(DEPLOYMENT_ID, backend, settings(), health=FailedHealth(1, 0.01, 0.1))

        with pytest.raises(DriverError) as failure:
            await restarted.recover(config, started.instance_id)

        assert failure.value.code is DriverErrorCode.HEALTHCHECK_FAILED
        assert (await restarted.inspect()).state is DriverState.FAILED
        actual = await restarted.observe()
        assert actual is not None and actual.running
        assert actual.labels[LABEL_INSTANCE] == started.instance_id
        assert not any(action in ("run", "stop", "remove") for action, _ in backend.calls)

    run_recovery(scenario)


def test_container_id_replacement_during_health_cannot_confirm_running() -> None:
    class ReplacingHealth(LowHealthCheck):
        def __init__(self, backend: InProcessBackend) -> None:
            super().__init__(1, 0.01, 0.1)
            self.backend = backend

        async def wait_until_healthy(self, targets: HealthTargets) -> None:
            # Даже одинаковые UUID/ревизия не подтверждают прежний физический ресурс.
            self.backend.containers[NAME].container_id = "b" * 64

    async def scenario(backend: InProcessBackend) -> None:
        config = deployment(tcp_port=free_port())
        original = LowDriver(DEPLOYMENT_ID, backend, settings())
        started = await original.start(config)
        backend.calls.clear()
        restarted = LowDriver(DEPLOYMENT_ID, backend, settings(), health=ReplacingHealth(backend))
        with pytest.raises(DriverError) as failure:
            await restarted.recover(config, started.instance_id)
        assert failure.value.code is DriverErrorCode.INVALID_STATE
        assert [action for action, _ in backend.calls] == ["inspect", "inspect"]
        assert backend.containers[NAME].running

    run_recovery(scenario)


@pytest.mark.parametrize("label", [LABEL_DEPLOYMENT, LABEL_LEVEL])
def test_managed_wrong_slot_is_never_adopted_or_deleted(label: str) -> None:
    async def scenario(backend: InProcessBackend) -> None:
        config = deployment(tcp_port=free_port())
        original = LowDriver(DEPLOYMENT_ID, backend, settings())
        await original.start(config)
        container = backend.containers[NAME]
        value = OTHER_DEPLOYMENT_ID if label == LABEL_DEPLOYMENT else "medium"
        labels = {**container.labels, label: value}
        container.spec = dataclasses.replace(container.spec, labels=tuple(labels.items()))
        backend.calls.clear()
        restarted = LowDriver(DEPLOYMENT_ID, backend, settings())

        assert (await restarted.inspect()).state is DriverState.FAILED
        with pytest.raises(DriverError):
            await restarted.observe()
        with pytest.raises(DriverError) as failure:
            await restarted.stop()
        assert failure.value.code is DriverErrorCode.CLEANUP_FAILED
        assert container.running and backend.containers[NAME] is container
        assert not any(action in ("run", "stop", "remove") for action, _ in backend.calls)

    run_recovery(scenario)
