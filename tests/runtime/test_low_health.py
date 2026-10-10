"""Освобождение портов подтверждает отказ connect, а не таймаут или ошибку маршрута."""

from __future__ import annotations

import asyncio
import errno

import pytest

from agent.runtime.contract import ServiceSpec
from agent.runtime.drivers.base import DriverError, DriverErrorCode, DriverState
from agent.runtime.drivers.low import HealthTargets, LowDriver, LowHealthCheck
from tests.runtime.test_low_driver import (
    DEPLOYMENT_ID,
    InProcessBackend,
    deployment,
    free_port,
    settings,
)


def test_accepting_port_is_not_released_and_probe_sends_no_payload() -> None:
    async def scenario() -> None:
        payloads: list[bytes] = []

        async def receive(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                payloads.append(await reader.read())
            finally:
                writer.close()
                await writer.wait_closed()

        async with await asyncio.start_server(receive, "127.0.0.1", 0) as listener:
            port = listener.sockets[0].getsockname()[1]
            check = LowHealthCheck(timeout=0.1, interval=0.02, probe_timeout=0.01)
            assert not await check.wait_until_released(HealthTargets("127.0.0.1", port, None, None))
            await asyncio.sleep(0)
            assert payloads and all(payload == b"" for payload in payloads)
            assert listener.is_serving()

    asyncio.run(scenario())


def test_waits_for_both_ports_to_close_with_one_shared_deadline() -> None:
    async def scenario() -> None:
        def receive(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            writer.close()

        first = await asyncio.start_server(receive, "127.0.0.1", 0)
        second = await asyncio.start_server(receive, "127.0.0.1", 0)
        targets = HealthTargets(
            "127.0.0.1", first.sockets[0].getsockname()[1], None, second.sockets[0].getsockname()[1]
        )
        check = LowHealthCheck(timeout=8.0, interval=0.02, probe_timeout=0.01)
        waiting = asyncio.create_task(check.wait_until_released(targets))
        try:
            await asyncio.sleep(0.04)
            first.close()
            await first.wait_closed()
            await asyncio.sleep(0.04)
            assert not waiting.done()
            second.close()
            await second.wait_closed()
            assert await waiting
        finally:
            first.close()
            second.close()
            await first.wait_closed()
            await second.wait_closed()
            if not waiting.done():
                waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", [TimeoutError(), OSError(errno.ENETUNREACH, "no route")])
def test_other_network_failures_do_not_confirm_release(
    monkeypatch: pytest.MonkeyPatch, failure: OSError
) -> None:
    async def connect(_host: str, _port: int) -> None:
        raise failure

    monkeypatch.setattr(asyncio, "open_connection", connect)

    async def scenario() -> None:
        check = LowHealthCheck(timeout=0.04, interval=0.01, probe_timeout=0.01)
        assert not await check.wait_until_released(HealthTargets("127.0.0.1", 2200, None, 8800))

    asyncio.run(scenario())


def test_timeout_cancels_all_concurrent_probes_without_claiming_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered: set[int] = set()
    cancelled: set[int] = set()

    async def connect(_host: str, port: int) -> None:
        entered.add(port)
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.add(port)

    monkeypatch.setattr(asyncio, "open_connection", connect)

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        began = loop.time()
        check = LowHealthCheck(timeout=0.04, interval=0.01, probe_timeout=0.001)
        assert not await check.wait_until_released(HealthTargets("127.0.0.1", 2200, None, 8800))
        assert entered == cancelled == {2200, 8800}
        assert loop.time() - began < 0.5

    asyncio.run(scenario())


def test_slow_connection_refusal_is_parallel_and_not_cut_off_by_health_probe_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Отказ Windows может идти дольше probe_timeout; у двух портов общий срок."""

    async def scenario() -> None:
        entered: set[int] = set()
        both_started = asyncio.Event()

        async def connect(_host: str, port: int) -> None:
            entered.add(port)
            if len(entered) == 2:
                both_started.set()
            await both_started.wait()
            await asyncio.sleep(0.02)
            raise ConnectionRefusedError

        monkeypatch.setattr(asyncio, "open_connection", connect)
        check = LowHealthCheck(timeout=1.0, interval=0.01, probe_timeout=0.001)
        assert await check.wait_until_released(HealthTargets("127.0.0.1", 2200, None, 8800))
        assert entered == {2200, 8800}

    asyncio.run(scenario())


@pytest.mark.parametrize("host, expected", [("0.0.0.0", "127.0.0.1"), ("::", "::1")])
def test_wildcard_uses_loopback_for_every_published_port(
    monkeypatch: pytest.MonkeyPatch, host: str, expected: str
) -> None:
    calls: set[tuple[str, int]] = set()

    async def connect(address: str, port: int) -> None:
        calls.add((address, port))
        raise ConnectionRefusedError

    monkeypatch.setattr(asyncio, "open_connection", connect)

    async def scenario() -> None:
        check = LowHealthCheck(timeout=1.0, interval=0.01, probe_timeout=0.01)
        assert await check.wait_until_released(HealthTargets(host, 2200, None, 8800))
        assert calls == {(expected, 2200), (expected, 8800)}

    asyncio.run(scenario())


def test_driver_release_probe_shares_operation_lock_and_preserves_state() -> None:
    class PausedHealth(LowHealthCheck):
        def __init__(self) -> None:
            super().__init__(timeout=1.0, interval=0.01, probe_timeout=0.01)
            self.entered = asyncio.Event()
            self.proceed = asyncio.Event()

        async def wait_until_released(self, targets: HealthTargets) -> bool:
            self.entered.set()
            await self.proceed.wait()
            return False

    async def scenario() -> None:
        backend = InProcessBackend()
        health = PausedHealth()
        driver = LowDriver(DEPLOYMENT_ID, backend, settings(), health=health)
        probing = asyncio.create_task(driver.wait_until_released(deployment(tcp_port=2200)))
        await health.entered.wait()
        observing = asyncio.create_task(driver.inspect())
        await asyncio.sleep(0)
        assert not observing.done() and not backend.calls
        health.proceed.set()
        assert not await probing
        assert (await observing).state is DriverState.STOPPED
        assert backend.mutation_targets == []

    asyncio.run(scenario())


def test_driver_rejects_release_probe_for_another_deployment() -> None:
    async def scenario() -> None:
        backend = InProcessBackend()
        driver = LowDriver(DEPLOYMENT_ID, backend, settings())
        with pytest.raises(DriverError) as failed:
            await driver.wait_until_released(
                deployment(tcp_port=2200, deployment_id="99999999-9999-4999-8999-999999999999")
            )
        assert failed.value.code is DriverErrorCode.INVALID_STATE
        assert not backend.calls and not backend.mutation_targets

    asyncio.run(scenario())


def test_release_probe_does_not_depend_on_profile_text() -> None:
    """Регрессия R3-M2: профиль, отвергнутый при start, не мешает подтвердить stop."""

    async def scenario() -> None:
        backend = InProcessBackend()
        driver = LowDriver(DEPLOYMENT_ID, backend, settings(start_timeout=5.0))
        plan = deployment(http_port=free_port(), http="demo\tpage")
        with pytest.raises(DriverError) as rejected:
            await driver.start(plan)
        assert rejected.value.code is DriverErrorCode.UNSUPPORTED_PROFILE

        assert await driver.wait_until_released(plan)
        assert not backend.mutation_targets

    asyncio.run(scenario())


def test_release_covers_every_port_of_a_profile_rejected_at_start() -> None:
    """Регрессия: центр допускает два TCP-сервиса; stop проверяет порты обоих."""

    def close_at_once(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()

    async def scenario() -> None:
        listener = await asyncio.start_server(close_at_once, "127.0.0.1", 0)
        busy = listener.sockets[0].getsockname()[1]
        plan = deployment(
            service_list=(
                ServiceSpec("tcp", free_port(), "SSH-2.0-A"),
                ServiceSpec("tcp", busy, "SSH-2.0-B"),
            )
        )
        quick = LowDriver(DEPLOYMENT_ID, InProcessBackend(), settings(start_timeout=0.3))
        with pytest.raises(DriverError) as rejected:
            await quick.start(plan)
        assert rejected.value.code is DriverErrorCode.UNSUPPORTED_PROFILE
        try:
            assert not await quick.wait_until_released(plan), "второй TCP-порт ещё слушается"
        finally:
            listener.close()
            await listener.wait_closed()
        patient = LowDriver(DEPLOYMENT_ID, InProcessBackend(), settings(start_timeout=5.0))
        assert await patient.wait_until_released(plan)

    asyncio.run(scenario())


def test_release_probe_refuses_a_profile_of_another_level() -> None:
    async def scenario() -> None:
        backend = InProcessBackend()
        driver = LowDriver(DEPLOYMENT_ID, backend, settings())
        plan = deployment(level="medium", service_list=(ServiceSpec("ssh", free_port(), ""),))
        with pytest.raises(DriverError) as refused:
            await driver.wait_until_released(plan)
        assert refused.value.code is DriverErrorCode.UNSUPPORTED_PROFILE
        assert not backend.calls

    asyncio.run(scenario())


def test_release_probes_every_tcp_and_http_port_of_the_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Каждый tcp/http-порт профиля опрашивается; занятый любой из них не даёт stopped.

    Подмена connect делает проверку независимой от медленного отказа Windows loopback:
    свободные порты сразу отвечают отказом, занятый принимает соединение.
    """
    tcp, first_http, second_http = 2200, 8800, 8801
    plan = deployment(
        service_list=(
            ServiceSpec("tcp", tcp, "SSH-2.0-A"),
            ServiceSpec("http", first_http, "a"),
            ServiceSpec("http", second_http, "b"),
        )
    )
    probed: set[int] = set()
    busy: set[int] = {second_http}

    class Writer:
        def close(self) -> None:
            """Соединение подмены закрывать не нужно."""

        async def wait_closed(self) -> None:
            """Соединение подмены уже закрыто."""

    async def connect(address: str, port: int) -> tuple[None, Writer]:
        probed.add(port)
        if port in busy:
            return None, Writer()
        raise ConnectionRefusedError

    monkeypatch.setattr(asyncio, "open_connection", connect)

    async def scenario() -> None:
        driver = LowDriver(
            DEPLOYMENT_ID,
            InProcessBackend(),
            settings(start_timeout=0.2, probe_interval=0.01),
        )
        assert not await driver.wait_until_released(plan), "второй HTTP-порт занят"
        assert probed == {tcp, first_http, second_http}
        busy.clear()
        probed.clear()
        assert await driver.wait_until_released(plan)
        assert probed == {tcp, first_http, second_http}

    asyncio.run(scenario())
