"""Проверки описания контейнера и DockerCli на подмене исполнителя (docker не запускается)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import Any

import pytest

from agent.runtime.drivers.containers import (
    BackendError,
    BindMount,
    ContainerObservation,
    ContainerSpec,
    DockerCli,
    PortPublish,
)
from infra.process import CommandResult

NAME = "honeyforge-low-11111111-1111-4111-8111-111111111111"
CONTAINER_ID = "0123456789abcdef" * 4
REPLACEMENT_ID = "f" * 64


def spec(**changes: Any) -> ContainerSpec:
    values: dict[str, Any] = {
        "name": NAME,
        "image": "honeyforge-low:local",
        "command": ("--host=0.0.0.0", "--banner=-dash"),
        "publish": (PortPublish("127.0.0.1", 2222, 2222),),
        "labels": (("honeyforge.managed", "true"),),
    }
    values.update(changes)
    return ContainerSpec(**values)


class FakeRunner:
    """Возвращает заданные результаты и запоминает точные списки аргументов."""

    def __init__(self, *results: CommandResult) -> None:
        self.results = list(results) or [CommandResult(0, "")]
        self.commands: list[list[str]] = []

    def run(self, command: Sequence[str]) -> CommandResult:
        self.commands.append(list(command))
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


def cli(*results: CommandResult) -> tuple[DockerCli, FakeRunner]:
    runner = FakeRunner(*results)
    return DockerCli(runner), runner


def observation(**changes: Any) -> ContainerObservation:
    values: dict[str, Any] = {
        "name": NAME,
        "status": "running",
        "running": True,
        "exit_code": 0,
        "labels": {"honeyforge.managed": "true"},
        "container_id": CONTAINER_ID,
    }
    values.update(changes)
    return ContainerObservation(**values)


# --- ContainerSpec ------------------------------------------------------------------


def test_valid_spec_is_accepted() -> None:
    assert spec().name == NAME


@pytest.mark.parametrize(
    "changes",
    [
        {"name": "mysql"},
        {"name": "honeyforge-"},
        {"name": "honeyforge-UPPER"},
        {"name": "honeyforge-a b"},
        {"name": "honeyforge-a/b"},
        {"name": "honeyforge-" + "a" * 120},
        {"image": ""},
        {"image": "-evil"},
        {"image": "img name"},
        {"image": "img;rm -rf"},
        {"network": "-host"},
        {"network": "a b"},
        {"user": "root"},
        {"user": "0:0"},
        {"user": "0:5"},
        {"user": "5:0"},
        {"user": "00:5"},
        {"user": "5:000"},
        {"network": "host"},
        {"network": "NONE"},
        {"log_max_size": "10"},
        {"log_max_size": "0m"},
        {"log_max_files": 0},
        {"memory_mb": 8},
        {"memory_mb": 4097},
        {"memory_mb": True},
        {"cpus": 0},
        {"cpus": 9},
        {"cpus": float("nan")},
        {"cpus": True},
        {"pids_limit": 0},
        {"pids_limit": True},
        {"command": ("ok\nbad",)},
        {"command": ("bad\x00",)},
        {"command": ("a" * 1025,)},
        {"command": (5,)},
        {"labels": (("UPPER", "x"),)},
        {"labels": (("ok", "line\nbreak"),)},
        {"labels": (("ok", "v" * 257),)},
        {
            "publish": (
                PortPublish("127.0.0.1", 2222, 2222),
                PortPublish("127.0.0.1", 2222, 8080),
            )
        },
    ],
    ids=lambda changes: next(iter(changes)) + ":" + str(list(changes.values())[0])[:20],
)
def test_invalid_spec_is_rejected(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        spec(**changes)


@pytest.mark.parametrize(
    ("host", "host_port", "container_port"),
    [("localhost", 1, 1), ("127.0.0.1", 0, 1), ("127.0.0.1", 65536, 1), ("127.0.0.1", 1, 0)],
)
def test_invalid_publish_is_rejected(host: str, host_port: int, container_port: int) -> None:
    with pytest.raises(ValueError):
        PortPublish(host, host_port, container_port)


def test_publish_argument_brackets_ipv6() -> None:
    assert PortPublish("127.0.0.1", 22, 2222).argument() == "127.0.0.1:22:2222"
    assert PortPublish("::1", 22, 2222).argument() == "[::1]:22:2222"


# --- docker run -----------------------------------------------------------------------


def test_run_command_contains_every_hardening_measure_and_no_shell() -> None:
    argv = DockerCli().run_command(spec(network="honeyforge-low"))

    assert argv[:3] == ["docker", "run", "--detach"]
    for flag in ("--read-only", "--init"):
        assert flag in argv
    pairs = {
        ("--cap-drop", "ALL"),
        ("--security-opt", "no-new-privileges:true"),
        ("--restart", "no"),
        ("--user", "65532:65532"),
        ("--pids-limit", "64"),
        ("--memory", "128m"),
        ("--cpus", "0.5"),
        ("--network", "honeyforge-low"),
        ("--publish", "127.0.0.1:2222:2222"),
        ("--label", "honeyforge.managed=true"),
        ("--name", NAME),
    }
    assert pairs <= set(zip(argv, argv[1:], strict=False))
    assert all(isinstance(part, str) for part in argv)


def test_container_arguments_follow_the_image_and_cannot_become_docker_options() -> None:
    argv = DockerCli().run_command(spec())
    image_at = argv.index("honeyforge-low:local")

    assert argv[image_at + 1 :] == ["--host=0.0.0.0", "--banner=-dash"]
    assert "--banner=-dash" not in argv[:image_at]


def test_run_command_omits_network_when_not_configured() -> None:
    assert "--network" not in DockerCli().run_command(spec())


def test_run_executes_exactly_the_built_command() -> None:
    docker, runner = cli(CommandResult(0, "a" * 64 + "\n"))

    asyncio.run(docker.run(spec()))

    assert runner.commands == [docker.run_command(spec())]


@pytest.mark.parametrize(
    ("result", "unavailable"),
    [
        (CommandResult(125, "docker: invalid reference format"), False),
        (CommandResult(1, "Cannot connect to the Docker daemon at unix:///x"), True),
        (CommandResult(1, "Is the docker daemon running?"), True),
        (CommandResult(None, error="[Errno 2] No such file: docker"), True),
        (CommandResult(-9, timed_out=True), True),
        (CommandResult(-9, "partial", truncated=True), False),
    ],
    ids=["exit-125", "daemon-down", "daemon-not-running", "docker-missing", "timeout", "truncated"],
)
def test_run_failures_are_reported_with_availability_flag(
    result: CommandResult, unavailable: bool
) -> None:
    docker, _ = cli(result)

    with pytest.raises(BackendError) as error:
        asyncio.run(docker.run(spec()))

    assert error.value.unavailable is unavailable


def test_error_message_is_bounded_and_single_line() -> None:
    docker, _ = cli(CommandResult(1, "line\n" * 5000))

    with pytest.raises(BackendError) as error:
        asyncio.run(docker.run(spec()))

    assert "\n" not in error.value.message
    assert len(error.value.message) < 300


# --- docker inspect ---------------------------------------------------------------------


def inspect_json(**state: Any) -> str:
    body = {
        "Id": CONTAINER_ID,
        "State": {"Status": "running", "Running": True, "ExitCode": 0, **state},
        "Labels": {"honeyforge.managed": "true", "honeyforge.revision": "3"},
    }
    return json.dumps(body)


def test_inspect_parses_state_and_labels() -> None:
    docker, runner = cli(CommandResult(0, inspect_json()))

    observed = asyncio.run(docker.inspect(NAME))

    assert observed is not None
    assert (observed.status, observed.running, observed.exit_code) == ("running", True, 0)
    assert observed.labels["honeyforge.revision"] == "3"
    assert observed.container_id == CONTAINER_ID
    assert runner.commands == [
        [
            "docker",
            "inspect",
            "--type",
            "container",
            "--format",
            '{"Id":{{json .Id}},"State":{{json .State}},"Labels":{{json .Config.Labels}}}',
            NAME,
        ]
    ]


def test_inspect_of_exited_container() -> None:
    docker, _ = cli(CommandResult(0, inspect_json(Status="exited", Running=False, ExitCode=137)))

    observed = asyncio.run(docker.inspect(NAME))

    assert observed is not None
    assert (observed.running, observed.exit_code) == (False, 137)


def test_inspect_null_labels_and_odd_exit_code() -> None:
    body = {
        "Id": CONTAINER_ID,
        "State": {"Status": "created", "Running": False, "ExitCode": "x"},
        "Labels": None,
    }
    docker, _ = cli(CommandResult(0, json.dumps(body)))

    observed = asyncio.run(docker.inspect(NAME))

    assert observed is not None
    assert observed.labels == {} and observed.exit_code is None


def test_inspect_of_missing_container_returns_none() -> None:
    docker, _ = cli(CommandResult(1, "Error response from daemon: No such container: x"))

    assert asyncio.run(docker.inspect(NAME)) is None


@pytest.mark.parametrize(
    "output",
    ["not json", "[]", "{}", '{"State": {"Status": 1}}', '{"State": {"Running": true}}'],
    ids=["text", "list", "empty", "no-running", "no-status"],
)
def test_inspect_unexpected_format_is_an_error(output: str) -> None:
    docker, _ = cli(CommandResult(0, output))

    with pytest.raises(BackendError):
        asyncio.run(docker.inspect(NAME))


def test_inspect_other_failure_is_an_error() -> None:
    docker, _ = cli(CommandResult(1, "permission denied"))

    with pytest.raises(BackendError):
        asyncio.run(docker.inspect(NAME))


INVALID_CONTAINER_IDS = [
    None,
    "",
    "a" * 12,
    "a" * 63,
    "a" * 65,
    "A" * 64,
    "g" * 64,
    "--force",
    NAME,
    CONTAINER_ID + "\n",
    1,
    True,
    [CONTAINER_ID],
]


@pytest.mark.parametrize("container_id", INVALID_CONTAINER_IDS)
def test_inspect_rejects_invalid_container_identity(container_id: Any) -> None:
    body = json.loads(inspect_json())
    body["Id"] = container_id
    docker, _ = cli(CommandResult(0, json.dumps(body)))

    with pytest.raises(BackendError, match="неожиданный формат"):
        asyncio.run(docker.inspect(NAME))


def test_inspect_rejects_a_missing_container_identity() -> None:
    body = json.loads(inspect_json())
    del body["Id"]
    docker, _ = cli(CommandResult(0, json.dumps(body)))

    with pytest.raises(BackendError, match="неожиданный формат"):
        asyncio.run(docker.inspect(NAME))


def test_legacy_observation_construction_defaults_identity_to_none() -> None:
    observed = ContainerObservation(NAME, "running", True, 0, {"honeyforge.managed": "true"})

    assert observed.container_id is None


# --- docker rm --------------------------------------------------------------------------


def test_remove_runs_forced_removal() -> None:
    docker, runner = cli(CommandResult(0, NAME))

    asyncio.run(docker.remove(NAME))

    assert runner.commands == [["docker", "rm", "--force", "--volumes", NAME]]


def test_observation_stop_and_remove_use_only_the_immutable_id() -> None:
    docker, runner = cli(CommandResult(0, CONTAINER_ID))
    observed = observation()

    asyncio.run(docker.stop(observed, 10))
    asyncio.run(docker.remove(observed))

    assert runner.commands == [
        ["docker", "stop", "--time", "10", CONTAINER_ID],
        ["docker", "rm", "--force", "--volumes", CONTAINER_ID],
    ]


def test_remove_of_missing_container_is_not_an_error() -> None:
    docker, _ = cli(CommandResult(1, "Error response from daemon: No such container: x"))

    asyncio.run(docker.remove(NAME))


def test_remove_failure_is_an_error() -> None:
    docker, _ = cli(CommandResult(1, "container is in use"))

    with pytest.raises(BackendError):
        asyncio.run(docker.remove(NAME))


@pytest.mark.parametrize("operation", ["inspect", "remove", "stop"])
@pytest.mark.parametrize("name", ["postgres", "honeyforge", "honeyforge-a;b", "-f", ""])
def test_foreign_containers_are_never_touched(operation: str, name: str) -> None:
    docker, runner = cli()

    with pytest.raises(BackendError):
        if operation == "stop":
            asyncio.run(docker.stop(name, 5))
        else:
            asyncio.run(getattr(docker, operation)(name))

    assert runner.commands == []


@pytest.mark.parametrize("operation", ["remove", "stop"])
@pytest.mark.parametrize("name", ["postgres", "honeyforge", "honeyforge-a;b", "-f", ""])
def test_foreign_observations_are_never_touched(operation: str, name: str) -> None:
    docker, runner = cli()
    observed = observation(name=name)

    with pytest.raises(BackendError):
        if operation == "stop":
            asyncio.run(docker.stop(observed, 5))
        else:
            asyncio.run(docker.remove(observed))

    assert runner.commands == []


@pytest.mark.parametrize("operation", ["remove", "stop"])
@pytest.mark.parametrize("container_id", INVALID_CONTAINER_IDS)
def test_invalid_observation_identity_never_reaches_runner(
    operation: str, container_id: Any
) -> None:
    docker, runner = cli()
    observed = observation(container_id=container_id)

    with pytest.raises(BackendError, match="полного Docker ID"):
        if operation == "stop":
            asyncio.run(docker.stop(observed, 5))
        else:
            asyncio.run(docker.remove(observed))

    assert runner.commands == []


class ReusingNameRunner(FakeRunner):
    """Моделирует замену контейнера с сохранением имени после inspect либо stop."""

    def __init__(self, replace_after: str) -> None:
        super().__init__()
        self.replace_after = replace_after
        self.containers = {CONTAINER_ID: NAME}

    def run(self, command: Sequence[str]) -> CommandResult:
        self.commands.append(list(command))
        operation = command[1]
        if operation == "inspect":
            result = CommandResult(0, inspect_json())
        else:
            target = command[-1]
            container_id = next(
                (key for key, name in self.containers.items() if target in (key, name)), None
            )
            if container_id is None:
                return CommandResult(1, f"Error response from daemon: No such container: {target}")
            if operation == "rm":
                del self.containers[container_id]
            result = CommandResult(0, container_id)
        if operation == self.replace_after:
            self.containers = {REPLACEMENT_ID: NAME}
        return result


@pytest.mark.parametrize("replace_after", ["inspect", "stop"])
def test_cleanup_never_targets_reused_container_name(replace_after: str) -> None:
    """Регрессия: новый контейнер с тем же именем нельзя остановить или удалить."""
    runner = ReusingNameRunner(replace_after)
    docker = DockerCli(runner)
    observed = asyncio.run(docker.inspect(NAME))
    assert observed is not None

    asyncio.run(docker.stop(observed, 10))
    asyncio.run(docker.remove(observed))

    assert runner.containers == {REPLACEMENT_ID: NAME}
    assert len(runner.commands) == 3
    assert runner.commands[1:] == [
        ["docker", "stop", "--time", "10", CONTAINER_ID],
        ["docker", "rm", "--force", "--volumes", CONTAINER_ID],
    ]


# --- Различение «контейнера нет» и «Docker недоступен» -------------------------------------

UNAVAILABLE_OUTPUTS = [
    "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. "
    "Is the docker daemon running?",
    'error during connect: Get "http://%2F%2F.%2Fpipe%2Fdocker_engine/v1.47/containers/x/json": '
    "open //./pipe/docker_engine: The system cannot find the file specified.",
    "dial unix /var/run/docker.sock: connect: no such file or directory",
    "permission denied while trying to connect to the Docker daemon socket "
    "at unix:///var/run/docker.sock",
]


@pytest.mark.parametrize("operation", ["inspect", "remove", "stop"])
@pytest.mark.parametrize(
    "output", UNAVAILABLE_OUTPUTS, ids=["daemon", "windows-pipe", "no-socket", "denied"]
)
def test_unavailable_daemon_is_never_taken_for_a_missing_container(
    operation: str, output: str
) -> None:
    """Регрессия: «connect: no such file or directory» принималось за отсутствие контейнера."""
    docker, _ = cli(CommandResult(1, output))

    with pytest.raises(BackendError) as error:
        if operation == "stop":
            asyncio.run(docker.stop(NAME, 5))
        else:
            asyncio.run(getattr(docker, operation)(NAME))

    assert error.value.unavailable is True


@pytest.mark.parametrize("operation", ["remove", "stop"])
@pytest.mark.parametrize(
    "output", UNAVAILABLE_OUTPUTS, ids=["daemon", "windows-pipe", "no-socket", "denied"]
)
def test_unavailable_daemon_is_reported_for_observation(operation: str, output: str) -> None:
    docker, _ = cli(CommandResult(1, output))

    with pytest.raises(BackendError) as error:
        if operation == "stop":
            asyncio.run(docker.stop(observation(), 5))
        else:
            asyncio.run(docker.remove(observation()))

    assert error.value.unavailable is True


@pytest.mark.parametrize(
    "output",
    ["Error response from daemon: No such container: x", "Error: No such object: x"],
    ids=["container", "object"],
)
def test_precise_missing_messages_mean_no_container(output: str) -> None:
    docker, _ = cli(CommandResult(1, output))

    assert asyncio.run(docker.inspect(NAME)) is None
    asyncio.run(docker.remove(NAME))
    asyncio.run(docker.stop(NAME, 5))
    asyncio.run(docker.remove(observation()))
    asyncio.run(docker.stop(observation(), 5))


def test_inspect_skips_cli_warnings_before_the_json_line() -> None:
    warning = "WARNING: Error loading config file: /home/x/.docker/config.json: permission denied"
    docker, _ = cli(CommandResult(0, warning + "\n" + inspect_json()))

    observed = asyncio.run(docker.inspect(NAME))

    assert observed is not None and observed.running


# --- Мягкая остановка, журнал контейнера, отказ от скачивания образов ------------------------


def test_stop_sends_sigterm_with_a_bounded_timeout() -> None:
    docker, runner = cli(CommandResult(0, NAME))

    asyncio.run(docker.stop(NAME, 10))

    assert runner.commands == [["docker", "stop", "--time", "10", NAME]]


@pytest.mark.parametrize("timeout", [-1, 26, True])
def test_stop_timeout_is_bounded(timeout: int) -> None:
    docker, runner = cli()

    with pytest.raises(BackendError):
        asyncio.run(docker.stop(NAME, timeout))
    assert runner.commands == []


def test_run_limits_container_logs_and_never_pulls_images() -> None:
    argv = DockerCli().run_command(spec())
    pairs = set(zip(argv, argv[1:], strict=False))

    assert {
        ("--log-driver", "local"),
        ("--log-opt", "max-size=10m"),
        ("--log-opt", "max-file=3"),
        ("--pull", "never"),
    } <= pairs
    assert argv.index("--pull") < argv.index("honeyforge-low:local")


def test_image_and_network_checks_are_reusable() -> None:
    ContainerSpec.check_image("ghcr.io/kirit0zxc/honeyforge-low@sha256:" + "a" * 64)
    ContainerSpec.check_network("honeyforge-low")
    for bad in ("host", "none"):
        with pytest.raises(ValueError):
            ContainerSpec.check_network(bad)
    with pytest.raises(ValueError):
        ContainerSpec.check_image("--privileged")


# --- Монтирование каталога хоста ---------------------------------------------------------

JOURNAL_HOST = "/var/lib/honeyforge/low/11111111-1111-4111-8111-111111111111"


@pytest.mark.parametrize(
    "source",
    [JOURNAL_HOST, r"C:\HoneyForge\journal data", "/srv/my journal (1)", "/a=b", "/srv/журнал"],
    ids=["posix", "windows-with-spaces", "posix-with-spaces", "equals", "unicode"],
)
def test_bind_mount_accepts_ordinary_absolute_paths(source: str) -> None:
    mount = BindMount(source, "/journal")

    assert mount.read_only is False
    assert mount.argument() == f"type=bind,source={source},target=/journal"


@pytest.mark.parametrize("read_only", [False, True])
def test_bind_mount_renders_the_explicit_access_mode(read_only: bool) -> None:
    mount = BindMount(JOURNAL_HOST, "/journal", read_only=read_only)

    expected = f"type=bind,source={JOURNAL_HOST},target=/journal"
    assert mount.argument() == expected + (",readonly" if read_only else "")


@pytest.mark.parametrize("read_only", [0, 1, 0.0, 1.0, "", "false", "true", None, [], {}])
def test_bind_mount_refuses_non_bool_access_mode(read_only: Any) -> None:
    with pytest.raises(ValueError, match="read_only"):
        BindMount(JOURNAL_HOST, "/journal", read_only=read_only)


@pytest.mark.parametrize(
    "source",
    [
        "",
        "relative/path",
        "journal",
        "/a/../b",
        r"C:\x\..\y",
        "/a,readonly",
        '/a"b',
        "/a\nb",
        "/",
        "C:\\",
        "/var/run/docker.sock",
        "/var/run",
        "/run",
        "//var/run",
        "/var",
        "/a'b",
        "/a\u202eb",
        "/a\u200bb",
    ],
    ids=[
        "empty",
        "relative",
        "bare",
        "dotdot",
        "dotdot-windows",
        "comma",
        "quote",
        "newline",
        "posix-root",
        "windows-root",
        "docker-socket",
        "var-run",
        "run",
        "double-slash-run",
        "var",
        "single-quote",
        "bidi",
        "zero-width",
    ],
)
@pytest.mark.parametrize("read_only", [False, True])
def test_bind_mount_refuses_unsafe_sources(source: str, read_only: bool) -> None:
    with pytest.raises(ValueError):
        BindMount(source, "/journal", read_only=read_only)


@pytest.mark.parametrize("target", ["journal", "/", "/a/../b", "/a b", "/a,b", ""])
@pytest.mark.parametrize("read_only", [False, True])
def test_bind_mount_refuses_unsafe_targets(target: str, read_only: bool) -> None:
    with pytest.raises(ValueError):
        BindMount("/srv/journal", target, read_only=read_only)


def test_spec_with_a_mount_renders_the_mount_option() -> None:
    argv = DockerCli().run_command(spec(mounts=(BindMount(JOURNAL_HOST, "/journal"),)))

    assert ("--mount", f"type=bind,source={JOURNAL_HOST},target=/journal") in set(
        zip(argv, argv[1:], strict=False)
    )
    assert "--read-only" in argv, "корневая файловая система остаётся только для чтения"
    assert argv.index("--mount") < argv.index("honeyforge-low:local")


def test_spec_with_read_only_config_and_writable_journal_preserves_both_modes() -> None:
    argv = DockerCli().run_command(
        spec(
            mounts=(
                BindMount("/srv/honeyforge/config", "/config", read_only=True),
                BindMount(JOURNAL_HOST, "/journal"),
            )
        )
    )

    mount_indices = [index for index, argument in enumerate(argv) if argument == "--mount"]
    assert [argv[index + 1] for index in mount_indices] == [
        "type=bind,source=/srv/honeyforge/config,target=/config,readonly",
        f"type=bind,source={JOURNAL_HOST},target=/journal",
    ]
    assert all(index + 1 < argv.index("honeyforge-low:local") for index in mount_indices)
    assert "--read-only" in argv, "корневая файловая система остаётся только для чтения"


def test_spec_without_mounts_has_no_mount_option() -> None:
    assert "--mount" not in DockerCli().run_command(spec())


@pytest.mark.parametrize("first_read_only", [False, True])
@pytest.mark.parametrize("second_read_only", [False, True])
def test_duplicate_mount_targets_are_refused(first_read_only: bool, second_read_only: bool) -> None:
    with pytest.raises(ValueError):
        spec(
            mounts=(
                BindMount("/a", "/journal", read_only=first_read_only),
                BindMount("/b", "/journal", read_only=second_read_only),
            )
        )
