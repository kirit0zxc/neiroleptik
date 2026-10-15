"""Регистрация по OpenAPI без реального центра и без публикации секретов."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from test_executor_client import HttpHarness, validate_schema
from test_executor_client import http as http

from agent.runtime.executor import enroll as cli
from agent.runtime.executor.client import ClientError
from agent.runtime.executor.enroll import EnrollmentClient, EnrollmentReply
from agent.runtime.executor.tokens import TokenFile, TokenFileError

ENROLLMENT = "hf_enroll_synthetic-token-for-tests"
AGENT = "hf_agent_synthetic-token-for-tests"


def test_enroll_matches_openapi_without_bearer(http: HttpHarness) -> None:
    payload = {"host_id": str(uuid4()), "agent_token": AGENT}
    http.replies["/agent/v1/enroll"] = (201, payload)
    reply = asyncio.run(EnrollmentClient(http.url, allow_http=True).enroll(ENROLLMENT, "узел"))
    assert reply.host_id == payload["host_id"] and reply.agent_token == AGENT
    assert AGENT not in repr(reply)
    method, path, headers, body = http.requests[0]
    assert method == "POST" and path == "/agent/v1/enroll"
    assert "Authorization" not in headers
    assert body == {"enrollment_token": ENROLLMENT, "name": "узел", "capabilities": ["low"]}
    validate_schema(body, "EnrollInput")
    validate_schema(payload, "EnrollRead")


def test_enrollment_loopback_bypasses_proxy(
    http: HttpHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    http.replies["/agent/v1/enroll"] = (201, {"host_id": str(uuid4()), "agent_token": AGENT})
    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    asyncio.run(EnrollmentClient(http.url, allow_http=True).enroll(ENROLLMENT, "host"))
    assert len(http.requests) == 1


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_enrollment_never_redirects_secret(http: HttpHarness, status: int) -> None:
    http.replies["/agent/v1/enroll"] = (status, {"detail": ENROLLMENT})
    with pytest.raises(ClientError, match="REDIRECT_REJECTED") as failure:
        asyncio.run(EnrollmentClient(http.url, allow_http=True).enroll(ENROLLMENT, "host"))
    assert failure.value.status == status and len(http.requests) == 1
    assert ENROLLMENT not in str(failure.value)


@pytest.mark.parametrize(
    "value",
    [
        {},
        [],
        {"host_id": "not-uuid", "agent_token": AGENT},
        {"host_id": str(uuid4()), "agent_token": "secret\r\nheader"},
        {"host_id": str(uuid4()), "agent_token": None},
        {"host_id": str(uuid4()), "agent_token": AGENT, "extra": True},
    ],
)
def test_malformed_enrollment_response_is_safe(value: object) -> None:
    with pytest.raises(ClientError, match="INVALID_RESPONSE") as failure:
        EnrollmentReply.parse(value)
    assert "secret" not in str(failure.value) and AGENT not in repr(failure.value)


@pytest.mark.parametrize(
    "token,name",
    [
        ("short", "host"),
        ("x" * 129, "host"),
        (ENROLLMENT + "\n", "host"),
        (ENROLLMENT, ""),
        (ENROLLMENT, "x" * 81),
        (ENROLLMENT, "bad\x00name"),
        (ENROLLMENT, "\ud800"),
    ],
)
def test_invalid_enrollment_input_never_contacts_center(
    http: HttpHarness,
    token: str,
    name: str,
) -> None:
    with pytest.raises(ClientError, match="INVALID_REQUEST"):
        asyncio.run(EnrollmentClient(http.url, allow_http=True).enroll(token, name))
    assert http.requests == []


@pytest.mark.parametrize(
    "url",
    [
        "https://name:secret@example.org",
        "https://example.org/?secret",
        "https://example.org/#",
        "http://remote.example",
        "https://example.org\\evil",
        "https://example.org/\rsecret",
    ],
)
def test_enrollment_reuses_url_validation(url: str) -> None:
    with pytest.raises(ValueError):
        EnrollmentClient(url, allow_http=True)


def args_for(http: HttpHarness, path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        center=http.url,
        name="host",
        token_file=path,
        level=["low"],
        allow_http_loopback=True,
    )


def test_enroll_run_creates_token_file_before_http(http: HttpHarness, tmp_path: Path) -> None:
    target = tmp_path / "agent.token"
    http.replies["/agent/v1/enroll"] = (201, {"host_id": str(uuid4()), "agent_token": AGENT})
    reply = asyncio.run(cli.run(args_for(http, target), ENROLLMENT))
    assert TokenFile(target).read() == reply.agent_token == AGENT


def test_existing_destination_prevents_spending_enrollment(
    http: HttpHarness, tmp_path: Path
) -> None:
    target = tmp_path / "agent.token"
    target.write_text("existing")
    with pytest.raises(TokenFileError, match="TOKEN_FILE_EXISTS"):
        asyncio.run(cli.run(args_for(http, target), ENROLLMENT))
    assert http.requests == [] and target.read_text() == "existing"


def test_failed_enrollment_is_not_retried_and_removes_empty_reservation(
    http: HttpHarness,
    tmp_path: Path,
) -> None:
    target = tmp_path / "agent.token"
    http.replies["/agent/v1/enroll"] = (401, {"detail": ENROLLMENT})
    with pytest.raises(ClientError) as failure:
        asyncio.run(cli.run(args_for(http, target), ENROLLMENT))
    assert failure.value.status == 401 and len(http.requests) == 1
    assert not target.exists() and ENROLLMENT not in str(failure.value)


def test_enrollment_requires_http_201(http: HttpHarness) -> None:
    http.replies["/agent/v1/enroll"] = (200, {"host_id": str(uuid4()), "agent_token": AGENT})
    with pytest.raises(ClientError, match="HTTP_ERROR") as failure:
        asyncio.run(EnrollmentClient(http.url, allow_http=True).enroll(ENROLLMENT, "host"))
    assert failure.value.status == 200


def test_cli_secret_is_not_printed_or_inherited(
    http: HttpHarness,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "agent.token"
    identifier = str(uuid4())
    http.replies["/agent/v1/enroll"] = (201, {"host_id": identifier, "agent_token": AGENT})
    monkeypatch.setenv("HF_ENROLLMENT_TOKEN", ENROLLMENT)
    monkeypatch.setattr(
        "sys.argv",
        [
            "enroll",
            "--center",
            http.url,
            "--name",
            "host",
            "--token-file",
            str(target),
            "--allow-http-loopback",
        ],
    )
    cli.main()
    output = capsys.readouterr()
    assert identifier in output.out and ENROLLMENT not in output.out and AGENT not in output.out
    import os

    assert "HF_ENROLLMENT_TOKEN" not in os.environ
    assert TokenFile(target).read() == AGENT


def test_cli_error_is_safe_and_no_retry(
    http: HttpHarness,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "agent.token"
    http.replies["/agent/v1/enroll"] = (503, {"detail": ENROLLMENT})
    monkeypatch.setenv("HF_ENROLLMENT_TOKEN", ENROLLMENT)
    monkeypatch.setattr(
        "sys.argv",
        [
            "enroll",
            "--center",
            http.url,
            "--name",
            "host",
            "--token-file",
            str(target),
            "--allow-http-loopback",
        ],
    )
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert stopped.value.code == 1 and len(http.requests) == 1
    output = capsys.readouterr()
    assert "HTTP_ERROR" in output.err and "мог быть израсходован" in output.err
    assert ENROLLMENT not in output.err and AGENT not in output.err and not target.exists()
