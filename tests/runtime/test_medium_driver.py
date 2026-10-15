"""MediumDriver на подмене Docker, которая проверяет раскладку как точка входа образа.

Настоящий Cowrie здесь не запускается: подмена разбирает аргументы контейнера,
требует ту же раскладку каталогов, что `honeypots/medium/entrypoint.sh`, читает
созданный драйвером `cowrie.cfg` настоящим `configparser` и отвечает на опубликованном
порту строкой идентификации SSH. Это проверяет перевод профиля, подготовку хоста и
пробу здоровья, но не поведение Cowrie и не настоящий Docker Engine.
"""

from __future__ import annotations

import asyncio
import configparser
import os
import stat
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
from test_low_driver import free_port

from agent.runtime.contract import (
    ChannelSpec,
    CredentialSpec,
    DeploymentSpec,
    LoggingSpec,
    ProfileSpec,
    Secret,
    ServiceSpec,
)
from agent.runtime.drivers import medium as medium_module
from agent.runtime.drivers.base import DriverError, DriverErrorCode, DriverState
from agent.runtime.drivers.containers import (
    LABEL_MANAGED,
    BackendError,
    ContainerObservation,
    ContainerSpec,
    DockerCli,
)
from agent.runtime.drivers.health import HealthCheckFailed
from agent.runtime.drivers.medium import (
    CONFIG_CONTAINER_DIR,
    MEDIUM_SSH_CONTAINER_PORT,
    STATE_CONTAINER_DIR,
    STATE_SUBDIRECTORIES,
    TMP_CONTAINER_DIR,
    CowrieConfiguration,
    MediumDriver,
    MediumHealthCheck,
    MediumProfileTranslator,
    MediumSettings,
)

DEPLOYMENT_ID = "22222222-2222-4222-8222-222222222222"
OTHER_DEPLOYMENT_ID = "88888888-8888-4888-8888-888888888888"
NAME = f"honeyforge-medium-{DEPLOYMENT_ID}"
BANNER = "SSH-2.0-OpenSSH_9.6"
PASSWORD = "only-in-userdb"
# Строка, которую подмена отправляет, когда профиль не задал версию.
UPSTREAM_BANNER = "SSH-2.0-OpenSSH_6.0p1 Debian-4+deb7u2"


@pytest.fixture(autouse=True)
def container_chown(monkeypatch: pytest.MonkeyPatch) -> Mock:
    """В подмене Docker процесс остаётся нашим: реальные владельцы файлов не меняются."""
    chown = Mock()
    monkeypatch.setattr(medium_module.os, "chown", chown, raising=False)
    monkeypatch.setattr(medium_module.os, "fchown", Mock(), raising=False)
    return chown


def profile(
    *,
    level: str = "medium",
    services: tuple[ServiceSpec, ...] | None = None,
    port: int | None = None,
    banner: str = BANNER,
    hostname: str = "edge",
    credentials: tuple[CredentialSpec, ...] | None = None,
) -> ProfileSpec:
    chosen = (
        services
        if services is not None
        else (ServiceSpec("ssh", port if port is not None else 2222, banner),)
    )
    return ProfileSpec(
        interaction_level=level,  # type: ignore[arg-type]
        hostname=hostname,
        services=chosen,
        logging=LoggingSpec(2048),
        channel=ChannelSpec(1500, 100),
        high=None,
        credentials=credentials
        if credentials is not None
        else (CredentialSpec("admin", Secret(PASSWORD)),),
    )


def deployment(
    *, revision: int = 1, deployment_id: str = DEPLOYMENT_ID, **changes: Any
) -> DeploymentSpec:
    return DeploymentSpec(deployment_id, str(uuid4()), revision, "planned", profile(**changes))


def settings(root: Path, **changes: Any) -> MediumSettings:
    values: dict[str, Any] = {
        "state_root": root,
        "start_timeout": 3.0,
        "probe_interval": 0.02,
        "probe_timeout": 1.0,
    }
    values.update(changes)
    return MediumSettings(**values)


def read_config(spec: ContainerSpec) -> configparser.ConfigParser:
    """Читает созданный драйвером cowrie.cfg тем же разбором, что Cowrie.

    Интерполяция оставлена по умолчанию: значение с «%» уронило бы чтение, поэтому
    тест заодно подтверждает смысл запрета «%» в баннере профиля.
    """
    source = next(m.source for m in spec.mounts if m.target == CONFIG_CONTAINER_DIR)
    parser = configparser.ConfigParser()
    parser.read(Path(source) / "cowrie.cfg", encoding="ascii")
    return parser


@dataclass
class FakeCowrie:
    """Контейнер подмены: сервер, который отвечает строкой идентификации SSH."""

    spec: ContainerSpec
    server: asyncio.AbstractServer | None
    running: bool = True
    exit_code: int | None = None
    container_id: str = field(default_factory=lambda: uuid4().hex + uuid4().hex)

    @property
    def labels(self) -> dict[str, str]:
        return dict(self.spec.labels)


@dataclass
class CowrieBackend:
    """Подмена Docker для Medium: проверяет раскладку и слушает опубликованный порт."""

    containers: dict[str, FakeCowrie] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)
    mutation_targets: list[tuple[str, str | ContainerObservation]] = field(default_factory=list)
    configs: list[configparser.ConfigParser] = field(default_factory=list)
    listen: bool = True
    silent: bool = False
    tamper_banner: str | None = None
    run_delay: float = 0.0
    run_error: BackendError | None = None
    inspect_error: BackendError | None = None
    stop_error: BackendError | None = None
    remove_error: BackendError | None = None
    # Docker ответил успехом, но контейнер остался либо порт остался занят чужим
    # процессом: исполнитель обязан обнаружить это после удаления.
    forget_remove: bool = False
    leak_port: bool = False
    leaked: list[asyncio.AbstractServer] = field(default_factory=list)

    async def run(self, spec: ContainerSpec) -> None:
        self.calls.append(("run", spec.name))
        if self.run_delay:
            await asyncio.sleep(self.run_delay)
        if self.run_error is not None:
            raise self.run_error
        if spec.name in self.containers:
            raise BackendError("имя контейнера уже занято")
        config = self._entrypoint(spec)
        self.configs.append(config)
        server: asyncio.AbstractServer | None = None
        if self.listen:
            banner = self.tamper_banner
            if banner is None:
                banner = config["ssh"].get("version", UPSTREAM_BANNER)
            publish = spec.publish[0]
            server = await asyncio.start_server(
                self._session(banner), publish.host_ip, publish.host_port
            )
        self.containers[spec.name] = FakeCowrie(spec, server)

    def _entrypoint(self, spec: ContainerSpec) -> configparser.ConfigParser:
        """Повторяет проверки honeypots/medium/entrypoint.sh до запуска ловушки."""
        expected = {
            f"--config-dir={CONFIG_CONTAINER_DIR}",
            f"--state-dir={STATE_CONTAINER_DIR}",
        }
        if set(spec.command) != expected:
            raise BackendError("неизвестный аргумент точки входа")
        mounts = {mount.target: mount for mount in spec.mounts}
        config_dir = Path(mounts[CONFIG_CONTAINER_DIR].source)
        state_dir = Path(mounts[STATE_CONTAINER_DIR].source)
        for name in ("cowrie.cfg", "userdb.txt"):
            if not (config_dir / name).is_file():
                raise BackendError("каталог конфигурации неполный")
        for name in STATE_SUBDIRECTORIES:
            if not (state_dir / name).is_dir():
                raise BackendError("каталог состояния неполный")
        parser = configparser.ConfigParser()
        parser.read(config_dir / "cowrie.cfg", encoding="ascii")
        listen = parser["ssh"]["listen_endpoints"]
        if listen != f"tcp:{MEDIUM_SSH_CONTAINER_PORT}:interface=0.0.0.0":
            raise BackendError("ловушка слушает не тот порт контейнера")
        # Первый запуск экземпляра создаёт ключи хоста в записываемом состоянии.
        (state_dir / "keys" / "ssh_host_ed25519_key").touch()
        return parser

    def _session(
        self, banner: str
    ) -> Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]:
        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                if not self.silent:
                    writer.write(banner.encode("ascii") + b"\r\n")
                    await writer.drain()
                # Cowrie ждёт строку клиента; проба её не отправляет и закрывает связь.
                await reader.read()
            except (OSError, asyncio.CancelledError):
                pass
            finally:
                writer.close()

        return handle

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

    def _key(self, target: str | ContainerObservation) -> str | None:
        if isinstance(target, str):
            return target if target in self.containers else None
        return next(
            (
                key
                for key, item in self.containers.items()
                if item.container_id == target.container_id
            ),
            None,
        )

    async def stop(self, target: str | ContainerObservation, timeout: int) -> None:
        name = target if isinstance(target, str) else target.name
        self.calls.append(("stop", name))
        self.mutation_targets.append(("stop", target))
        if self.stop_error is not None:
            raise self.stop_error
        key = self._key(target)
        container = self.containers.get(key) if key is not None else None
        if container is not None and container.running:
            await self._shutdown(container)
            container.running = False
            container.exit_code = 0

    async def remove(self, target: str | ContainerObservation) -> None:
        name = target if isinstance(target, str) else target.name
        self.calls.append(("remove", name))
        self.mutation_targets.append(("remove", target))
        if self.remove_error is not None:
            raise self.remove_error
        key = self._key(target)
        if key is None:
            return
        if self.forget_remove:
            # Docker ответил успехом, но контейнер остался: исполнитель обязан
            # обнаружить это повторным наблюдением.
            return
        container = self.containers.pop(key)
        if container.running:
            await self._shutdown(container)

    async def _shutdown(self, container: FakeCowrie) -> None:
        if container.server is None:
            return
        if self.leak_port:
            # Контейнера больше нет, но порт продолжает принимать соединения.
            self.leaked.append(container.server)
            container.server = None
            return
        container.server.close()
        await container.server.wait_closed()
        container.server = None

    def add_foreign(self, name: str) -> None:
        spec = ContainerSpec(name=name, image="other:1", command=(), labels=(("x", "y"),))
        self.containers[name] = FakeCowrie(spec, None)

    async def close(self) -> None:
        self.stop_error = self.remove_error = self.inspect_error = None
        self.forget_remove = self.leak_port = False
        for name in list(self.containers):
            await self.remove(name)
        for server in self.leaked:
            server.close()
            await server.wait_closed()
        self.leaked.clear()


def run_scenario(
    scenario: Callable[[CowrieBackend, MediumDriver], Awaitable[None]],
    tmp_path: Path,
    *,
    backend: CowrieBackend | None = None,
    driver_settings: MediumSettings | None = None,
) -> None:
    async def main() -> None:
        fake = backend if backend is not None else CowrieBackend()
        driver = MediumDriver(DEPLOYMENT_ID, fake, driver_settings or settings(tmp_path / "state"))
        try:
            await scenario(fake, driver)
        finally:
            await fake.close()

    asyncio.run(main())


async def banner_of(host: str, port: int) -> bytes:
    reader, writer = await asyncio.open_connection(host, port)
    try:
        return await reader.readline()
    finally:
        writer.close()
        await writer.wait_closed()


def test_start_publishes_ssh_and_answers_with_profile_banner(tmp_path: Path) -> None:
    port = free_port()

    async def scenario(backend: CowrieBackend, driver: MediumDriver) -> None:
        status = await driver.start(deployment(port=port))
        assert status.state is DriverState.RUNNING and status.healthy
        assert status.instance_id is not None and status.revision == 1
        assert await banner_of("127.0.0.1", port) == BANNER.encode("ascii") + b"\r\n"
        assert [call[0] for call in backend.calls].count("run") == 1

    run_scenario(scenario, tmp_path)


def test_container_run_keeps_hardening_and_adds_only_declared_writable_paths(
    tmp_path: Path,
) -> None:
    port = free_port()

    async def scenario(backend: CowrieBackend, driver: MediumDriver) -> None:
        await driver.start(deployment(port=port))
        spec = backend.containers[NAME].spec
        argv = DockerCli().run_command(spec)
        for option in (
            "--read-only",
            "--init",
            "--detach",
        ):
            assert option in argv
        assert argv[argv.index("--cap-drop") + 1] == "ALL"
        assert argv[argv.index("--security-opt") + 1] == "no-new-privileges:true"
        assert argv[argv.index("--restart") + 1] == "no"
        assert argv[argv.index("--pull") + 1] == "never"
        assert argv[argv.index("--user") + 1] == "1000:1000"
        assert argv[argv.index("--memory") + 1] == "512m"
        assert argv[argv.index("--cpus") + 1] == "1"
        assert argv[argv.index("--pids-limit") + 1] == "256"
        assert argv[argv.index("--publish") + 1] == f"127.0.0.1:{port}:{MEDIUM_SSH_CONTAINER_PORT}"
        assert (
            argv[argv.index("--tmpfs") + 1]
            == f"{TMP_CONTAINER_DIR}:rw,noexec,nosuid,nodev,size=16m"
        )
        mounts = [argv[index + 1] for index, item in enumerate(argv) if item == "--mount"]
        assert sum(item.endswith(",readonly") for item in mounts) == 1
        assert sum(f"target={CONFIG_CONTAINER_DIR}," in item for item in mounts) == 1
        assert sum(item.endswith(f"target={STATE_CONTAINER_DIR}") for item in mounts) == 1
        # Команда идёт после образа, поэтому docker не разбирает её как свои опции.
        assert argv[argv.index(spec.image) + 1 :] == list(spec.command)
        assert dict(spec.labels)["honeyforge.level"] == "medium"

    run_scenario(scenario, tmp_path)


def test_generated_configuration_follows_profile_and_closes_extra_channels(
    tmp_path: Path,
) -> None:
    port = free_port()

    async def scenario(backend: CowrieBackend, driver: MediumDriver) -> None:
        await driver.start(deployment(port=port, hostname="gateway"))
        spec = backend.containers[NAME].spec
        config = read_config(spec)
        assert config["honeypot"]["hostname"] == "gateway"
        assert config["honeypot"]["backend"] == "shell"
        assert config["honeypot"]["auth_class"] == "UserDB"
        assert config["honeypot"]["log_path"] == f"{STATE_CONTAINER_DIR}/log"
        assert config["honeypot"]["ttylog_path"] == f"{STATE_CONTAINER_DIR}/lib/tty"
        assert config["ssh"]["version"] == BANNER
        assert config["ssh"]["ed25519_private_key"].startswith(f"{STATE_CONTAINER_DIR}/keys/")
        assert config["telnet"].getboolean("enabled") is False
        for option in ("forwarding", "forward_redirect", "forward_tunnel", "sftp_enabled"):
            assert config["ssh"].getboolean(option) is False
        assert config["output_jsonlog"].getboolean("enabled") is True
        assert config["output_jsonlog"]["logfile"] == f"{STATE_CONTAINER_DIR}/log/cowrie.json"
        source = next(m.source for m in spec.mounts if m.target == CONFIG_CONTAINER_DIR)
        userdb = (Path(source) / "userdb.txt").read_text(encoding="ascii")
        assert f"admin:x:{PASSWORD}" in userdb.splitlines()

    run_scenario(scenario, tmp_path)


def test_default_banner_leaves_upstream_version_and_probe_checks_protocol(
    tmp_path: Path,
) -> None:
    port = free_port()

    async def scenario(backend: CowrieBackend, driver: MediumDriver) -> None:
        status = await driver.start(deployment(port=port, banner=""))
        assert status.healthy
        assert "version" not in read_config(backend.containers[NAME].spec)["ssh"]
        assert await banner_of("127.0.0.1", port) == UPSTREAM_BANNER.encode("ascii") + b"\r\n"

    run_scenario(scenario, tmp_path)


def test_password_stays_in_userdb_and_out_of_docker_and_statuses(tmp_path: Path) -> None:
    port = free_port()

    async def scenario(backend: CowrieBackend, driver: MediumDriver) -> None:
        status = await driver.start(deployment(port=port))
        spec = backend.containers[NAME].spec
        argv = DockerCli().run_command(spec)
        assert all(PASSWORD not in item for item in argv)
        assert all(PASSWORD not in value for _, value in spec.labels)
        assert PASSWORD not in str(status) and PASSWORD not in repr(status)
        config_dir = Path(next(m.source for m in spec.mounts if m.target == CONFIG_CONTAINER_DIR))
        assert PASSWORD not in (config_dir / "cowrie.cfg").read_text(encoding="ascii")
        inspected = await driver.inspect()
        assert PASSWORD not in repr(inspected)

    run_scenario(scenario, tmp_path)


@pytest.mark.skipif(os.name == "nt", reason="POSIX права каталогов отсутствуют на Windows")
def test_userdb_and_state_get_closed_posix_permissions(tmp_path: Path) -> None:
    port = free_port()

    async def scenario(backend: CowrieBackend, driver: MediumDriver) -> None:
        await driver.start(deployment(port=port))
        spec = backend.containers[NAME].spec
        config_dir = Path(next(m.source for m in spec.mounts if m.target == CONFIG_CONTAINER_DIR))
        state_dir = Path(next(m.source for m in spec.mounts if m.target == STATE_CONTAINER_DIR))
        assert stat.S_IMODE((config_dir / "userdb.txt").stat().st_mode) == 0o600
        assert stat.S_IMODE((config_dir / "cowrie.cfg").stat().st_mode) == 0o640
        for directory in (config_dir, state_dir, state_dir / "lib" / "tty"):
            assert stat.S_IMODE(directory.stat().st_mode) == 0o750

    run_scenario(scenario, tmp_path)


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"level": "low"}, id="другой уровень"),
        pytest.param(
            {"services": (ServiceSpec("tcp", 2222, BANNER),)},
            id="не ssh",
        ),
        pytest.param(
            {
                "services": (
                    ServiceSpec("ssh", 2222, BANNER),
                    ServiceSpec("ssh", 2223, BANNER),
                )
            },
            id="два ssh",
        ),
        pytest.param({"banner": "OpenSSH_9.6"}, id="не строка идентификации"),
        pytest.param({"banner": "SSH-2.0-Open%SSH"}, id="процент в баннере"),
        pytest.param({"banner": "SSH-2.0-OpenSSH "}, id="пробел в конце баннера"),
        pytest.param({"banner": "SSH-2.0-" + "x" * 250}, id="слишком длинный баннер"),
        pytest.param({"banner": "SSH-2.0-Опен"}, id="баннер не ASCII"),
        pytest.param({"credentials": ()}, id="нет учётных записей"),
        pytest.param(
            {"credentials": (CredentialSpec("admin", Secret("pass:word")),)},
            id="двоеточие в пароле",
        ),
        pytest.param(
            {"credentials": (CredentialSpec("admin", Secret("*")),)},
            id="шаблон вместо пароля",
        ),
        pytest.param(
            {"credentials": (CredentialSpec("admin", Secret("!root")),)},
            id="отрицание в пароле",
        ),
        pytest.param(
            {"credentials": (CredentialSpec("admin", Secret("па роль")),)},
            id="пароль не ASCII",
        ),
    ],
)
def test_unsupported_profile_is_refused_without_touching_docker(
    tmp_path: Path, changes: dict[str, Any]
) -> None:
    async def scenario(backend: CowrieBackend, driver: MediumDriver) -> None:
        with pytest.raises(DriverError) as failure:
            await driver.start(deployment(**changes))
        assert failure.value.code is DriverErrorCode.UNSUPPORTED_PROFILE
        assert PASSWORD not in failure.value.message
        assert not backend.calls and not backend.containers

    run_scenario(scenario, tmp_path)


def test_refused_revision_does_not_kill_the_running_instance(tmp_path: Path) -> None:
    port = free_port()

    async def scenario(backend: CowrieBackend, driver: MediumDriver) -> None:
        first = await driver.start(deployment(port=port))
        with pytest.raises(DriverError) as failure:
            await driver.apply_config(deployment(revision=2, port=port, banner="telnet"))
        assert failure.value.code is DriverErrorCode.UNSUPPORTED_PROFILE
        status = await driver.inspect()
        assert status.state is DriverState.RUNNING and status.instance_id == first.instance_id
        assert await banner_of("127.0.0.1", port) == BANNER.encode("ascii") + b"\r\n"

    run_scenario(scenario, tmp_path)


def test_silent_container_fails_health_check_and_is_removed(tmp_path: Path) -> None:
    backend = CowrieBackend(silent=True)

    async def scenario(fake: CowrieBackend, driver: MediumDriver) -> None:
        with pytest.raises(DriverError) as failure:
            await driver.start(deployment(port=free_port()))
        assert failure.value.code is DriverErrorCode.HEALTHCHECK_FAILED
        assert not fake.containers
        assert (await driver.inspect()).state is DriverState.FAILED

    run_scenario(scenario, tmp_path, backend=backend)


def test_foreign_banner_is_not_accepted_as_the_profile_instance(tmp_path: Path) -> None:
    backend = CowrieBackend(tamper_banner="SSH-2.0-Другая")

    async def scenario(fake: CowrieBackend, driver: MediumDriver) -> None:
        with pytest.raises(DriverError) as failure:
            await driver.start(deployment(port=free_port()))
        assert failure.value.code is DriverErrorCode.HEALTHCHECK_FAILED
        assert "идентификации" in failure.value.message
        assert not fake.containers

    run_scenario(scenario, tmp_path, backend=backend)


def test_probe_rejects_a_port_that_answers_other_protocol(tmp_path: Path) -> None:
    """Проба не принимает чужой сервис за ловушку и ничего ему не отправляет."""

    async def scenario() -> None:
        received: list[bytes] = []

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            writer.write(b"220 smtp ready\r\n")
            await writer.drain()
            received.append(await reader.read(16))
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = int(server.sockets[0].getsockname()[1])
        check = MediumHealthCheck(0.3, 0.02, 0.2)
        targets = medium_module.MediumTargets("127.0.0.1", port, None)
        try:
            with pytest.raises(HealthCheckFailed):
                await check.wait_until_healthy(targets)
        finally:
            server.close()
            await server.wait_closed()
        assert all(not item for item in received)

    asyncio.run(scenario())


def test_stop_releases_the_port_and_repeats_safely(tmp_path: Path) -> None:
    port = free_port()
    plan = deployment(port=port)

    async def scenario(backend: CowrieBackend, driver: MediumDriver) -> None:
        started = await driver.start(plan)
        status = await driver.stop()
        assert status.state is DriverState.STOPPED and not status.healthy
        # instance_id последнего экземпляра сохраняется, revision очищается.
        assert status.instance_id == started.instance_id and status.revision is None
        assert not backend.containers
        assert await driver.wait_until_released(plan)
        assert (await driver.stop()).state is DriverState.STOPPED

    run_scenario(scenario, tmp_path)


def test_release_probe_covers_every_port_of_a_profile_refused_at_start(tmp_path: Path) -> None:
    first, second = free_port(), free_port()
    plan = deployment(
        services=(ServiceSpec("ssh", first, BANNER), ServiceSpec("ssh", second, BANNER))
    )

    def accept(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()

    async def scenario(backend: CowrieBackend, driver: MediumDriver) -> None:
        with pytest.raises(DriverError):
            await driver.start(plan)
        assert await driver.wait_until_released(plan)
        server = await asyncio.start_server(accept, "127.0.0.1", second)
        try:
            assert not await driver.wait_until_released(plan), "второй порт ещё слушается"
        finally:
            server.close()
            await server.wait_closed()
        assert await driver.wait_until_released(plan)

    run_scenario(scenario, tmp_path)


def test_release_probe_refuses_a_profile_of_another_level(tmp_path: Path) -> None:
    async def scenario(backend: CowrieBackend, driver: MediumDriver) -> None:
        with pytest.raises(DriverError) as failure:
            await driver.wait_until_released(deployment(level="low"))
        assert failure.value.code is DriverErrorCode.UNSUPPORTED_PROFILE

    run_scenario(scenario, tmp_path)


def test_other_deployment_is_refused_without_mutations(tmp_path: Path) -> None:
    async def scenario(backend: CowrieBackend, driver: MediumDriver) -> None:
        with pytest.raises(DriverError) as failure:
            await driver.start(deployment(deployment_id=OTHER_DEPLOYMENT_ID))
        assert failure.value.code is DriverErrorCode.INVALID_STATE
        assert not backend.calls

    run_scenario(scenario, tmp_path)


def test_reset_starts_a_new_instance_in_its_own_state_directory(tmp_path: Path) -> None:
    port = free_port()
    root = tmp_path / "state"

    async def scenario(backend: CowrieBackend, driver: MediumDriver) -> None:
        first = await driver.start(deployment(port=port))
        second = await driver.reset()
        assert second.instance_id != first.instance_id and second.healthy
        instances = sorted(p.name for p in (root / DEPLOYMENT_ID).iterdir())
        # Каталог прежнего экземпляра сохраняется: его журнал мог быть не прочитан.
        assert instances == sorted([str(UUID(first.instance_id or "")), str(second.instance_id)])
        assert await banner_of("127.0.0.1", port) == BANNER.encode("ascii") + b"\r\n"

    run_scenario(scenario, tmp_path, driver_settings=settings(root))


def test_unusable_state_root_fails_before_removing_the_running_instance(tmp_path: Path) -> None:
    port = free_port()
    root = tmp_path / "state"
    root.mkdir()

    async def scenario(backend: CowrieBackend, driver: MediumDriver) -> None:
        first = await driver.start(deployment(port=port))
        # Каталог назначения занят обычным файлом: подготовка следующей попытки
        # не может создать каталог экземпляра.
        instance = root / DEPLOYMENT_ID / str(first.instance_id)
        for item in sorted(instance.rglob("*"), reverse=True):
            item.unlink() if item.is_file() else item.rmdir()
        instance.rmdir()
        (root / DEPLOYMENT_ID).rmdir()
        (root / DEPLOYMENT_ID).write_text("busy", encoding="ascii")
        with pytest.raises(DriverError) as failure:
            await driver.apply_config(deployment(revision=2, port=port))
        assert failure.value.code is DriverErrorCode.START_FAILED
        assert str(root) not in failure.value.message
        assert backend.containers[NAME].running
        assert "remove" not in [call[0] for call in backend.calls]

    run_scenario(scenario, tmp_path, driver_settings=settings(root))


def test_foreign_container_in_the_slot_is_never_stopped(tmp_path: Path) -> None:
    async def scenario(backend: CowrieBackend, driver: MediumDriver) -> None:
        backend.add_foreign(NAME)
        with pytest.raises(DriverError) as failure:
            await driver.start(deployment(port=free_port()))
        assert failure.value.code is DriverErrorCode.START_FAILED
        assert NAME in backend.containers
        assert not backend.mutation_targets
        assert backend.containers[NAME].labels.get(LABEL_MANAGED) is None

    run_scenario(scenario, tmp_path)


def test_settings_reject_unusable_host_configuration(tmp_path: Path) -> None:
    for changes in (
        {"state_root": Path("relative")},
        {"image": "--privileged"},
        {"network": "host"},
        {"user": "0:0"},
        {"user": "00:5"},
        {"stop_timeout": 30},
        {"start_timeout": float("nan")},
        {"probe_timeout": float("inf")},
        {"probe_interval": 0},
    ):
        with pytest.raises(ValueError):
            settings(tmp_path / "state", **changes)


def test_configuration_text_is_ascii_and_deterministic(tmp_path: Path) -> None:
    """Конфигурация и userdb записываются как ASCII: иначе файл нельзя создать."""
    translator = MediumProfileTranslator(settings(tmp_path / "state"))
    blueprint = translator.blueprint(deployment(port=2222))
    configuration = CowrieConfiguration(blueprint)
    for text in (configuration.cowrie_cfg(), configuration.userdb()):
        text.encode("ascii")
        assert text.endswith("\n")
    assert configuration.cowrie_cfg() == CowrieConfiguration(blueprint).cowrie_cfg()
    assert repr(blueprint).count(PASSWORD) == 0
