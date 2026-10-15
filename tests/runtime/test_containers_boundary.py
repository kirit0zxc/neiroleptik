"""Примитивы DockerCli для доверенной границы процесса на подмене исполнителя.

Эти вызовы отделяют создание контейнера от его запуска, адресуют контейнер только
полным Docker ID и сверяют список контейнеров с записью в каталог слота. Проверка
идёт по точным спискам аргументов и по фактическим ответам Engine; отдельный прогон
с настоящим демоном — `test_boundary_docker.py`.
"""

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
CONTAINER_ID = "0123456789abcdef" * 4
OTHER_ID = "f" * 64
LABEL_WRITER = "honeyforge.writer_id"
WRITER = "44444444-4444-4444-8444-444444444444"
# Фактический ответ Docker Desktop 29.5.2 на Windows: источник сообщается как C:/...
WINDOWS_MOUNTS = (
    '{"id":"ffe0c7770e2793fa1dc93bb7d8212db47042a57f01261bb25c9d63cdee03fc18",'
    '"mounts":[{"Destination":"/journal","Mode":"","Propagation":"rprivate","RW":true,'
    '"Source":"C:/Users/Karp/AppData/Local/Temp/journalprobe","Type":"bind"}]}'
)


def spec(**changes: Any) -> ContainerSpec:
    values: dict[str, Any] = {
        "name": NAME,
        "image": "honeyforge-low:local",
        "command": ("--host=0.0.0.0", "--journal-first-seq=2"),
        "publish": (PortPublish("127.0.0.1", 2222, 2222),),
        "mounts": (BindMount("/srv/honeyforge/journals/slot", "/journal"),),
        "labels": (("honeyforge.managed", "true"), (LABEL_WRITER, WRITER)),
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


def inspect_body(container_id: str = CONTAINER_ID, status: str = "created") -> str:
    """Ответ docker inspect в том же формате, который запрашивает DockerCli."""
    return json.dumps(
        {
            "Id": container_id,
            "State": {"Status": status, "Running": status == "running", "ExitCode": 0},
            "Labels": {"honeyforge.managed": "true", LABEL_WRITER: WRITER},
            "Name": f"/{NAME}",
        }
    )


def test_create_command_repeats_run_hardening_without_detach() -> None:
    """docker create отличается от run только подкомандой и отсутствием --detach."""
    docker = DockerCli()
    run = docker.run_command(spec())
    create = docker.create_command(spec())
    assert run[:3] == ["docker", "run", "--detach"]
    assert create[:2] == ["docker", "create"]
    assert "--detach" not in create
    assert create[2:] == run[3:]
    # Команда ловушки по-прежнему идёт после образа и не разбирается docker.
    assert create[-2:] == ["--host=0.0.0.0", "--journal-first-seq=2"]


def test_create_returns_the_full_identifier_from_the_engine() -> None:
    """Полный ID берётся из ответа Engine на create и сохраняется до start."""
    docker, runner = cli(CommandResult(0, f"{CONTAINER_ID}\n"))
    assert asyncio.run(docker.create(spec())) == CONTAINER_ID
    assert runner.commands == [docker.create_command(spec())]


def test_create_refuses_an_answer_without_a_full_identifier() -> None:
    """Сокращённый префикс или имя вместо ID не принимаются."""
    docker, _ = cli(CommandResult(0, f"{CONTAINER_ID[:12]}\n"))
    with pytest.raises(BackendError):
        asyncio.run(docker.create(spec()))


def test_create_skips_warnings_before_the_identifier() -> None:
    """Предупреждения CLI идут в тот же поток и не мешают прочитать ID."""
    docker, _ = cli(CommandResult(0, f"WARNING: platform mismatch\n{CONTAINER_ID}\n"))
    assert asyncio.run(docker.create(spec())) == CONTAINER_ID


def test_start_and_inspect_use_only_the_full_identifier() -> None:
    """Запуск и наблюдение адресуют контейнер полным ID, а не именем слота."""
    docker, runner = cli(CommandResult(0, ""))
    asyncio.run(docker.start_created(CONTAINER_ID))
    assert runner.commands == [["docker", "start", CONTAINER_ID]]
    for wrong in ("", NAME, CONTAINER_ID[:12], CONTAINER_ID.upper()):
        with pytest.raises(BackendError):
            asyncio.run(docker.start_created(wrong))


def test_inspect_by_id_returns_name_state_and_labels() -> None:
    """Наблюдение по ID приносит имя из ответа Engine и различает created и running."""
    docker, runner = cli(CommandResult(0, inspect_body()))
    created = asyncio.run(docker.inspect_by_id(CONTAINER_ID))
    assert created is not None
    assert (created.name, created.status, created.running) == (NAME, "created", False)
    assert created.container_id == CONTAINER_ID
    assert created.labels[LABEL_WRITER] == WRITER
    assert runner.commands[0][:4] == ["docker", "inspect", "--type", "container"]
    running = cli(CommandResult(0, inspect_body(status="running")))[0]
    observed = asyncio.run(running.inspect_by_id(CONTAINER_ID))
    assert observed is not None and observed.running


def test_inspect_by_id_refuses_a_foreign_name_or_another_container() -> None:
    """Чужое имя и ответ про другой контейнер не принимаются."""
    foreign = inspect_body().replace(f"/{NAME}", "/postgres")
    docker, _ = cli(CommandResult(0, foreign))
    with pytest.raises(BackendError):
        asyncio.run(docker.inspect_by_id(CONTAINER_ID))
    mismatched = cli(CommandResult(0, inspect_body(container_id=OTHER_ID)))[0]
    with pytest.raises(BackendError):
        asyncio.run(mismatched.inspect_by_id(CONTAINER_ID))


def test_missing_container_is_not_an_error_but_an_unavailable_daemon_is() -> None:
    """Отсутствие контейнера различается от недоступного демона и после create."""
    docker, _ = cli(CommandResult(1, "Error: No such object: " + CONTAINER_ID))
    assert asyncio.run(docker.inspect_by_id(CONTAINER_ID)) is None
    unavailable = cli(CommandResult(1, "Cannot connect to the Docker daemon"))[0]
    with pytest.raises(BackendError) as error:
        asyncio.run(unavailable.inspect_by_id(CONTAINER_ID))
    assert error.value.unavailable


def test_containers_by_label_filters_with_the_engine() -> None:
    """Поиск потерянного create идёт фильтром Engine по метке writer_id."""
    docker, runner = cli(CommandResult(0, f"{CONTAINER_ID}\n{OTHER_ID}\n"))
    assert asyncio.run(docker.containers_by_label(LABEL_WRITER, WRITER)) == (
        CONTAINER_ID,
        OTHER_ID,
    )
    assert runner.commands == [
        [
            "docker",
            "ps",
            "--all",
            "--no-trunc",
            "--filter",
            f"label={LABEL_WRITER}={WRITER}",
            "--format",
            "{{.ID}}",
        ]
    ]
    for key, value in ((LABEL_WRITER, ""), ("Writer Id", WRITER), (LABEL_WRITER, "a\nb")):
        with pytest.raises(BackendError):
            asyncio.run(docker.containers_by_label(key, value))


def test_engine_identity_joins_daemon_and_server_version() -> None:
    """Идентичность демона и версия сервера читаются одной командой docker info."""
    docker, runner = cli(CommandResult(0, '{"id":"daemon-2b7a","version":"29.5.2"}'))
    assert asyncio.run(docker.engine_identity()) == "daemon-2b7a/29.5.2"
    assert runner.commands[0][:2] == ["docker", "info"]
    broken = cli(CommandResult(0, "not json"))[0]
    with pytest.raises(BackendError):
        asyncio.run(broken.engine_identity())


def test_writable_mount_holders_detects_overlapping_sources() -> None:
    """Совпадение, вложенность и обратная вложенность источника считаются доступом."""
    slot = "/srv/honeyforge/journals/slot"
    listing = CommandResult(0, f"{CONTAINER_ID}\n{OTHER_ID}\n")
    mounts = CommandResult(
        0,
        "\n".join(
            [
                json.dumps(
                    {
                        "id": CONTAINER_ID,
                        "mounts": [
                            {"Source": "/srv/honeyforge/journals", "RW": True, "Type": "bind"}
                        ],
                    }
                ),
                json.dumps(
                    {
                        "id": OTHER_ID,
                        "mounts": [
                            {"Source": "/srv/other", "RW": True, "Type": "bind"},
                            {"Source": f"{slot}/nested", "RW": True, "Type": "bind"},
                        ],
                    }
                ),
            ]
        ),
    )
    docker, runner = cli(listing, mounts)
    assert asyncio.run(docker.writable_mount_holders(slot)) == (CONTAINER_ID, OTHER_ID)
    assert runner.commands[0][:4] == ["docker", "ps", "--all", "--no-trunc"]


def test_read_only_and_unrelated_mounts_are_not_holders() -> None:
    """Доступ только для чтения и чужой каталог ограждению не мешают."""
    slot = "/srv/honeyforge/journals/slot"
    listing = CommandResult(0, f"{CONTAINER_ID}\n")
    mounts = CommandResult(
        0,
        json.dumps(
            {
                "id": CONTAINER_ID,
                "mounts": [
                    {"Source": slot, "RW": False, "Type": "bind"},
                    {"Source": "/srv/elsewhere", "RW": True, "Type": "bind"},
                ],
            }
        ),
    )
    docker, _ = cli(listing, mounts)
    assert asyncio.run(docker.writable_mount_holders(slot)) == ()


def test_unexpected_mount_answer_is_refused() -> None:
    """Неожиданный состав ответа Engine не превращается в пустой список."""
    listing = CommandResult(0, f"{CONTAINER_ID}\n")
    docker, _ = cli(listing, CommandResult(0, '{"id":"' + CONTAINER_ID + '"}'))
    with pytest.raises(BackendError):
        asyncio.run(docker.writable_mount_holders("/srv/honeyforge/journals/slot"))


def test_windows_mount_source_is_refused_instead_of_silently_empty() -> None:
    """Источник Docker Desktop вида C:/... не путь POSIX: сверка отказывает (§8.2)."""
    docker, _ = cli(CommandResult(0, f"{CONTAINER_ID}\n"), CommandResult(0, WINDOWS_MOUNTS))
    with pytest.raises(BackendError):
        asyncio.run(docker.writable_mount_holders("C:/Users/Karp/AppData/Local/Temp/journalprobe"))


def test_too_many_containers_make_the_sweep_unknown() -> None:
    """Сверка ограничена: при большем числе контейнеров исход считается неизвестным."""
    listing = CommandResult(0, "\n".join(f"{number:064x}" for number in range(300)))
    docker, _ = cli(listing)
    with pytest.raises(BackendError):
        asyncio.run(docker.writable_mount_holders("/srv/honeyforge/journals/slot"))
    labels = cli(CommandResult(0, "\n".join(f"{number:064x}" for number in range(300))))[0]
    with pytest.raises(BackendError):
        asyncio.run(labels.containers_by_label(LABEL_WRITER, WRITER))


def test_batch_without_an_observation_for_each_identifier_is_unknown() -> None:
    """Неполный охват писателей — неизвестный исход, а не пустой список (§2).

    Строка отсутствия контейнера оправдывает только тот ID, для которого Engine её
    выдал: остальные наблюдаются отдельно, и молчание считается отказом.
    """
    slot = "/srv/honeyforge/journals/slot"
    listing = CommandResult(0, f"{CONTAINER_ID}\n{OTHER_ID}\n")
    # Успешный ответ без строк наблюдения: контейнеры не объявлены отсутствующими.
    silent = CommandResult(0, "")
    docker, _ = cli(listing, silent)
    with pytest.raises(BackendError):
        asyncio.run(docker.writable_mount_holders(slot))


def test_disappeared_container_of_a_batch_is_not_a_holder() -> None:
    """Исчезновение между ps и inspect допустимо: остальные наблюдения сохраняются."""
    slot = "/srv/honeyforge/journals/slot"
    listing = CommandResult(0, f"{CONTAINER_ID}\n{OTHER_ID}\n")
    partial = CommandResult(
        1,
        json.dumps({"id": CONTAINER_ID, "mounts": [{"Source": slot, "RW": True}]})
        + "\nError response from daemon: No such container: "
        + OTHER_ID,
    )
    missing = CommandResult(1, "Error response from daemon: No such container: " + OTHER_ID)
    docker, _ = cli(listing, partial, missing, missing)
    assert asyncio.run(docker.writable_mount_holders(slot)) == (CONTAINER_ID,)
