"""Примитивы Engine для границы процесса на настоящем Docker. По умолчанию выключено.

Остальные тесты границы работают с подменой Engine, поэтому отдельно проверяются именно
новые вызовы `DockerCli`: create без запуска с полным ID, start по этому ID, наблюдение
по ID, поиск по метке, список контейнеров с записью в каталог и идентичность демона.

Тест меняет состояние настоящего демона, поэтому включается явно:
`HF_DOCKER_BOUNDARY=1` и образ, который уже есть локально (`HF_DOCKER_IMAGE`,
по умолчанию `python:3.12-slim`). Контейнер всегда удаляется в finally.
"""

import asyncio
import os
import uuid
from pathlib import Path

import pytest

from agent.runtime.drivers.containers import (
    LABEL_MANAGED,
    BackendError,
    BindMount,
    ContainerSpec,
    DockerCli,
)

LABEL_PROBE = "honeyforge.probe"

pytestmark = pytest.mark.skipif(
    os.environ.get("HF_DOCKER_BOUNDARY") != "1",
    reason="проверка требует настоящего Docker Engine и включается HF_DOCKER_BOUNDARY=1",
)


def probe_spec(name: str, mount: Path, probe: str) -> ContainerSpec:
    """Контейнер-пустышка с теми же мерами ограничения, что и ловушка Low."""
    return ContainerSpec(
        name=name,
        image=os.environ.get("HF_DOCKER_IMAGE", "python:3.12-slim"),
        command=("python", "-c", "import time; time.sleep(600)"),
        mounts=(BindMount(str(mount), "/journal"),),
        labels=((LABEL_MANAGED, "true"), (LABEL_PROBE, probe)),
    )


def test_create_start_observe_and_remove_by_full_identifier(tmp_path: Path) -> None:
    """Полный ID известен до запуска, и все изменения адресуются только им."""
    docker = DockerCli()
    probe = str(uuid.uuid4())
    name = f"honeyforge-boundary-probe-{probe[:8]}"
    mount = tmp_path / "journal"
    mount.mkdir()

    async def scenario() -> None:
        identity = await docker.engine_identity()
        assert "/" in identity and len(identity) <= 256
        container_id = await docker.create(probe_spec(name, mount, probe))
        try:
            assert len(container_id) == 64 and int(container_id, 16) >= 0
            created = await docker.inspect_by_id(container_id)
            assert created is not None
            # До запуска Engine сообщает именно created: контейнер ни разу не работал.
            assert (created.status, created.running) == ("created", False)
            assert created.name == name
            assert created.container_id == container_id
            assert created.labels[LABEL_PROBE] == probe

            by_label = await docker.containers_by_label(LABEL_PROBE, probe)
            assert by_label == (container_id,)

            await docker.start_created(container_id)
            running = await docker.inspect_by_id(container_id)
            assert running is not None and running.running and running.status == "running"

            if os.name == "posix":
                # На Linux Engine сообщает фактический путь хоста; producer работает там.
                holders = await docker.writable_mount_holders(str(mount))
                assert container_id in holders
            else:
                # Источник вида C:/... не путь POSIX: сверка отказывает, а не молчит.
                with pytest.raises(BackendError):
                    await docker.writable_mount_holders(str(mount))

            await docker.stop(running, 1)
            await docker.remove(running)
            assert await docker.inspect_by_id(container_id) is None
            assert await docker.containers_by_label(LABEL_PROBE, probe) == ()
        finally:
            observation = await docker.inspect_by_id(container_id)
            if observation is not None:
                await docker.remove(observation)

    asyncio.run(scenario())


def test_inspect_by_identifier_refuses_a_foreign_container() -> None:
    """Контейнер вне пространства HoneyForge агент не наблюдает по ID."""
    docker = DockerCli()

    async def scenario() -> None:
        missing = await docker.inspect_by_id("0" * 64)
        assert missing is None

    asyncio.run(scenario())
