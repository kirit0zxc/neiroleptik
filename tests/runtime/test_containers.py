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
    ContainerSpec,
    DockerCli,
    PortPublish,
)
from infra.process import CommandResult

NAME = "honeyforge-low-11111111-1111-4111-8111-111111111111"


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
    assert runner.commands == [
        [
            "docker",
            "inspect",
            "--type",
            "container",
            "--format",
            '{"State":{{json .State}},"Labels":{{json .Config.Labels}}}',
            NAME,
        ]
    ]


def test_inspect_of_exited_container() -> None:
    docker, _ = cli(CommandResult(0, inspect_json(Status="exited", Running=False, ExitCode=137)))

    observed = asyncio.run(docker.inspect(NAME))

    assert observed is not None
    assert (observed.running, observed.exit_code) == (False, 137)


def test_inspect_null_labels_and_odd_exit_code() -> None:
    body = {"State": {"Status": "created", "Running": False, "ExitCode": "x"}, "Labels": None}
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


# --- docker rm --------------------------------------------------------------------------


def test_remove_runs_forced_removal() -> None:
    docker, runner = cli(CommandResult(0, NAME))

    asyncio.run(docker.remove(NAME))

    assert runner.commands == [["docker", "rm", "--force", "--volumes", NAME]]


def test_remove_of_missing_container_is_not_an_error() -> None:
    docker, _ = cli(CommandResult(1, "Error response from daemon: No such container: x"))

    asyncio.run(docker.remove(NAME))


def test_remove_failure_is_an_error() -> None:
    docker, _ = cli(CommandResult(1, "container is in use"))

    with pytest.raises(BackendError):
        asyncio.run(docker.remove(NAME))


@pytest.mark.parametrize("operation", ["inspect", "remove"])
@pytest.mark.parametrize("name", ["postgres", "honeyforge", "honeyforge-a;b", "-f", ""])
def test_foreign_containers_are_never_touched(operation: str, name: str) -> None:
    docker, runner = cli()

    with pytest.raises(BackendError):
        asyncio.run(getattr(docker, operation)(name))

    assert runner.commands == []


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
    [JOURNAL_HOST, r"C:\Users\Karp\Desktop\lovyshka jopera\journal", "/srv/my journal (1)"],
    ids=["posix", "windows-with-spaces", "posix-with-spaces"],
)
def test_bind_mount_accepts_ordinary_absolute_paths(source: str) -> None:
    assert BindMount(source, "/journal").argument() == f"type=bind,source={source},target=/journal"


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
        "/a=b",
        "/a\nb",
    ],
    ids=[
        "empty",
        "relative",
        "bare",
        "dotdot",
        "dotdot-windows",
        "comma",
        "quote",
        "equals",
        "newline",
    ],
)
def test_bind_mount_refuses_unsafe_sources(source: str) -> None:
    with pytest.raises(ValueError):
        BindMount(source, "/journal")


@pytest.mark.parametrize("target", ["journal", "/", "/a/../b", "/a b", "/a,b", ""])
def test_bind_mount_refuses_unsafe_targets(target: str) -> None:
    with pytest.raises(ValueError):
        BindMount("/srv/journal", target)


def test_spec_with_a_mount_renders_the_mount_option() -> None:
    argv = DockerCli().run_command(spec(mounts=(BindMount(JOURNAL_HOST, "/journal"),)))

    assert ("--mount", f"type=bind,source={JOURNAL_HOST},target=/journal") in set(
        zip(argv, argv[1:], strict=False)
    )
    assert "--read-only" in argv, "корневая файловая система остаётся только для чтения"
    assert argv.index("--mount") < argv.index("honeyforge-low:local")


def test_spec_without_mounts_has_no_mount_option() -> None:
    assert "--mount" not in DockerCli().run_command(spec())


def test_duplicate_mount_targets_are_refused() -> None:
    with pytest.raises(ValueError):
        spec(mounts=(BindMount("/a", "/journal"), BindMount("/b", "/journal")))
