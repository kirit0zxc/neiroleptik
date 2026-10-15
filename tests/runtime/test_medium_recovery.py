"""Регрессии Medium: ответ порта, подготовка хоста и восстановление после рестарта.

Проверяется, что отказ в любой из этих точек остаётся кодом драйвера и не выводит
исключение в цикл исполнителя, а также что сверка сохранённой попытки запуска не
повторяет `docker run`. Настоящий Cowrie здесь не запускается.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest
from test_medium_driver import (
    DEPLOYMENT_ID,
    CowrieBackend,
    MediumDriver,
    container_chown,
    deployment,
    free_port,
    settings,
)

from agent.runtime.drivers.base import DriverError, DriverErrorCode, DriverState

# Фикстура подменяет смену владельца: в подмене Docker процесс остаётся нашим.
__all__ = ["container_chown"]


def test_flooding_port_fails_health_check_and_cleans_up(tmp_path: Path) -> None:
    """Ответ без перевода строки не выводит исключение из драйвера.

    `StreamReader.readline` поднимает `ValueError`, когда ответ превысил предел буфера.
    Без его перехвата контейнер остался бы работать, слот остался бы `STOPPED`, а
    исполнитель завершился бы без ACK центру.
    """

    async def scenario() -> None:
        backend = CowrieBackend(flood=True)
        driver = MediumDriver(
            DEPLOYMENT_ID, backend, settings(tmp_path / "state", start_timeout=0.4)
        )
        try:
            with pytest.raises(DriverError) as failure:
                await driver.start(deployment(port=free_port()))
            assert failure.value.code is DriverErrorCode.HEALTHCHECK_FAILED
            assert "длиннее предела строки" in failure.value.message
            assert not backend.containers, "контейнер неудачной попытки удалён"
            assert [call[0] for call in backend.calls].count("remove") == 1
            assert (await driver.inspect()).state is DriverState.FAILED
        finally:
            await backend.close()

    asyncio.run(scenario())


def test_unexpected_host_failure_becomes_start_failed(tmp_path: Path) -> None:
    """Исключение подготовки хоста, которого уровень не предусмотрел, не уходит наружу."""

    class BrokenHost(MediumDriver):
        """Подготовка хоста падает не-OSError: так ведёт себя os.chown при негодном uid."""

        def _prepare_host(self, blueprint, instance_id, spec) -> None:  # type: ignore[no-untyped-def]
            raise OverflowError("uid за пределом uid_t")

    async def scenario() -> None:
        backend = CowrieBackend()
        driver = BrokenHost(DEPLOYMENT_ID, backend, settings(tmp_path / "state"))
        try:
            with pytest.raises(DriverError) as failure:
                await driver.start(deployment(port=free_port()))
            assert failure.value.code is DriverErrorCode.START_FAILED
            assert not backend.calls, "отказ произошёл до освобождения слота"
        finally:
            await backend.close()

    asyncio.run(scenario())


def test_container_user_owns_state_but_only_shares_group_on_credentials(
    tmp_path: Path, container_chown: Mock
) -> None:
    """Владение `conf/` остаётся за агентом: ловушке достаточно группы.

    Иначе процесс ловушки владел бы на хосте каталогом со своими учётными данными и
    мог бы их заменить. Проверяются аргументы смены владельца, а не права живого
    контейнера: на Windows `os.chown` отсутствует и подменён.
    """

    async def scenario() -> None:
        backend = CowrieBackend()
        root = tmp_path / "state"
        driver = MediumDriver(DEPLOYMENT_ID, backend, settings(root))
        try:
            await driver.start(deployment(port=free_port()))
            owners = {
                Path(call.args[0]): (call.args[1], call.args[2])
                for call in container_chown.call_args_list
            }
            relative = {str(path.relative_to(root)): owner for path, owner in owners.items()}
            instance = next(name for name in relative if name.count("\\") + name.count("/") == 1)
            assert relative[DEPLOYMENT_ID] == (-1, 1000)
            assert relative[instance] == (-1, 1000)
            assert relative[str(Path(instance) / "conf")] == (-1, 1000), (
                "конфигурация с паролями не принадлежит ловушке"
            )
            assert relative[str(Path(instance) / "state")] == (1000, 1000), (
                "состояние ловушка обязана писать"
            )
            assert relative[str(Path(instance) / "state" / "keys")] == (1000, 1000)
            # Корень состояния готовит оператор: драйвер его владельца не меняет.
            assert root not in owners
        finally:
            await backend.close()

    asyncio.run(scenario())


def test_recover_confirms_the_saved_instance_without_second_run(tmp_path: Path) -> None:
    """Сверка сохранённой попытки запуска не повторяет docker run."""
    plan = deployment(port=free_port())
    root = tmp_path / "state"

    async def scenario() -> None:
        backend = CowrieBackend()
        try:
            started = await MediumDriver(DEPLOYMENT_ID, backend, settings(root)).start(plan)
            # Новый драйвер — как после рестарта агента: состояния в памяти нет.
            restarted = MediumDriver(DEPLOYMENT_ID, backend, settings(root))
            status = await restarted.recover(plan, started.instance_id)
            assert status.state is DriverState.RUNNING and status.healthy
            assert status.instance_id == started.instance_id and status.revision == 1
            assert [call[0] for call in backend.calls].count("run") == 1
            # Чужая попытка запуска не принимается за свою.
            failing = MediumDriver(DEPLOYMENT_ID, backend, settings(root))
            with pytest.raises(DriverError) as failure:
                await failing.recover(plan, str(uuid4()))
            assert failure.value.code is DriverErrorCode.INVALID_STATE
            assert [call[0] for call in backend.calls].count("run") == 1
        finally:
            await backend.close()

    asyncio.run(scenario())


def test_recover_reports_a_flooding_port_without_cleanup(tmp_path: Path) -> None:
    """Та же защита от ответа без перевода строки действует в ветке восстановления."""
    plan = deployment(port=free_port())
    root = tmp_path / "state"

    async def scenario() -> None:
        backend = CowrieBackend()
        try:
            started = await MediumDriver(DEPLOYMENT_ID, backend, settings(root)).start(plan)
            backend.flood = True
            restarted = MediumDriver(DEPLOYMENT_ID, backend, settings(root, start_timeout=0.4))
            with pytest.raises(DriverError) as failure:
                await restarted.recover(plan, started.instance_id)
            assert failure.value.code is DriverErrorCode.HEALTHCHECK_FAILED
            # Восстановление не делает аварийную очистку: право на мутацию даёт lease.
            assert [call[0] for call in backend.calls].count("remove") == 0
        finally:
            await backend.close()

    asyncio.run(scenario())
