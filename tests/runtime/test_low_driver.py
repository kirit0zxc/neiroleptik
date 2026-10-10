"""LowDriver на подмене Docker, которая запускает настоящий LowService по тем же аргументам."""

from __future__ import annotations

import asyncio
import dataclasses
import io
import json
import os
import socket
import stat
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest

from agent.runtime.contract import (
    ChannelSpec,
    CredentialSpec,
    DeploymentSpec,
    LoggingSpec,
    ProfileSpec,
    Secret,
    ServiceSpec,
)
from agent.runtime.drivers import low as low_module
from agent.runtime.drivers.base import DriverError, DriverErrorCode, DriverState, DriverStatus
from agent.runtime.drivers.containers import (
    LABEL_MANAGED,
    BackendError,
    ContainerObservation,
    ContainerSpec,
)
from agent.runtime.drivers.low import (
    DEFAULT_JOURNAL_FILE_BYTES,
    DEFAULT_JOURNAL_QUOTA_BYTES,
    LABEL_INSTANCE,
    MIN_JOURNAL_FILE_BYTES,
    HealthCheckFailed,
    LowDriver,
    LowHealthCheck,
    LowProfileTranslator,
    LowSettings,
)
from honeypots.low import JsonLineEventSink, LowService
from honeypots.low.__main__ import build_parser, config_from_args, journal_from_args
from honeypots.low.journal import (
    DEFAULT_FILE_BYTES,
    DEFAULT_QUOTA_BYTES,
    MIN_FILE_BYTES,
    RotatingJournalSink,
)

DEPLOYMENT_ID = "11111111-1111-4111-8111-111111111111"
OTHER_DEPLOYMENT_ID = "99999999-9999-4999-8999-999999999999"
NAME = f"honeyforge-low-{DEPLOYMENT_ID}"


@pytest.fixture(autouse=True)
def container_chown(monkeypatch: pytest.MonkeyPatch) -> Mock:
    """В подмене Docker процесс остаётся нашим: реальные владельцы файлов не меняются."""
    chown = Mock()
    monkeypatch.setattr(low_module.os, "chown", chown, raising=False)
    return chown


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def services(
    tcp_port: int | None, http_port: int | None, **banners: str
) -> tuple[ServiceSpec, ...]:
    items: list[ServiceSpec] = []
    if tcp_port is not None:
        items.append(ServiceSpec("tcp", tcp_port, banners.get("tcp", "SSH-2.0-OpenSSH_9.6")))
    if http_port is not None:
        items.append(ServiceSpec("http", http_port, banners.get("http", "Welcome")))
    return tuple(items)


def deployment(
    *,
    revision: int = 1,
    tcp_port: int | None = None,
    http_port: int | None = None,
    level: str = "low",
    deployment_id: str = DEPLOYMENT_ID,
    service_list: tuple[ServiceSpec, ...] | None = None,
    **banners: str,
) -> DeploymentSpec:
    chosen = service_list if service_list is not None else services(tcp_port, http_port, **banners)
    profile = ProfileSpec(
        interaction_level=level,  # type: ignore[arg-type]
        hostname="edge",
        services=chosen,
        logging=LoggingSpec(2048),
        channel=ChannelSpec(1500, 100),
        high=None,
        credentials=(CredentialSpec("admin", Secret("never-in-containers")),),
    )
    return DeploymentSpec(deployment_id, str(uuid4()), revision, "planned", profile)


@dataclass
class FakeContainer:
    spec: ContainerSpec
    service: LowService | None
    running: bool = True
    exit_code: int | None = None
    sink: RotatingJournalSink | None = None
    container_id: str = field(default_factory=lambda: uuid4().hex + uuid4().hex)

    @property
    def labels(self) -> dict[str, str]:
        return dict(self.spec.labels)


@dataclass
class InProcessBackend:
    """Подмена Docker. Аргументы разбирает настоящий CLI Low, ловушкой служит LowService."""

    containers: dict[str, FakeContainer] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)
    mutation_targets: list[tuple[str, str | ContainerObservation]] = field(default_factory=list)
    streams: dict[str, io.StringIO] = field(default_factory=dict)
    max_simultaneous: int = 0
    run_error: BackendError | None = None
    run_error_after_creation: BackendError | None = None
    remove_error: BackendError | None = None
    inspect_error: BackendError | None = None
    stop_error: BackendError | None = None
    listen: bool = True
    tamper_banner: str | None = None
    run_delay: float = 0.0

    async def run(self, spec: ContainerSpec) -> None:
        self.calls.append(("run", spec.name))
        if self.run_delay:
            await asyncio.sleep(self.run_delay)
        if self.run_error is not None:
            raise self.run_error
        if spec.name in self.containers:
            raise BackendError("имя контейнера уже занято")
        command = list(spec.command)
        if self.tamper_banner is not None:
            command = [
                f"--banner={self.tamper_banner}" if part.startswith("--banner=") else part
                for part in command
            ]
        config = config_from_args(build_parser().parse_args(command))
        published = {item.container_port: item for item in spec.publish}
        host = spec.publish[0].host_ip if spec.publish else "127.0.0.1"
        tcp = published.get(config.tcp_port)
        http = published.get(config.http_port)
        config = dataclasses.replace(
            config,
            host=host,
            tcp_port=tcp.host_port if tcp else 0,
            http_port=http.host_port if http else 0,
        )
        service: LowService | None = None
        journal_sink: RotatingJournalSink | None = None
        if self.listen:
            stream = io.StringIO()
            self.streams[spec.name] = stream
            parsed = build_parser().parse_args(command)
            journal = journal_from_args(parsed)
            sink: Any = JsonLineEventSink(stream)
            if journal is not None:
                # Docker подставил бы каталог хоста на место /journal; делаем то же самое.
                mount = next(m for m in spec.mounts if Path(m.target) == journal.directory)
                journal_sink = RotatingJournalSink(
                    dataclasses.replace(journal, directory=Path(mount.source))
                )
                journal_sink.open()
                sink = journal_sink
            service = LowService(config, sink)
            await service.start()
        self.containers[spec.name] = FakeContainer(spec, service, sink=journal_sink)
        self.max_simultaneous = max(self.max_simultaneous, len(self.containers))
        if self.run_error_after_creation is not None:
            raise self.run_error_after_creation

    async def inspect(self, name: str) -> ContainerObservation | None:
        self.calls.append(("inspect", name))
        if self.inspect_error is not None:
            raise self.inspect_error
        container = self.containers.get(name)
        if container is None:
            return None
        return ContainerObservation(
            name,
            "running" if container.running else "exited",
            container.running,
            container.exit_code,
            container.labels,
            container_id=container.container_id,
        )

    def _target_key(self, target: str | ContainerObservation) -> str | None:
        """Снимок адресует только прежний ID; замена контейнера по имени не влияет на поиск."""
        if isinstance(target, str):
            return target if target in self.containers else None
        return next(
            (
                key
                for key, container in self.containers.items()
                if container.container_id == target.container_id
            ),
            None,
        )

    async def stop(self, target: str | ContainerObservation, timeout: int) -> None:
        """Как docker stop: процесс получает сигнал и корректно завершается."""
        name = target if isinstance(target, str) else target.name
        self.calls.append(("stop", name))
        self.mutation_targets.append(("stop", target))
        if self.stop_error is not None:
            raise self.stop_error
        key = self._target_key(target)
        container = self.containers.get(key) if key is not None else None
        if container is not None and container.running:
            if container.service is not None:
                await container.service.stop()
            if container.sink is not None:
                container.sink.close()
            container.running = False
            container.exit_code = 0

    async def remove(self, target: str | ContainerObservation) -> None:
        name = target if isinstance(target, str) else target.name
        self.calls.append(("remove", name))
        self.mutation_targets.append(("remove", target))
        if self.remove_error is not None:
            raise self.remove_error
        key = self._target_key(target)
        container = self.containers.pop(key, None) if key is not None else None
        if container is not None and container.running:
            if container.service is not None:
                await container.service.stop()
            if container.sink is not None:
                container.sink._abandon()

    async def crash(self, name: str, exit_code: int = 137) -> None:
        container = self.containers[name]
        if container.running:
            if container.service is not None:
                await container.service.stop()
            if container.sink is not None:
                container.sink._abandon()
        container.running = False
        container.exit_code = exit_code

    def add_foreign(self, name: str) -> None:
        spec = ContainerSpec(name=name, image="other:1", command=(), labels=(("x", "y"),))
        self.containers[name] = FakeContainer(spec, None)

    async def close(self) -> None:
        for name in list(self.containers):
            await self.remove(name)


def settings(**changes: Any) -> LowSettings:
    values: dict[str, Any] = {"start_timeout": 3.0, "probe_interval": 0.02, "probe_timeout": 1.0}
    values.update(changes)
    return LowSettings(**values)


def run_scenario(
    scenario: Callable[[InProcessBackend, LowDriver], Awaitable[None]],
    *,
    backend: InProcessBackend | None = None,
    driver_settings: LowSettings | None = None,
    health: LowHealthCheck | None = None,
) -> None:
    async def main() -> None:
        fake = backend if backend is not None else InProcessBackend()
        driver = LowDriver(DEPLOYMENT_ID, fake, driver_settings or settings(), health=health)
        try:
            await scenario(fake, driver)
        finally:
            fake.run_error = fake.remove_error = fake.inspect_error = fake.stop_error = None
            await fake.close()

    asyncio.run(main())


async def fetch(port: int, request: bytes = b"") -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        if request:
            writer.write(request)
            await writer.drain()
            return await asyncio.wait_for(reader.read(), timeout=2)
        return await asyncio.wait_for(reader.readline(), timeout=2)
    finally:
        writer.close()


async def is_closed(port: int) -> bool:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), timeout=1)
    except OSError:
        return True
    writer.close()
    return False


# --- Перевод профиля ---------------------------------------------------------------------


def translator() -> LowProfileTranslator:
    return LowProfileTranslator(LowSettings(bind_address="127.0.0.1"))


def test_blueprint_command_is_built_from_the_profile() -> None:
    plan = translator().blueprint(deployment(tcp_port=2200, http_port=8800, revision=4))

    assert plan.revision == 4
    assert plan.command == (
        "--host=0.0.0.0",
        "--tcp-port=2222",
        "--http-port=8080",
        "--max-input-bytes=2048",
        "--tcp-public-port=2200",
        "--banner=SSH-2.0-OpenSSH_9.6",
        "--http-public-port=8800",
        "--http-body=Welcome",
    )


def test_container_spec_publishes_profile_ports_on_the_bind_address_only() -> None:
    plan = translator().blueprint(deployment(tcp_port=2200, http_port=8800))
    instance = str(uuid4())

    container = translator().container_spec(plan, instance)

    assert container.name == NAME
    assert [(p.host_ip, p.host_port, p.container_port) for p in container.publish] == [
        ("127.0.0.1", 2200, 2222),
        ("127.0.0.1", 8800, 8080),
    ]
    labels = dict(container.labels)
    assert labels["honeyforge.managed"] == "true"
    assert labels["honeyforge.deployment_id"] == DEPLOYMENT_ID
    assert labels["honeyforge.instance_id"] == instance
    assert labels["honeyforge.revision"] == "1"


def test_only_http_profile_does_not_publish_tcp() -> None:
    plan = translator().blueprint(deployment(http_port=8800))

    container = translator().container_spec(plan, str(uuid4()))

    assert [p.container_port for p in container.publish] == [8080]
    assert not any(part.startswith("--banner") for part in plan.command)


def test_profile_without_http_text_uses_the_default_page() -> None:
    plan = translator().blueprint(deployment(tcp_port=2200, http_port=8800, http=""))

    assert not any(part.startswith("--http-body") for part in plan.command)


def test_passwords_never_reach_the_container_description() -> None:
    plan = translator().blueprint(deployment(tcp_port=2200))
    container = translator().container_spec(plan, str(uuid4()))

    assert "never-in-containers" not in repr(plan) + repr(container)


@pytest.mark.parametrize(
    "case",
    [
        pytest.param({"level": "medium", "tcp_port": 2200}, id="medium-level"),
        pytest.param({"service_list": (ServiceSpec("ssh", 22, "x"),)}, id="ssh-service"),
        pytest.param(
            {"service_list": (ServiceSpec("tcp", 1, "a"), ServiceSpec("tcp", 2, "b"))},
            id="two-tcp",
        ),
        pytest.param(
            {"service_list": (ServiceSpec("http", 1, "a"), ServiceSpec("http", 2, "b"))},
            id="two-http",
        ),
        pytest.param({"service_list": (ServiceSpec("tcp", 1, ""),)}, id="empty-tcp-banner"),
        pytest.param({"service_list": (ServiceSpec("tcp", 1, "Сервис"),)}, id="non-ascii-banner"),
        pytest.param({"service_list": (ServiceSpec("tcp", 1, "a\x1bb"),)}, id="escape-in-banner"),
        pytest.param(
            {"service_list": (ServiceSpec("http", 1, "a\tb"),)}, id="control-in-http-text"
        ),
    ],
)
def test_unsupported_profiles_are_refused_not_distorted(case: dict[str, Any]) -> None:
    with pytest.raises(DriverError) as error:
        translator().blueprint(deployment(**case))

    assert error.value.code is DriverErrorCode.UNSUPPORTED_PROFILE


def test_wildcard_bind_is_probed_through_loopback() -> None:
    for bind, expected in (("0.0.0.0", "127.0.0.1"), ("::", "::1"), ("10.1.2.3", "10.1.2.3")):
        t = LowProfileTranslator(LowSettings(bind_address=bind))
        plan = t.blueprint(deployment(tcp_port=2200, http_port=8800))
        assert t.health_targets(plan).host == expected


@pytest.mark.parametrize(
    "changes",
    [
        {"bind_address": "localhost"},
        {"start_timeout": 0},
        {"probe_interval": -1},
        {"probe_timeout": True},
        {"journal_root": "relative/dir"},
        {"journal_max_bytes": 0},
        {"journal_max_bytes": MIN_FILE_BYTES - 1},
        {"journal_max_bytes": True},
        {"journal_max_bytes": 300000, "journal_quota_bytes": 299999},
        {"journal_quota_bytes": DEFAULT_FILE_BYTES - 1},
        {"journal_max_bytes": DEFAULT_QUOTA_BYTES + 1},
        {"journal_quota_bytes": True},
        {"image": "--privileged"},
        {"image": "two words"},
        {"network": "host"},
        {"network": "none"},
        {"stop_timeout": 26},
        {"stop_timeout": True},
    ],
)
def test_invalid_settings_are_rejected(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        LowSettings(**changes)


@pytest.mark.parametrize("name", ["start_timeout", "probe_interval", "probe_timeout"])
@pytest.mark.parametrize(
    "value",
    [
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="positive-infinity"),
        pytest.param(float("-inf"), id="negative-infinity"),
        pytest.param(10**1000, id="overflowing-int"),
    ],
)
def test_health_check_timeouts_must_be_finite(name: str, value: int | float) -> None:
    with pytest.raises(ValueError, match="конечным положительным"):
        LowSettings(**{name: value})


def test_driver_status_invariants() -> None:
    with pytest.raises(ValueError):
        DriverStatus(DriverState.STOPPED, healthy=True)
    with pytest.raises(ValueError):
        DriverStatus(DriverState.RUNNING, instance_id="nope")


# --- Жизненный цикл ----------------------------------------------------------------------


def test_start_runs_a_real_trap_and_reports_a_fresh_instance() -> None:
    tcp, http = free_port(), free_port()

    async def scenario(backend: InProcessBackend, driver: LowDriver) -> None:
        status = await driver.start(deployment(revision=7, tcp_port=tcp, http_port=http))

        assert (status.state, status.healthy, status.revision) == (DriverState.RUNNING, True, 7)
        assert status.instance_id is not None and UUID(status.instance_id)
        assert await fetch(tcp) == b"SSH-2.0-OpenSSH_9.6\r\n"
        page = await fetch(http, b"GET / HTTP/1.0\r\n\r\n")
        assert page.startswith(b"HTTP/1.1 200") and page.endswith(b"Welcome\n")
        assert backend.containers[NAME].labels[LABEL_INSTANCE] == status.instance_id
        assert [name for name, _ in backend.calls[:2]] == ["inspect", "run"]

    run_scenario(scenario)


def test_start_twice_is_an_invalid_state() -> None:
    tcp = free_port()

    async def scenario(backend: InProcessBackend, driver: LowDriver) -> None:
        await driver.start(deployment(tcp_port=tcp))
        with pytest.raises(DriverError) as error:
            await driver.start(deployment(tcp_port=tcp))
        assert error.value.code is DriverErrorCode.INVALID_STATE
        assert (await driver.inspect()).state is DriverState.RUNNING

    run_scenario(scenario)


def test_apply_config_replaces_the_instance_and_moves_to_new_ports() -> None:
    old_tcp, new_tcp = free_port(), free_port()

    async def scenario(backend: InProcessBackend, driver: LowDriver) -> None:
        first = await driver.start(deployment(revision=1, tcp_port=old_tcp))
        second = await driver.apply_config(
            deployment(revision=2, tcp_port=new_tcp, tcp="SSH-2.0-B")
        )

        assert second.revision == 2 and second.healthy
        assert second.instance_id != first.instance_id
        assert await fetch(new_tcp) == b"SSH-2.0-B\r\n"
        assert await is_closed(old_tcp)
        assert len(backend.containers) == 1

    run_scenario(scenario)


def test_unsupported_new_profile_does_not_destroy_the_working_instance() -> None:
    tcp = free_port()

    async def scenario(backend: InProcessBackend, driver: LowDriver) -> None:
        before = await driver.start(deployment(revision=1, tcp_port=tcp))
        bad = deployment(revision=2, service_list=(ServiceSpec("ssh", 22, "x"),))

        with pytest.raises(DriverError) as error:
            await driver.apply_config(bad)

        assert error.value.code is DriverErrorCode.UNSUPPORTED_PROFILE
        after = await driver.inspect()
        assert (after.state, after.healthy, after.revision) == (DriverState.RUNNING, True, 1)
        assert after.instance_id == before.instance_id
        assert await fetch(tcp) == b"SSH-2.0-OpenSSH_9.6\r\n"
        assert ("remove", NAME) not in backend.calls

    run_scenario(scenario)


def test_reset_recreates_the_instance_with_the_same_profile() -> None:
    tcp = free_port()

    async def scenario(backend: InProcessBackend, driver: LowDriver) -> None:
        first = await driver.start(deployment(revision=5, tcp_port=tcp))
        second = await driver.reset()

        assert second.instance_id != first.instance_id
        assert (second.revision, second.healthy) == (5, True)
        assert await fetch(tcp) == b"SSH-2.0-OpenSSH_9.6\r\n"

    run_scenario(scenario)


def test_stop_is_idempotent_keeps_the_last_instance_and_allows_restart() -> None:
    tcp = free_port()

    async def scenario(backend: InProcessBackend, driver: LowDriver) -> None:
        started = await driver.start(deployment(tcp_port=tcp))
        stopped = await driver.stop()
        again = await driver.stop()

        assert stopped == again
        assert (stopped.state, stopped.healthy) == (DriverState.STOPPED, False)
        assert stopped.instance_id == started.instance_id
        assert await is_closed(tcp) and not backend.containers

        restarted = await driver.start(deployment(tcp_port=tcp))
        assert restarted.instance_id != started.instance_id

    run_scenario(scenario)


def test_stop_of_a_never_started_slot_is_safe() -> None:
    async def scenario(backend: InProcessBackend, driver: LowDriver) -> None:
        status = await driver.stop()
        assert status == DriverStatus(DriverState.STOPPED)

    run_scenario(scenario)


@pytest.mark.parametrize("operation", ["reset", "apply_config"])
def test_operations_that_need_a_running_slot_refuse_a_stopped_one(operation: str) -> None:
    async def scenario(backend: InProcessBackend, driver: LowDriver) -> None:
        with pytest.raises(DriverError) as error:
            if operation == "reset":
                await driver.reset()
            else:
                await driver.apply_config(deployment(tcp_port=free_port()))
        assert error.value.code is DriverErrorCode.INVALID_STATE
        assert not backend.calls

    run_scenario(scenario)


def test_deployment_of_another_slot_is_refused() -> None:
    async def scenario(backend: InProcessBackend, driver: LowDriver) -> None:
        with pytest.raises(DriverError) as error:
            await driver.start(deployment(tcp_port=free_port(), deployment_id=OTHER_DEPLOYMENT_ID))
        assert error.value.code is DriverErrorCode.INVALID_STATE
        assert not backend.calls

    run_scenario(scenario)


# --- Отказы ------------------------------------------------------------------------------


def test_silent_container_fails_health_check_and_is_removed() -> None:
    tcp = free_port()
    backend = InProcessBackend(listen=False)

    async def scenario(fake: InProcessBackend, driver: LowDriver) -> None:
        with pytest.raises(DriverError) as error:
            await driver.start(deployment(tcp_port=tcp))
        assert error.value.code is DriverErrorCode.HEALTHCHECK_FAILED
        assert not fake.containers
        status = await driver.inspect()
        assert (status.state, status.healthy) == (DriverState.FAILED, False)
        assert status.revision is None and status.instance_id is None

        fake.listen = True
        recovered = await driver.reset()
        assert (recovered.state, recovered.healthy, recovered.revision) == (
            DriverState.RUNNING,
            True,
            1,
        )

    run_scenario(scenario, backend=backend, driver_settings=settings(start_timeout=0.3))


def test_wrong_banner_fails_health_check() -> None:
    backend = InProcessBackend(tamper_banner="SSH-2.0-Impostor")

    async def scenario(fake: InProcessBackend, driver: LowDriver) -> None:
        with pytest.raises(DriverError) as error:
            await driver.start(deployment(tcp_port=free_port()))
        assert error.value.code is DriverErrorCode.HEALTHCHECK_FAILED
        assert "баннер" in error.value.message
        assert not fake.containers

    run_scenario(scenario, backend=backend, driver_settings=settings(start_timeout=0.3))


def test_failed_start_reports_start_failed_and_leaves_nothing() -> None:
    backend = InProcessBackend(run_error=BackendError("образ не найден"))

    async def scenario(fake: InProcessBackend, driver: LowDriver) -> None:
        with pytest.raises(DriverError) as error:
            await driver.start(deployment(tcp_port=free_port()))
        assert error.value.code is DriverErrorCode.START_FAILED
        assert not any(action in ("stop", "remove") for action, _ in fake.calls)
        assert not fake.containers
        assert (await driver.inspect()).state is DriverState.FAILED

    run_scenario(scenario, backend=backend)


def test_unavailable_backend_is_reported_as_such() -> None:
    backend = InProcessBackend(inspect_error=BackendError("docker недоступен", unavailable=True))

    async def scenario(fake: InProcessBackend, driver: LowDriver) -> None:
        with pytest.raises(DriverError) as error:
            await driver.start(deployment(tcp_port=free_port()))
        assert error.value.code is DriverErrorCode.BACKEND_UNAVAILABLE
        with pytest.raises(DriverError) as inspect_error:
            await driver.inspect()
        assert inspect_error.value.code is DriverErrorCode.BACKEND_UNAVAILABLE

    run_scenario(scenario, backend=backend)


def test_cleanup_failure_after_unhealthy_start_is_reported_as_cleanup_failed() -> None:
    backend = InProcessBackend(listen=False, remove_error=BackendError("занят"))

    async def scenario(fake: InProcessBackend, driver: LowDriver) -> None:
        with pytest.raises(DriverError) as error:
            await driver.start(deployment(tcp_port=free_port()))
        assert error.value.code is DriverErrorCode.CLEANUP_FAILED
        assert (await driver.inspect()).state is DriverState.FAILED

    run_scenario(scenario, backend=backend, driver_settings=settings(start_timeout=0.2))


def test_failed_stop_leaves_the_slot_failed_not_stopped() -> None:
    tcp = free_port()

    async def scenario(fake: InProcessBackend, driver: LowDriver) -> None:
        await driver.start(deployment(tcp_port=tcp))
        fake.remove_error = BackendError("занят")
        with pytest.raises(DriverError) as error:
            await driver.stop()
        assert error.value.code is DriverErrorCode.CLEANUP_FAILED
        assert (await driver.inspect()).state is DriverState.FAILED
        fake.remove_error = None
        assert (await driver.stop()).state is DriverState.STOPPED

    run_scenario(scenario)


def test_leftover_managed_container_is_removed_before_start() -> None:
    tcp = free_port()

    async def scenario(fake: InProcessBackend, driver: LowDriver) -> None:
        old = deployment(tcp_port=tcp)
        plan = translator().blueprint(old)
        await fake.run(translator().container_spec(plan, str(uuid4())))

        status = await driver.start(old)

        assert status.healthy and len(fake.containers) == 1
        assert fake.containers[NAME].labels[LABEL_INSTANCE] == status.instance_id

    run_scenario(scenario)


def test_foreign_container_with_the_slot_name_is_not_touched() -> None:
    async def scenario(fake: InProcessBackend, driver: LowDriver) -> None:
        fake.add_foreign(NAME)
        with pytest.raises(DriverError) as error:
            await driver.start(deployment(tcp_port=free_port()))
        assert error.value.code is DriverErrorCode.START_FAILED
        assert NAME in fake.containers
        assert ("remove", NAME) not in fake.calls

    run_scenario(scenario)


# --- inspect -----------------------------------------------------------------------------


def test_inspect_notices_a_crashed_container() -> None:
    tcp = free_port()

    async def scenario(fake: InProcessBackend, driver: LowDriver) -> None:
        await driver.start(deployment(tcp_port=tcp))
        await fake.crash(NAME, exit_code=137)

        status = await driver.inspect()

        assert (status.state, status.healthy) == (DriverState.FAILED, False)
        assert "137" in status.detail

    run_scenario(scenario)


def test_inspect_notices_a_vanished_container() -> None:
    tcp = free_port()

    async def scenario(fake: InProcessBackend, driver: LowDriver) -> None:
        await driver.start(deployment(tcp_port=tcp))
        await fake.remove(NAME)

        status = await driver.inspect()

        assert status.state is DriverState.FAILED and "исчез" in status.detail

    run_scenario(scenario)


def test_inspect_does_not_probe_the_trap() -> None:
    """Периодический inspect не должен порождать события Low (см. спецификацию, 4.4)."""
    tcp = free_port()

    async def scenario(fake: InProcessBackend, driver: LowDriver) -> None:
        await driver.start(deployment(tcp_port=tcp))
        events_before = fake.streams[NAME].getvalue()

        for _ in range(5):
            assert (await driver.inspect()).healthy

        assert fake.streams[NAME].getvalue() == events_before

    run_scenario(scenario)


def test_fresh_driver_adopts_a_running_container_without_claiming_health() -> None:
    tcp = free_port()

    async def scenario(fake: InProcessBackend, driver: LowDriver) -> None:
        started = await driver.start(deployment(revision=9, tcp_port=tcp))
        restarted_agent = LowDriver(DEPLOYMENT_ID, fake, settings())

        status = await restarted_agent.inspect()

        assert status.state is DriverState.RUNNING and status.healthy is False
        assert (status.instance_id, status.revision) == (started.instance_id, 9)
        with pytest.raises(DriverError) as error:
            await restarted_agent.reset()
        assert error.value.code is DriverErrorCode.INVALID_STATE
        adopted = await restarted_agent.apply_config(deployment(revision=10, tcp_port=tcp))
        assert adopted.healthy and adopted.instance_id != started.instance_id

    run_scenario(scenario)


def test_fresh_driver_without_container_is_stopped() -> None:
    async def scenario(fake: InProcessBackend, driver: LowDriver) -> None:
        assert await driver.inspect() == DriverStatus(DriverState.STOPPED)

    run_scenario(scenario)


def test_foreign_container_makes_inspect_report_failure() -> None:
    async def scenario(fake: InProcessBackend, driver: LowDriver) -> None:
        fake.add_foreign(NAME)

        status = await driver.inspect()

        assert status.state is DriverState.FAILED and "HoneyForge" in status.detail

    run_scenario(scenario)


@pytest.mark.parametrize("missing", ["instance_id", "revision", "container_id"])
def test_incomplete_managed_identity_is_failed_without_adoption(missing: str) -> None:
    """Managed и имя слота не позволяют подхватить контейнер с неполной identity."""

    async def scenario(fake: InProcessBackend, driver: LowDriver) -> None:
        spec = translator().container_spec(
            translator().blueprint(deployment(tcp_port=2200)), str(uuid4())
        )
        if missing != "container_id":
            spec = dataclasses.replace(
                spec,
                labels=tuple(
                    (key, value) for key, value in spec.labels if key != f"honeyforge.{missing}"
                ),
            )
        container = FakeContainer(spec, None)
        if missing == "container_id":
            container.container_id = ""
        fake.containers[NAME] = container

        status = await driver.inspect()

        assert status.state is DriverState.FAILED
        assert status.instance_id is None and status.revision is None and not status.healthy
        assert status.detail
        assert fake.calls == [("inspect", NAME)]
        assert fake.mutation_targets == []
        assert fake.containers[NAME] is container

    run_scenario(scenario)


def test_stop_ports_are_confirmed_released_without_backend_mutations() -> None:
    """Отсутствие контейнера дополняется реальным отказом connect к его двум портам."""
    tcp, http = free_port(), free_port()

    async def scenario(fake: InProcessBackend, driver: LowDriver) -> None:
        plan = deployment(tcp_port=tcp, http_port=http)
        await driver.start(plan)
        stopped = await driver.stop()
        calls_before = list(fake.calls)
        mutations_before = list(fake.mutation_targets)

        assert await driver.wait_until_released(plan)
        assert fake.calls == calls_before
        assert fake.mutation_targets == mutations_before
        assert await driver.inspect() == stopped

    run_scenario(scenario, driver_settings=settings(start_timeout=8.0))


# --- Параллелизм -------------------------------------------------------------------------


def test_concurrent_operations_are_serialised() -> None:
    tcp = free_port()
    backend = InProcessBackend(run_delay=0.05)

    async def scenario(fake: InProcessBackend, driver: LowDriver) -> None:
        await driver.start(deployment(tcp_port=tcp))

        results = await asyncio.gather(driver.reset(), driver.reset(), driver.inspect())

        assert fake.max_simultaneous == 1
        assert results[0].instance_id != results[1].instance_id
        assert results[2].state is DriverState.RUNNING and results[2].healthy
        assert len(fake.containers) == 1

    run_scenario(scenario, backend=backend)


def test_health_check_gives_up_by_the_deadline() -> None:
    check = LowHealthCheck(timeout=0.2, interval=0.02, probe_timeout=0.1)
    plan = translator().blueprint(deployment(tcp_port=free_port()))
    started = asyncio.new_event_loop()
    try:
        began = started.time()
        with pytest.raises(HealthCheckFailed) as failure:
            started.run_until_complete(check.wait_until_healthy(translator().health_targets(plan)))
        assert str(failure.value) != "проверка не выполнялась"
        assert started.time() - began < 1.5
    finally:
        started.close()


# --- Регрессии по итогам ревью PR2 ----------------------------------------------------------


def test_removal_is_always_preceded_by_a_graceful_stop() -> None:
    """Ловушка получает SIGTERM до удаления: иначе она не закроет соединения и журнал."""
    tcp = free_port()

    async def scenario(fake: InProcessBackend, driver: LowDriver) -> None:
        await driver.start(deployment(revision=1, tcp_port=tcp))
        await driver.reset()
        await driver.apply_config(deployment(revision=2, tcp_port=tcp))
        await driver.stop()

        actions = [action for action, _ in fake.calls if action in ("stop", "remove")]
        assert actions == ["stop", "remove"] * 3

    run_scenario(scenario)


@pytest.mark.parametrize("operation", ["stop", "apply_config"])
def test_foreign_container_with_the_slot_name_survives_stop_and_apply(operation: str) -> None:
    """Регрессия: stop и apply_config удаляли чужой контейнер, который start не трогал."""
    tcp = free_port()

    async def scenario(fake: InProcessBackend, driver: LowDriver) -> None:
        await driver.start(deployment(tcp_port=tcp))
        await fake.remove(NAME)
        fake.add_foreign(NAME)
        before = len(fake.calls)

        with pytest.raises(DriverError) as error:
            if operation == "stop":
                await driver.stop()
            else:
                await driver.apply_config(deployment(revision=2, tcp_port=tcp))

        assert error.value.code is DriverErrorCode.CLEANUP_FAILED
        assert NAME in fake.containers and fake.containers[NAME].labels == {"x": "y"}
        assert [action for action, _ in fake.calls[before:]] == ["inspect"]
        assert (await driver.inspect()).state is DriverState.FAILED

    run_scenario(scenario)


def test_cancelled_apply_config_never_claims_the_removed_instance_is_healthy() -> None:
    """Регрессия: отмена во время проверки здоровья оставляла RUNNING healthy старого экземпляра."""
    tcp = free_port()

    async def scenario(fake: InProcessBackend, driver: LowDriver) -> None:
        first = await driver.start(deployment(revision=1, tcp_port=tcp))
        fake.listen = False  # новый экземпляр не ответит на пробы
        task = asyncio.create_task(driver.apply_config(deployment(revision=2, tcp_port=tcp)))
        await asyncio.sleep(0.15)  # старый контейнер уже удалён, идёт проверка здоровья
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        status = await driver.inspect()  # ждёт завершения начатой операции под замком

        assert not (status.healthy and status.instance_id == first.instance_id)
        assert (status.state, status.healthy) == (DriverState.FAILED, False)
        assert not fake.containers, "неудачный экземпляр убран"

    run_scenario(scenario, driver_settings=settings(start_timeout=0.6))


def test_cancelled_start_completes_before_the_next_operation() -> None:
    """Регрессия: отмена отпускала замок, пока docker run ещё шёл, и stop опережал запуск."""
    backend = InProcessBackend(run_delay=0.3)

    async def scenario(fake: InProcessBackend, driver: LowDriver) -> None:
        task = asyncio.create_task(driver.start(deployment(tcp_port=free_port())))
        await asyncio.sleep(0.05)  # docker run уже выполняется
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        stopped = await driver.stop()

        assert stopped.state is DriverState.STOPPED
        assert not fake.containers, "stop дождался запуска и убрал его контейнер"
        assert fake.max_simultaneous == 1

    run_scenario(scenario, backend=backend)


def test_exited_leftover_after_host_reboot_keeps_a_fresh_slot_stopped() -> None:
    """После перезагрузки хоста (restart=no) свежий драйвер видит остановленный контейнер."""
    tcp = free_port()

    async def scenario(fake: InProcessBackend, driver: LowDriver) -> None:
        plan = translator().blueprint(deployment(tcp_port=tcp))
        await fake.run(translator().container_spec(plan, str(uuid4())))
        await fake.crash(NAME, exit_code=0)

        status = await driver.inspect()
        assert status.state is DriverState.STOPPED and "остановленный" in status.detail

        started = await driver.start(deployment(tcp_port=tcp))
        assert started.healthy and len(fake.containers) == 1

    run_scenario(scenario)


def test_cleanup_failure_keeps_the_original_cause() -> None:
    backend = InProcessBackend(
        run_error_after_creation=BackendError("созданный контейнер не запустился"),
        remove_error=BackendError("занят"),
    )

    async def scenario(fake: InProcessBackend, driver: LowDriver) -> None:
        with pytest.raises(DriverError) as error:
            await driver.start(deployment(tcp_port=free_port()))

        assert error.value.code is DriverErrorCode.CLEANUP_FAILED
        assert "созданный контейнер не запустился" in error.value.message
        assert ("stop", NAME) in fake.calls and ("remove", NAME) in fake.calls

    run_scenario(scenario, backend=backend)


def test_partial_start_failure_cleans_up_the_managed_container() -> None:
    backend = InProcessBackend(
        run_error_after_creation=BackendError("созданный контейнер не запустился")
    )

    async def scenario(fake: InProcessBackend, driver: LowDriver) -> None:
        with pytest.raises(DriverError) as error:
            await driver.start(deployment(tcp_port=free_port()))

        assert error.value.code is DriverErrorCode.START_FAILED
        assert "созданный контейнер не запустился" in error.value.message
        assert ("stop", NAME) in fake.calls and ("remove", NAME) in fake.calls
        assert not fake.containers
        assert (await driver.inspect()).state is DriverState.FAILED

    run_scenario(scenario, backend=backend)


@pytest.mark.parametrize("failure_phase", ["run", "healthcheck"])
@pytest.mark.parametrize("replacement_kind", ["foreign", "managed-other-instance"])
def test_foreign_container_appearing_after_initial_inspect_survives_failure_cleanup(
    failure_phase: str, replacement_kind: str
) -> None:
    """Сбой запуска не даёт права удалять контейнер, занявший имя после первой проверки."""

    class ConcurrentReplacementBackend(InProcessBackend):
        def occupy_slot(self, spec: ContainerSpec) -> None:
            if replacement_kind == "foreign":
                self.add_foreign(spec.name)
            else:
                labels = {**dict(spec.labels), LABEL_INSTANCE: str(uuid4())}
                replacement = dataclasses.replace(spec, labels=tuple(labels.items()))
                self.containers[spec.name] = FakeContainer(replacement, None)

        async def run(self, spec: ContainerSpec) -> None:
            if failure_phase == "run":
                self.calls.append(("run", spec.name))
                self.occupy_slot(spec)
                raise BackendError("имя контейнера уже занято")
            await super().run(spec)
            # Подмена внешнего участника: наш процесс исчез, имя занял чужой контейнер.
            await self.crash(spec.name)
            self.containers.pop(spec.name)
            self.occupy_slot(spec)

    class FailedHealthCheck(LowHealthCheck):
        async def wait_until_healthy(self, targets: low_module.HealthTargets) -> None:
            raise HealthCheckFailed("ответ не соответствует профилю")

    async def scenario(fake: InProcessBackend, driver: LowDriver) -> None:
        with pytest.raises(DriverError) as error:
            await driver.start(deployment(tcp_port=free_port()))

        cause = (
            "имя контейнера уже занято"
            if failure_phase == "run"
            else "ответ не соответствует профилю"
        )
        assert error.value.code is DriverErrorCode.CLEANUP_FAILED
        if replacement_kind == "foreign":
            assert "не принадлежащим HoneyForge" in error.value.message
            assert fake.containers[NAME].labels == {"x": "y"}
        else:
            assert "другим экземпляром HoneyForge" in error.value.message
            assert fake.containers[NAME].labels[LABEL_MANAGED] == "true"
        assert f"исходная причина: {cause}" in error.value.message
        assert not any(action in ("stop", "remove") for action, _ in fake.calls)
        status = await driver.inspect()
        assert (status.state, status.healthy) == (DriverState.FAILED, False)
        assert status.instance_id is None
        assert status.detail == error.value.message
        assert await driver.inspect() == status

    run_scenario(
        scenario,
        backend=ConcurrentReplacementBackend(),
        health=FailedHealthCheck(timeout=0.2, interval=0.02, probe_timeout=0.1),
    )


def test_cleanup_inspection_failure_preserves_unverified_resources_and_the_original_cause() -> None:
    """Недоступный inspect не подтверждает владение и не разрешает удаление ресурса."""

    class UnavailableCleanupBackend(InProcessBackend):
        async def run(self, spec: ContainerSpec) -> None:
            await super().run(spec)
            self.inspect_error = BackendError("сбой проверки владельца", unavailable=True)
            raise BackendError("сбой после создания контейнера")

    async def scenario(fake: InProcessBackend, driver: LowDriver) -> None:
        with pytest.raises(DriverError) as error:
            await driver.start(deployment(tcp_port=free_port()))

        assert error.value.code is DriverErrorCode.CLEANUP_FAILED
        assert "сбой проверки владельца" in error.value.message
        assert "исходная причина: сбой после создания контейнера" in error.value.message
        assert NAME in fake.containers
        assert not any(action in ("stop", "remove") for action, _ in fake.calls)
        fake.inspect_error = None
        status = await driver.inspect()
        assert (status.state, status.healthy) == (DriverState.FAILED, False)
        assert status.detail == error.value.message

    run_scenario(scenario, backend=UnavailableCleanupBackend())


@pytest.mark.parametrize("replacement_at", ["after_inspect", "between_stop_and_remove"])
@pytest.mark.parametrize("replacement_kind", ["foreign", "managed-other-instance"])
@pytest.mark.parametrize("original_resource", ["renamed", "gone"])
def test_container_name_replacement_cannot_redirect_stop_or_remove(
    replacement_at: str, replacement_kind: str, original_resource: str
) -> None:
    """Снимок неизменяемого ID защищает обе операции даже при замене имени после проверки."""

    class ReassigningBackend(InProcessBackend):
        armed = False
        original: FakeContainer | None = None
        replacement: FakeContainer | None = None

        async def reassign_name(self) -> None:
            self.armed = False
            self.original = self.containers.pop(NAME)
            if original_resource == "renamed":
                self.containers[f"{NAME}-retired"] = self.original
            else:
                if self.original.service is not None:
                    await self.original.service.stop()
                if self.original.sink is not None:
                    self.original.sink._abandon()
                self.original.running = False
            if replacement_kind == "foreign":
                self.add_foreign(NAME)
            else:
                labels = {**self.original.labels, LABEL_INSTANCE: str(uuid4())}
                spec = dataclasses.replace(self.original.spec, labels=tuple(labels.items()))
                self.containers[NAME] = FakeContainer(spec, None)
            self.replacement = self.containers[NAME]

        async def inspect(self, name: str) -> ContainerObservation | None:
            observed = await super().inspect(name)
            if self.armed and observed is not None and replacement_at == "after_inspect":
                await self.reassign_name()
            return observed

        async def stop(self, target: str | ContainerObservation, timeout: int) -> None:
            await super().stop(target, timeout)
            if self.armed and replacement_at == "between_stop_and_remove":
                await self.reassign_name()

    backend = ReassigningBackend()
    tcp = free_port()

    async def scenario(fake: InProcessBackend, driver: LowDriver) -> None:
        started = await driver.start(deployment(tcp_port=tcp))
        original_id = fake.containers[NAME].container_id
        backend.armed = True

        assert (await driver.stop()).state is DriverState.STOPPED

        assert backend.original is not None and backend.replacement is not None
        assert fake.containers == {NAME: backend.replacement}
        assert backend.replacement.running and not backend.original.running
        assert backend.replacement.container_id != original_id
        if replacement_kind == "managed-other-instance":
            assert backend.replacement.labels[LABEL_INSTANCE] != started.instance_id
        assert [action for action, _ in fake.mutation_targets] == ["stop", "remove"]
        for _, target in fake.mutation_targets:
            assert isinstance(target, ContainerObservation)
            assert target.container_id == original_id
            assert target.labels[LABEL_INSTANCE] == started.instance_id
        assert await is_closed(tcp)

    run_scenario(scenario, backend=backend)


@pytest.mark.parametrize("container_id", [None, "", "a" * 63, "A" * 64])
def test_driver_refuses_destructive_calls_without_a_valid_immutable_container_id(
    container_id: str | None,
) -> None:
    """Проверка драйвера обязательна и для backend, не проверяющего собственные аргументы."""

    class InvalidIdentityBackend(InProcessBackend):
        async def inspect(self, name: str) -> ContainerObservation | None:
            observed = await super().inspect(name)
            if observed is None:
                return None
            return dataclasses.replace(observed, container_id=container_id)

    async def scenario(fake: InProcessBackend, driver: LowDriver) -> None:
        await driver.start(deployment(tcp_port=free_port()))

        with pytest.raises(DriverError) as error:
            await driver.stop()

        assert error.value.code is DriverErrorCode.CLEANUP_FAILED
        assert "идентификатор контейнера" in error.value.message
        assert not fake.mutation_targets
        assert fake.containers[NAME].running
        assert (await driver.inspect()).state is DriverState.FAILED

    run_scenario(scenario, backend=InvalidIdentityBackend())


def test_failed_graceful_stop_still_removes_the_container() -> None:
    tcp = free_port()

    async def scenario(fake: InProcessBackend, driver: LowDriver) -> None:
        await driver.start(deployment(tcp_port=tcp))
        fake.stop_error = BackendError("docker stop: истёк таймаут", unavailable=True)

        status = await driver.stop()

        assert status.state is DriverState.STOPPED and not fake.containers

    run_scenario(scenario)


# --- Файловый журнал для сборщика --------------------------------------------------------


def journal_files(root: Path, deployment_id: str = DEPLOYMENT_ID) -> list[Path]:
    return sorted((root / deployment_id).glob("low-*.jsonl"))


def journal_records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="ascii").splitlines()]


def test_blueprint_and_spec_carry_journal_arguments_and_the_mount(tmp_path: Path) -> None:
    translator_with_journal = LowProfileTranslator(
        LowSettings(journal_root=tmp_path, journal_max_bytes=300000, journal_quota_bytes=900000)
    )
    plan = translator_with_journal.blueprint(deployment(revision=6, tcp_port=2200))
    instance = str(uuid4())

    container = translator_with_journal.container_spec(plan, instance)

    assert "--journal-dir=/journal" in container.command
    assert f"--instance-id={instance}" in container.command
    assert f"--deployment-id={DEPLOYMENT_ID}" in container.command
    assert "--profile-revision=6" in container.command
    assert "--journal-max-bytes=300000" in container.command
    assert "--journal-quota-bytes=900000" in container.command
    [mount] = container.mounts
    assert (mount.source, mount.target) == (str(tmp_path / DEPLOYMENT_ID), "/journal")


def test_without_journal_root_there_is_no_mount_and_no_journal_arguments() -> None:
    plan = translator().blueprint(deployment(tcp_port=2200))

    container = translator().container_spec(plan, str(uuid4()))

    assert container.mounts == ()
    assert not any(part.startswith("--journal") for part in container.command)


def test_started_trap_writes_a_collectable_journal_on_the_host(tmp_path: Path) -> None:
    tcp, http = free_port(), free_port()

    async def scenario(backend: InProcessBackend, driver: LowDriver) -> None:
        status = await driver.start(deployment(revision=7, tcp_port=tcp, http_port=http))
        assert (tmp_path / DEPLOYMENT_ID).is_dir()
        response = await fetch(http, b"GET /probe HTTP/1.1\r\nHost: x\r\n\r\n")
        assert response.startswith(b"HTTP/1.1 200")
        await fetch(tcp)
        [path] = journal_files(tmp_path)
        # Событие request TCP появляется, когда сервер заметит закрытие соединения клиентом.
        async with asyncio.timeout(3):
            while len(journal_records(path)) < 1 + 2 * 4:
                await asyncio.sleep(0.02)

        header, *events = journal_records(path)
        assert header["record"] == "header"
        assert header["instance_id"] == status.instance_id
        assert header["deployment_id"] == DEPLOYMENT_ID
        assert header["profile_revision"] == 7
        # Проба здоровья при запуске тоже оставила события; запрос клиента идёт после них.
        http_events = [e for e in events if e["protocol"] == "http"]
        assert http_events[-1]["service"] == {"protocol": "http", "port": http}
        assert {e["service"]["port"] for e in events if e["protocol"] == "tcp"} == {tcp}
        assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
        connection_ids = {e["connection_id"] for e in events}
        assert len(connection_ids) * 2 == len(events), "у каждого соединения два события"

    run_scenario(scenario, driver_settings=settings(journal_root=tmp_path))


def test_every_new_instance_starts_its_own_journal_and_old_ones_are_closed(
    tmp_path: Path,
) -> None:
    tcp = free_port()

    async def scenario(backend: InProcessBackend, driver: LowDriver) -> None:
        first = await driver.start(deployment(revision=1, tcp_port=tcp))
        second = await driver.reset()
        third = await driver.apply_config(deployment(revision=2, tcp_port=tcp))

        paths = journal_files(tmp_path)
        by_instance = {journal_records(p)[0]["instance_id"]: p for p in paths}
        assert set(by_instance) == {first.instance_id, second.instance_id, third.instance_id}
        for old in (first, second):
            footer = journal_records(by_instance[old.instance_id])[-1]
            assert (footer["record"], footer["reason"]) == ("footer", "shutdown")
        assert journal_records(by_instance[third.instance_id])[0]["profile_revision"] == 2

    run_scenario(scenario, driver_settings=settings(journal_root=tmp_path))


def test_stop_closes_the_journal_with_a_footer(tmp_path: Path) -> None:
    tcp = free_port()

    async def scenario(backend: InProcessBackend, driver: LowDriver) -> None:
        await driver.start(deployment(tcp_port=tcp))
        await driver.stop()

        [path] = journal_files(tmp_path)
        assert journal_records(path)[-1]["record"] == "footer"

    run_scenario(scenario, driver_settings=settings(journal_root=tmp_path))


def test_unusable_journal_directory_fails_the_start_before_running_anything(
    tmp_path: Path,
) -> None:
    blocker = tmp_path / "occupied"
    blocker.write_text("файл, а не каталог")

    async def scenario(backend: InProcessBackend, driver: LowDriver) -> None:
        with pytest.raises(DriverError) as error:
            await driver.start(deployment(tcp_port=free_port()))
        assert error.value.code is DriverErrorCode.START_FAILED
        assert "журнала" in error.value.message
        assert not backend.calls, "отказ на шаге подготовки, до обращения к Docker"
        assert (await driver.inspect()).state is DriverState.STOPPED

    run_scenario(scenario, driver_settings=settings(journal_root=blocker / "journal"))


def test_journal_limits_do_not_drift_from_the_low_defaults() -> None:
    assert MIN_JOURNAL_FILE_BYTES == MIN_FILE_BYTES == 256 * 1024
    assert DEFAULT_JOURNAL_FILE_BYTES == DEFAULT_FILE_BYTES == 8 * 1024 * 1024
    assert DEFAULT_JOURNAL_QUOTA_BYTES == DEFAULT_QUOTA_BYTES == 256 * 1024 * 1024


@pytest.mark.parametrize(
    ("file_bytes", "quota_bytes"),
    [
        (None, None),
        (MIN_FILE_BYTES, MIN_FILE_BYTES),
        (None, DEFAULT_FILE_BYTES),
        (DEFAULT_QUOTA_BYTES, None),
    ],
)
def test_effective_journal_limits_accept_the_specified_boundaries(
    file_bytes: int | None, quota_bytes: int | None
) -> None:
    configured = settings(journal_max_bytes=file_bytes, journal_quota_bytes=quota_bytes)

    assert configured.journal_max_bytes == file_bytes
    assert configured.journal_quota_bytes == quota_bytes


@pytest.mark.parametrize(
    "name",
    ["docker.sock", "comma,path", 'quote"path', "quote'path", "control\npath", "zero\u200bpath"],
)
def test_journal_root_rejects_unsafe_bind_mount_sources(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError):
        settings(journal_root=tmp_path / name)


def test_journal_root_rejects_the_filesystem_root_and_parent_traversal(tmp_path: Path) -> None:
    for root in (Path(tmp_path.anchor), tmp_path / ".." / "journal"):
        with pytest.raises(ValueError):
            settings(journal_root=root)


def test_journal_root_rejects_absolute_paths_from_a_different_operating_system() -> None:
    candidates = ("C:\\HoneyForge\\journal", "/srv/honeyforge/journal")
    foreign_absolute = next(path for path in candidates if not Path(path).is_absolute())

    with pytest.raises(ValueError, match="текущей ОС"):
        settings(journal_root=foreign_absolute)


def test_journal_root_accepts_spaces_equals_and_non_ascii(tmp_path: Path) -> None:
    root = tmp_path / "журнал с пробелом=low"

    assert settings(journal_root=root).journal_root == root


def override_directory_metadata(
    monkeypatch: pytest.MonkeyPatch,
    directory: Path,
    *,
    uid: int = 1000,
    gid: int = 1000,
    mode: int = stat.S_IFDIR | 0o750,
) -> None:
    """Подменяет lstat слота: проверки владельца и ссылок одинаковы на Windows и Linux."""
    original = Path.lstat
    metadata = os.stat_result((mode, 0, 0, 1, uid, gid, 0, 0, 0, 0))

    def lstat(path: Path) -> os.stat_result:
        return metadata if path == directory else original(path)

    monkeypatch.setattr(Path, "lstat", lstat)


def test_journal_directory_is_created_for_the_container_user_before_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, container_chown: Mock
) -> None:
    directory = tmp_path / DEPLOYMENT_ID
    override_directory_metadata(monkeypatch, directory)
    mkdir_calls: list[tuple[Path, int, bool, bool]] = []
    original_mkdir = Path.mkdir

    def mkdir(path: Path, mode: int = 0o777, parents: bool = False, exist_ok: bool = False) -> None:
        mkdir_calls.append((path, mode, parents, exist_ok))
        original_mkdir(path, mode=mode, parents=parents, exist_ok=exist_ok)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    original_spec = LowProfileTranslator.container_spec

    def container_spec(
        translator: LowProfileTranslator, blueprint: low_module.LowBlueprint, instance_id: str
    ) -> ContainerSpec:
        return dataclasses.replace(
            original_spec(translator, blueprint, instance_id), user="123:456"
        )

    monkeypatch.setattr(LowProfileTranslator, "container_spec", container_spec)

    async def scenario(backend: InProcessBackend, driver: LowDriver) -> None:
        def chown(path: Path, uid: int, gid: int, *, follow_symlinks: bool) -> None:
            assert directory.is_dir() and not backend.calls
            assert (path, uid, gid, follow_symlinks) == (directory, 123, 456, False)

        container_chown.side_effect = chown

        assert (await driver.start(deployment(tcp_port=free_port()))).healthy

        assert (directory, 0o750, True, True) in mkdir_calls
        container_chown.assert_called_once_with(directory, 123, 456, follow_symlinks=False)

    run_scenario(scenario, driver_settings=settings(journal_root=tmp_path))


def test_matching_journal_owner_is_not_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, container_chown: Mock
) -> None:
    override_directory_metadata(monkeypatch, tmp_path / DEPLOYMENT_ID, uid=65532, gid=65532)

    async def scenario(backend: InProcessBackend, driver: LowDriver) -> None:
        assert (await driver.start(deployment(tcp_port=free_port()))).healthy
        container_chown.assert_not_called()

    run_scenario(scenario, driver_settings=settings(journal_root=tmp_path))


@pytest.mark.parametrize("mode", [0o500, 0o300, 0o600])
@pytest.mark.parametrize("operation", ["start", "apply_config"])
def test_journal_directory_requires_all_owner_access_bits_before_touching_the_container(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    container_chown: Mock,
    mode: int,
    operation: str,
) -> None:
    directory = tmp_path / DEPLOYMENT_ID
    directory.mkdir()
    override_directory_metadata(monkeypatch, directory, uid=65532, gid=65532)
    tcp = free_port()

    async def scenario(backend: InProcessBackend, driver: LowDriver) -> None:
        first = None
        if operation == "apply_config":
            first = await driver.start(deployment(revision=1, tcp_port=tcp))
        before_calls = list(backend.calls)
        override_directory_metadata(
            monkeypatch, directory, uid=65532, gid=65532, mode=stat.S_IFDIR | mode
        )

        with pytest.raises(DriverError) as error:
            if operation == "start":
                await driver.start(deployment(revision=2, tcp_port=tcp))
            else:
                await driver.apply_config(deployment(revision=2, tcp_port=tcp))

        assert error.value.code is DriverErrorCode.START_FAILED
        assert backend.calls == before_calls
        container_chown.assert_not_called()
        if first is not None:
            assert await driver.inspect() == first
            assert await fetch(tcp) == b"SSH-2.0-OpenSSH_9.6\r\n"
        else:
            assert (await driver.inspect()).state is DriverState.STOPPED

    run_scenario(scenario, driver_settings=settings(journal_root=tmp_path))


def test_platform_without_chown_still_prepares_the_journal_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delattr(low_module.os, "chown")

    async def scenario(backend: InProcessBackend, driver: LowDriver) -> None:
        assert (await driver.start(deployment(tcp_port=free_port()))).healthy
        assert (tmp_path / DEPLOYMENT_ID).is_dir()

    run_scenario(scenario, driver_settings=settings(journal_root=tmp_path))


def test_chown_failure_rejects_start_before_any_docker_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, container_chown: Mock
) -> None:
    override_directory_metadata(monkeypatch, tmp_path / DEPLOYMENT_ID)
    container_chown.side_effect = PermissionError("нет CAP_CHOWN")

    async def scenario(backend: InProcessBackend, driver: LowDriver) -> None:
        with pytest.raises(DriverError) as failure:
            await driver.start(deployment(tcp_port=free_port()))

        assert failure.value.code is DriverErrorCode.START_FAILED
        assert not backend.calls
        assert (await driver.inspect()).state is DriverState.STOPPED

    run_scenario(scenario, driver_settings=settings(journal_root=tmp_path))


@pytest.mark.parametrize("mode", [stat.S_IFLNK | 0o750, stat.S_IFREG | 0o640])
def test_journal_slot_must_be_a_directory_and_never_a_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, container_chown: Mock, mode: int
) -> None:
    directory = tmp_path / DEPLOYMENT_ID
    override_directory_metadata(monkeypatch, directory, mode=mode)

    async def scenario(backend: InProcessBackend, driver: LowDriver) -> None:
        with pytest.raises(DriverError) as failure:
            await driver.start(deployment(tcp_port=free_port()))

        assert failure.value.code is DriverErrorCode.START_FAILED
        assert not backend.calls
        container_chown.assert_not_called()

    run_scenario(scenario, driver_settings=settings(journal_root=tmp_path))


@pytest.mark.parametrize("failure", ["chown", "symlink"])
def test_failed_journal_preparation_during_apply_keeps_the_previous_healthy_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, container_chown: Mock, failure: str
) -> None:
    directory = tmp_path / DEPLOYMENT_ID
    override_directory_metadata(monkeypatch, directory)
    tcp = free_port()

    async def scenario(backend: InProcessBackend, driver: LowDriver) -> None:
        first = await driver.start(deployment(revision=1, tcp_port=tcp))
        before_calls = list(backend.calls)
        if failure == "chown":
            container_chown.side_effect = PermissionError("нет CAP_CHOWN")
        else:
            override_directory_metadata(monkeypatch, directory, mode=stat.S_IFLNK | 0o750)

        with pytest.raises(DriverError) as error:
            await driver.apply_config(deployment(revision=2, tcp_port=tcp))

        assert error.value.code is DriverErrorCode.START_FAILED
        assert backend.calls == before_calls
        assert await driver.inspect() == first
        assert await fetch(tcp) == b"SSH-2.0-OpenSSH_9.6\r\n"

    run_scenario(scenario, driver_settings=settings(journal_root=tmp_path))


@pytest.mark.parametrize("failure", ["crash", "forced_remove", "failed_graceful_stop"])
def test_abrupt_termination_never_adds_a_shutdown_footer(tmp_path: Path, failure: str) -> None:
    tcp = free_port()

    async def scenario(backend: InProcessBackend, driver: LowDriver) -> None:
        await driver.start(deployment(tcp_port=tcp))
        [path] = journal_files(tmp_path)
        sink = backend.containers[NAME].sink
        assert sink is not None and sink.current_journal_id is not None

        if failure == "crash":
            await backend.crash(NAME)
            assert (await driver.inspect()).state is DriverState.FAILED
            before_cleanup = path.read_bytes()
            await driver.stop()
            assert path.read_bytes() == before_cleanup
        elif failure == "forced_remove":
            await backend.remove(NAME)
        else:
            backend.stop_error = BackendError("мягкая остановка не завершилась")
            assert (await driver.stop()).state is DriverState.STOPPED

        records = journal_records(path)
        assert records[0]["record"] == "header"
        assert any(record["record"] == "event" for record in records)
        assert not any(record["record"] == "footer" for record in records)
        assert sink.current_journal_id is None
        assert await is_closed(tcp)

    run_scenario(scenario, driver_settings=settings(journal_root=tmp_path))


def test_removing_an_already_stopped_container_does_not_change_its_closed_journal(
    tmp_path: Path,
) -> None:
    async def scenario(backend: InProcessBackend, driver: LowDriver) -> None:
        await driver.start(deployment(tcp_port=free_port()))
        await backend.stop(NAME, timeout=10)
        [path] = journal_files(tmp_path)
        records = journal_records(path)
        assert records[-1]["record"] == "footer"
        assert records[-1]["reason"] == "shutdown"
        assert sum(record["record"] == "footer" for record in records) == 1
        closed = path.read_bytes()

        await backend.remove(NAME)

        assert path.read_bytes() == closed

    run_scenario(scenario, driver_settings=settings(journal_root=tmp_path))
