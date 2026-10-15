"""Medium использует общие ACK/lease и не принимает публичные адреса стенда."""

import asyncio
import os
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from test_executor import Center

from agent.runtime.contract import CredentialSpec, Secret
from agent.runtime.drivers.base import DriverError
from agent.runtime.drivers.containers import ContainerObservation
from agent.runtime.drivers.low import LowHealthCheck, LowSettings
from agent.runtime.drivers.medium import MediumProfileTranslator
from agent.runtime.executor.client import ClientError
from agent.runtime.executor.engine import CommandExecutor
from agent.runtime.executor.store import ExecutionStore


class MemoryDocker:
    """Подмена Docker хранит точные метки и количество мутаций."""

    def __init__(self):
        self.current = None
        self.runs = 0

    async def inspect(self, name):
        return self.current

    async def run(self, spec):
        self.runs += 1
        self.current = ContainerObservation(
            spec.name, "running", True, None, dict(spec.labels), "a" * 64
        )

    async def stop(self, observed, timeout):
        self.current = replace(observed, running=False, status="exited")

    async def remove(self, observed):
        self.current = None


def center_command():
    center = Center()
    center.level = "medium"
    center.services = [{"protocol": "ssh", "port": 2222}]
    command = center.request()
    center.command = replace(
        command,
        config=replace(command.config, credentials=(CredentialSpec("root", Secret("demo")),)),
    )
    return center


@pytest.mark.parametrize("enabled", [False, True])
def test_medium_start_ack_retry_stop(tmp_path, monkeypatch, enabled):
    # Здесь Docker подменён: root-права для смены владельца каталога не нужны.
    # Production-проверки владельца проверяются отдельно тестами LowDriver.
    if hasattr(os, "chown"):
        monkeypatch.setattr(os, "chown", lambda *args, **kwargs: None)

    async def healthy(self, target):
        assert target.tcp_port == 2222

    async def released(self, target):
        return True

    monkeypatch.setattr(LowHealthCheck, "wait_until_healthy", healthy)
    monkeypatch.setattr(LowHealthCheck, "wait_until_released", released)

    async def scenario():
        center, backend = center_command(), MemoryDocker()
        settings = LowSettings(
            image="honeyforge-medium:local", network="demo", journal_root=tmp_path / "journals"
        )
        with ExecutionStore(tmp_path / "private" / "state.db") as store:
            executor = CommandExecutor(
                center, store, backend, medium_settings=settings if enabled else None
            )
            center.lose_ack = True
            with pytest.raises(ClientError):
                await executor.run_once()
            assert await executor.run_once()
            if not enabled:
                assert backend.runs == 0
                return
            assert backend.runs == 1
            assert next(iter(center.results.values()))["outcome"] == "succeeded"
            assert dict(backend.current.labels)["honeyforge.level"] == "medium"
            stop = center.request("stop")
            center.command = replace(stop, config=replace(stop.config, credentials=()))
            assert await executor.run_once()
            assert backend.current is None

    asyncio.run(scenario())


def test_medium_rejects_public_bind(tmp_path: Path):
    translator = MediumProfileTranslator(
        LowSettings(bind_address="0.0.0.0", journal_root=tmp_path, network="demo")
    )
    with pytest.raises(DriverError):
        translator.blueprint(center_command().command.deployment())


def test_medium_has_separate_name_and_revision(tmp_path: Path, monkeypatch):
    if hasattr(os, "chown"):
        monkeypatch.setattr(os, "chown", lambda *args, **kwargs: None)
    translator = MediumProfileTranslator(LowSettings(journal_root=tmp_path, network="demo"))
    command = center_command().command
    spec = translator.container_spec(translator.blueprint(command.deployment()), str(uuid4()))
    assert spec.name.startswith("honeyforge-medium-")
    assert spec.user == "999:999"
    assert spec.publish[0].host_ip == "127.0.0.1"


def test_medium_reset_changes_instance_and_keeps_ack_bindings(tmp_path, monkeypatch):
    async def healthy(self, target):
        return None

    monkeypatch.setattr(LowHealthCheck, "wait_until_healthy", healthy)
    if hasattr(os, "chown"):
        monkeypatch.setattr(os, "chown", lambda *args, **kwargs: None)

    async def scenario():
        center, backend = center_command(), MemoryDocker()
        settings = LowSettings(network="demo", journal_root=tmp_path / "journals")
        with ExecutionStore(tmp_path / "state" / "executor.db") as store:
            executor = CommandExecutor(center, store, backend, medium_settings=settings)
            await executor.run_once()
            original = backend.current.labels["honeyforge.instance_id"]
            reset = center.request("reset")
            center.command = replace(reset, config=center_command().command.config)
            center.lose_ack = True
            with pytest.raises(ClientError):
                await executor.run_once()
            await executor.run_once()
            assert backend.current.labels["honeyforge.instance_id"] != original
            assert backend.runs == 2
            assert len(store.telemetry_bindings()) == 2

    asyncio.run(scenario())
