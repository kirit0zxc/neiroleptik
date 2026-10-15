"""Политика повторов и heartbeat проверяются независимо от долгой команды."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.runtime.executor import __main__ as cli
from agent.runtime.executor.client import ClientError
from agent.runtime.executor.tokens import TokenFile


@pytest.mark.parametrize(
    "code,status,expected",
    [
        ("NETWORK_ERROR", None, True),
        ("HTTP_ERROR", None, False),
        ("INVALID_RESPONSE", None, False),
        ("INVALID_REQUEST", 503, True),
        ("HTTP_ERROR", 500, True),
        ("NETWORK_ERROR", 400, False),
        ("LEASE_EXPIRED", 409, True),
        ("LEASE_MISMATCH", 409, True),
        ("LEASE_EXPIRED", 403, False),
        ("HOST_BUSY", 409, False),
        ("HTTP_ERROR", 404, False),
        ("HTTP_ERROR", 429, False),
        ("RESPONSE_TOO_LARGE", None, False),
    ],
)
def test_retry_policy_uses_http_status_before_remote_code(
    code: str, status: int | None, expected: bool
) -> None:
    assert cli._retryable(ClientError(code, status)) is expected


@pytest.mark.parametrize("heartbeat_failure", [False, True])
def test_heartbeat_runs_during_command_and_survives_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, heartbeat_failure: bool
) -> None:
    class Client:
        """Подмена сетевого клиента считает только heartbeat, без команды центра."""

        beats = 0

        async def heartbeat(self) -> SimpleNamespace:
            self.beats += 1
            if heartbeat_failure and self.beats == 1:
                raise ClientError("HTTP_ERROR", 401)
            return SimpleNamespace(capabilities=("medium",) if heartbeat_failure else ("low",))

    client = Client()

    class SlowExecutor:
        """Команда ждёт, позволяя проверить несколько независимых heartbeat."""

        def __init__(self, *args, **kwargs) -> None:
            pass

        async def run_once(self) -> bool:
            await asyncio.sleep(0.08)
            assert client.beats >= 3
            return True

    monkeypatch.setattr(cli, "HttpCommandClient", lambda *a, **kw: client)
    monkeypatch.setattr(cli, "CommandExecutor", SlowExecutor)
    monkeypatch.setattr(cli, "DockerCli", object)
    monkeypatch.setattr(cli, "_HEARTBEAT_INTERVAL", 0.01)
    args = argparse.Namespace(
        center="https://center.test",
        allow_http_loopback=False,
        image="honeyforge-low:local",
        bind_address="127.0.0.1",
        network=None,
        journal_root=None,
        medium_state_root=None,
        medium_image="honeyforge-medium:local",
        medium_user="1000:1000",
        state=tmp_path / "private" / "state.sqlite",
        once=True,
    )
    asyncio.run(cli._poll(args, "synthetic-token"))
    if heartbeat_failure:
        output = capsys.readouterr().out
        assert "HTTP_ERROR" in output
        assert "отличаются от настроенных [low]" in output


def test_cli_reads_private_token_file_without_inheritance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "token"
    token = "hf_agent_" + "a" * 64
    with TokenFile(path).reserve() as reservation:
        reservation.write(token)
    seen = []

    async def fake_run(args: argparse.Namespace, supplied: str) -> None:
        seen.append(supplied)

    monkeypatch.delenv("HF_AGENT_TOKEN", raising=False)
    monkeypatch.setattr(cli, "run", fake_run)
    monkeypatch.setattr(
        "sys.argv",
        [
            "executor",
            "--center",
            "https://center.test",
            "--token-file",
            str(path),
            "--state",
            str(tmp_path / "state"),
            "--journal-root",
            str(tmp_path / "journal"),
        ],
    )
    cli.main()
    assert seen == [token]


def test_cli_rejects_two_token_sources_without_displaying_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    secret = "hf_agent_" + "b" * 64
    monkeypatch.setenv("HF_AGENT_TOKEN", secret)
    monkeypatch.setattr(
        "sys.argv",
        [
            "executor",
            "--center",
            "https://center.test",
            "--token-file",
            str(tmp_path / "token"),
            "--state",
            str(tmp_path / "state"),
            "--journal-root",
            str(tmp_path / "journal"),
        ],
    )
    with pytest.raises(SystemExit) as failure:
        cli.main()
    assert failure.value.code == 1
    output = capsys.readouterr().err
    assert "LOCAL_CONFIGURATION_ERROR" in output and secret not in output
    import os

    assert "HF_AGENT_TOKEN" not in os.environ
