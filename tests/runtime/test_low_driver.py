"""LowDriver на подмене Docker, которая запускает настоящий LowService по тем же аргументам."""

from __future__ import annotations

import asyncio
import dataclasses
import io
import json
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
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
from agent.runtime.drivers.base import DriverError, DriverErrorCode, DriverState, DriverStatus
from agent.runtime.drivers.containers import (
    BackendError,
    ContainerObservation,
    ContainerSpec,
)
from agent.runtime.drivers.low import (
    LABEL_INSTANCE,
    LowDriver,
    LowHealthCheck,
    LowProfileTranslator,
    LowSettings,
)
from honeypots.low import JsonLineEventSink, LowService
from honeypots.low.__main__ import build_parser, config_from_args, journal_from_args
from honeypots.low.journal import RotatingJournalSink

DEPLOYMENT_ID = "11111111-1111-4111-8111-111111111111"
OTHER_DEPLOYMENT_ID = "99999999-9999-4999-8999-999999999999"
NAME = f"honeyforge-low-{DEPLOYMENT_ID}"


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

    @property
    def labels(self) -> dict[str, str]:
        return dict(self.spec.labels)


@dataclass
class InProcessBackend:
    """Подмена Docker. Аргументы разбирает настоящий CLI Low, ловушкой служит LowService."""

    containers: dict[str, FakeContainer] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)
    streams: dict[str, io.StringIO] = field(default_factory=dict)
    max_simultaneous: int = 0
    run_error: BackendError | None = None
    remove_error: BackendError | None = None
    inspect_error: BackendError | None = None
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
        )

    async def remove(self, name: str) -> None:
        self.calls.append(("remove", name))
        if self.remove_error is not None:
            raise self.remove_error
        container = self.containers.pop(name, None)
        if container is not None and container.service is not None:
            await container.service.stop()
        if container is not None and container.sink is not None:
            container.sink.close()

    async def crash(self, name: str, exit_code: int = 137) -> None:
        container = self.containers[name]
        if container.service is not None:
            await container.service.stop()
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
) -> None:
    async def main() -> None:
        fake = backend if backend is not None else InProcessBackend()
        driver = LowDriver(DEPLOYMENT_ID, fake, driver_settings or settings())
        try:
            await scenario(fake, driver)
        finally:
            fake.run_error = fake.remove_error = fake.inspect_error = None
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
        {"journal_quota_bytes": True},
    ],
)
def test_invalid_settings_are_rejected(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        LowSettings(**changes)


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
        assert ("remove", NAME) in fake.calls
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
        with pytest.raises(Exception, match="."):
            started.run_until_complete(check.wait_until_healthy(translator().health_targets(plan)))
        assert started.time() - began < 2.0
    finally:
        started.close()


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
        assert ("run", NAME) not in backend.calls
        assert (await driver.inspect()).state is DriverState.FAILED

    run_scenario(scenario, driver_settings=settings(journal_root=blocker / "journal"))
