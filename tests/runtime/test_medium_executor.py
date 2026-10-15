"""Start и stop Medium через claim/renew/ACK с подтверждением фактического состояния.

Транспорт центра и контейнерный backend подменены (`Center` из test_executor,
`CowrieBackend` из test_medium_driver): проверяется выбор драйвера по уровню, lease
перед мутациями, подтверждение запуска и остановки и коды отказов. Настоящий Cowrie
и настоящий Docker Engine здесь не участвуют.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from test_executor import Center
from test_medium_driver import BANNER, CowrieBackend, container_chown, settings

from agent.runtime.drivers.containers import BackendError, ContainerObservation
from agent.runtime.drivers.low import LowSettings
from agent.runtime.drivers.medium import MediumDriver, MediumSettings
from agent.runtime.drivers.registry import SUPPORTED_LEVELS, DriverRegistry
from agent.runtime.executor.client import ClientError
from agent.runtime.executor.engine import CommandExecutor, LeasedBackend, LeaseGate
from agent.runtime.executor.enroll import EnrollmentClient
from agent.runtime.executor.store import ExecutionStore

# Фикстура подменяет смену владельца: в подмене Docker процесс остаётся нашим.
__all__ = ["container_chown"]


def medium_center() -> Center:
    """Центр присылает профиль medium с баннером, который драйвер может применить."""
    center = Center()
    center.level = "medium"
    center.http_text = BANNER
    return center


def registry(tmp_path: Path, **changes: object) -> DriverRegistry:
    return DriverRegistry(medium=settings(tmp_path / "medium", **changes))


def test_start_and_stop_medium_pass_through_commands(tmp_path: Path) -> None:
    async def scenario() -> None:
        center = medium_center()
        backend = CowrieBackend()
        try:
            with ExecutionStore(tmp_path / "private" / "state.sqlite3") as store:
                executor = CommandExecutor(center, store, backend, registry(tmp_path))
                start = center.request()
                assert await executor.run_once()
                first = center.results[start.command_id]
                assert first["outcome"] == "succeeded"
                assert first["runtime_state"] == "running"
                assert first["applied_revision"] == 1
                # Ловушка действительно отвечает на опубликованном порту хоста.
                reader, writer = await asyncio.open_connection("127.0.0.1", center.port)
                assert await reader.readline() == BANNER.encode("ascii") + b"\r\n"
                writer.close()
                await writer.wait_closed()
                assert backend.containers[f"honeyforge-medium-{center.deployment_id}"]
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


def test_medium_container_carries_its_own_level_label(tmp_path: Path) -> None:
    async def scenario() -> None:
        center = medium_center()
        backend = CowrieBackend()
        try:
            with ExecutionStore(tmp_path / "private" / "state.sqlite3") as store:
                # Узел с двумя настроенными уровнями выбирает драйвер по уровню профиля.
                drivers = DriverRegistry(low=LowSettings(), medium=settings(tmp_path / "medium"))
                executor = CommandExecutor(center, store, backend, drivers)
                assert drivers.levels == ("low", "medium")
                center.request()
                assert await executor.run_once()
                container = backend.containers[f"honeyforge-medium-{center.deployment_id}"]
                assert container.labels["honeyforge.level"] == "medium"
                assert container.labels["honeyforge.deployment_id"] == center.deployment_id
        finally:
            await backend.close()

    asyncio.run(scenario())


def test_remaining_container_after_stop_is_not_reported_as_stopped(tmp_path: Path) -> None:
    async def scenario() -> None:
        center = medium_center()
        backend = CowrieBackend()
        try:
            with ExecutionStore(tmp_path / "private" / "state.sqlite3") as store:
                executor = CommandExecutor(center, store, backend, registry(tmp_path))
                center.request()
                assert await executor.run_once()
                stop = center.request("stop")
                # docker rm отвечает успехом, но контейнер остался в слоте.
                backend.forget_remove = True
                assert await executor.run_once()
                result = center.results[stop.command_id]
                assert result["outcome"] == "failed"
                assert result["error"]["code"] == "STOP_UNCONFIRMED"
                assert result["applied_revision"] == 1
                assert store.history(center.deployment_id).generation == 2
        finally:
            await backend.close()

    asyncio.run(scenario())


def test_busy_port_after_removal_is_not_reported_as_stopped(tmp_path: Path) -> None:
    async def scenario() -> None:
        center = medium_center()
        backend = CowrieBackend()
        try:
            with ExecutionStore(tmp_path / "private" / "state.sqlite3") as store:
                executor = CommandExecutor(
                    center, store, backend, registry(tmp_path, start_timeout=0.3)
                )
                center.request()
                assert await executor.run_once()
                stop = center.request("stop")
                # Контейнера больше нет, но порт продолжает принимать соединения.
                backend.leak_port = True
                assert await executor.run_once()
                result = center.results[stop.command_id]
                assert result["outcome"] == "failed"
                assert result["error"]["code"] == "PORT_STILL_OPEN"
                assert not backend.containers and backend.leaked
                # Чужой процесс на порту не останавливается и не удаляется.
                assert all(call[0] != "run" for call in backend.calls[-2:])
        finally:
            await backend.close()

    asyncio.run(scenario())


def test_medium_command_without_host_settings_never_touches_docker(tmp_path: Path) -> None:
    async def scenario() -> None:
        center = medium_center()
        backend = CowrieBackend()
        with ExecutionStore(tmp_path / "private" / "state.sqlite3") as store:
            # Узел без каталога состояния Cowrie остаётся Low-узлом.
            executor = CommandExecutor(center, store, backend, LowSettings())
            command = center.request()
            assert await executor.run_once()
            result = center.results[command.command_id]
            assert result["error"]["code"] == "UNSUPPORTED_COMMAND"
            assert result["runtime_state"] == "failed"
            assert result["instance_id"] is None and result["applied_revision"] is None
            assert not backend.calls

    asyncio.run(scenario())


def test_reset_action_is_unsupported_for_a_configured_level(tmp_path: Path) -> None:
    async def scenario() -> None:
        center = medium_center()
        backend = CowrieBackend()
        with ExecutionStore(tmp_path / "private" / "state.sqlite3") as store:
            executor = CommandExecutor(center, store, backend, registry(tmp_path))
            command = center.request("reset")
            assert await executor.run_once()
            result = center.results[command.command_id]
            assert result["error"]["code"] == "UNSUPPORTED_COMMAND"
            assert not backend.calls

    asyncio.run(scenario())


def test_leased_backend_refuses_a_container_of_another_level() -> None:
    async def scenario() -> None:
        center = medium_center()
        command = center.request()
        instance = str(uuid4())
        leased = LeasedBackend(
            CowrieBackend(), LeaseGate(command), command.deployment_id, {instance}, 1, "medium"
        )
        labels = {
            "honeyforge.managed": "true",
            "honeyforge.deployment_id": command.deployment_id,
            "honeyforge.level": "low",
            "honeyforge.instance_id": instance,
            "honeyforge.revision": "1",
        }
        observed = ContainerObservation(
            "honeyforge-low-x", "running", True, None, labels, container_id="a" * 64
        )
        with pytest.raises(BackendError) as failure:
            await leased.remove(observed)
        assert failure.value.message == "INSTANCE_MISMATCH"

    asyncio.run(scenario())


def test_registry_reports_only_configured_levels(tmp_path: Path) -> None:
    assert DriverRegistry().levels == ("low",)
    assert not DriverRegistry().supports("medium")
    with pytest.raises(ValueError):
        DriverRegistry().create("medium", str(uuid4()), CowrieBackend(), lambda: str(uuid4()))
    with pytest.raises(ValueError):
        DriverRegistry().create("high", str(uuid4()), CowrieBackend(), lambda: str(uuid4()))
    configured = DriverRegistry(medium=MediumSettings(state_root=tmp_path / "medium"))
    assert configured.levels == ("low", "medium")
    driver = configured.create("medium", str(uuid4()), CowrieBackend(), lambda: str(uuid4()))
    assert isinstance(driver, MediumDriver) and driver.level == "medium"


def test_enrollment_capabilities_follow_supported_levels() -> None:
    assert SUPPORTED_LEVELS == ("low", "medium")
    assert EnrollmentClient.capabilities(["low"]) == ["low"]
    # Порядок канонический и не зависит от порядка аргументов оператора.
    assert EnrollmentClient.capabilities(["medium", "low", "medium"]) == ["low", "medium"]
    for levels in ([], ["high"], ["low", "unknown"]):
        with pytest.raises(ClientError) as failure:
            EnrollmentClient.capabilities(levels)
        assert failure.value.code == "INVALID_REQUEST"
