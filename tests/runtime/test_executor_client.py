"""HTTP команд проверяется на настоящем loopback-сервере и опубликованном OpenAPI."""

from __future__ import annotations

import asyncio
import copy
import json
import select
import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator

from agent.runtime.contract import ContractError
from agent.runtime.executor.client import ClientError, HostRead, HttpCommandClient
from agent.runtime.executor.protocol import ClaimedCommand, LeaseReply

ROOT = Path(__file__).resolve().parents[2]
BEARER = "hf_agent_synthetic-secret-never-log"


def command_fixture() -> dict[str, Any]:
    return json.loads((ROOT / "contracts/fixtures/commands/claim-response.json").read_text())[
        "command"
    ]


def ack_fixture() -> dict[str, Any]:
    return json.loads((ROOT / "contracts/fixtures/commands/ack-success.json").read_text())


def job_fixture(command: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": command["command_id"],
        "deployment_id": command["deployment_id"],
        "command": command["action"],
        "state": "succeeded",
        "error": None,
        "error_code": None,
        "generation": command["generation"],
        "created_at": "2026-10-10T12:00:00Z",
        "completed_at": "2026-10-10T12:00:10Z",
    }


@dataclass
class HttpHarness:
    """Только центр подменён HTTP-ответами; клиент выполняет настоящие HTTP-запросы."""

    url: str = ""
    replies: dict[str, tuple[int, object]] = field(default_factory=dict)
    requests: list[tuple[str, str, dict[str, str], object]] = field(default_factory=list)
    delay: float = 0.0

    def client(self, **options: Any) -> HttpCommandClient:
        return HttpCommandClient(self.url, BEARER, allow_http=True, **options)


@pytest.fixture
def http() -> Iterator[HttpHarness]:
    harness = HttpHarness()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            """Тест не выводит заголовки и Bearer token в консоль."""

        def do_POST(self) -> None:
            self.respond()

        def do_GET(self) -> None:
            self.respond()

        def respond(self) -> None:
            size = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(size)
            request = json.loads(raw) if raw else None
            harness.requests.append((self.command, self.path, dict(self.headers.items()), request))
            if harness.delay:
                time.sleep(harness.delay)
            status, payload = harness.replies.get(self.path, (200, {"command": None}))
            encoded = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            if 300 <= status < 400:
                self.send_header("Location", harness.url + "/stolen-token")
            self.end_headers()
            try:
                self.wfile.write(encoded)
            except (ConnectionError, OSError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    harness.url = f"http://127.0.0.1:{server.server_port}"
    worker = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    worker.start()
    try:
        yield harness
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def validate_schema(value: object, name: str) -> None:
    """Тест опирается на опубликованный контракт, а не на локальную модель агента."""
    document = json.loads((ROOT / "contracts/openapi.json").read_text())
    validator = Draft202012Validator(
        {
            "$ref": "#/components/schemas/" + name,
            "components": document["components"],
        }
    )
    validator.validate(value)


def test_claim_renew_ack_and_config_with_bearer_match_openapi(http: HttpHarness) -> None:
    command = command_fixture()
    command["config"]["credentials"] = [{"username": "demo", "password": "private-value"}]
    claimed_path = "/agent/v1/commands/claim"
    renew_path = f"/agent/v1/commands/{command['command_id']}/renew"
    ack_path = f"/agent/v1/commands/{command['command_id']}/ack"
    lease = {
        name: command[name]
        for name in (
            "command_id",
            "lease_expires_at",
            "execution_expires_at",
        )
    }
    config = {
        "host_id": str(uuid4()),
        "deployments": [
            {
                "deployment_id": command["deployment_id"],
                "profile_id": command["profile_id"],
                "revision": command["revision"],
                "state": "running",
                "generation": 1,
                "applied_revision": 1,
                "instance_id": ack_fixture()["instance_id"],
                "config": command["config"],
            }
        ],
    }
    http.replies = {
        claimed_path: (200, {"command": command}),
        renew_path: (200, lease),
        ack_path: (200, job_fixture(command)),
        "/agent/v1/config": (200, config),
    }

    async def scenario() -> None:
        client = http.client()
        claimed = await client.claim(command["command_id"])
        assert claimed is not None
        assert claimed.deployment().config.services[0].port == 8080
        assert claimed.config.credentials[0].password.reveal() == "private-value"
        assert "private-value" not in repr(claimed)
        assert command["lease_token"] not in repr(claimed)
        renewed = await client.renew(claimed.command_id, claimed.lease_token)
        assert renewed.lease_expires_at.tzinfo is UTC
        result = await client.ack(claimed.command_id, ack_fixture())
        assert result["state"] == "succeeded"
        history = await client.config()
        assert history.deployments[0].applied_revision == 1
        assert not history.rejected

    asyncio.run(scenario())
    assert len(http.requests) == 4
    for method, path, headers, body in http.requests:
        assert headers["Authorization"] == "Bearer " + BEARER
        assert headers["Accept"] == "application/json"
        if path == "/agent/v1/config":
            assert method == "GET" and body is None
        else:
            assert method == "POST"
    validate_schema(http.requests[0][3], "CommandClaim")
    validate_schema(http.requests[1][3], "LeaseInput")
    validate_schema(http.requests[2][3], "CommandAck")
    validate_schema({"command": command}, "CommandClaimRead")
    validate_schema(lease, "LeaseRead")
    validate_schema(job_fixture(command), "JobRead")
    validate_schema(config, "AgentConfigRead")


def test_empty_queue_and_uuid_normalization(http: HttpHarness) -> None:
    identifier = command_fixture()["command_id"].upper()
    assert asyncio.run(http.client().claim(identifier)) is None
    assert http.requests[0][3] == {"command_id": identifier.lower()}


def test_loopback_center_never_goes_through_environment_proxy(
    http: HttpHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Регрессия R1-H4: по HTTP loopback Bearer уходил прокси из HTTP_PROXY."""
    seen: list[tuple[str, bool]] = []

    class Proxy(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            """Заголовки с Bearer не выводятся."""

        def do_POST(self) -> None:
            seen.append((self.path, "Authorization" in self.headers))
            self.send_response(502)
            self.send_header("Content-Length", "0")
            self.end_headers()

    proxy = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
    worker = threading.Thread(target=proxy.serve_forever, kwargs={"poll_interval": 0.01})
    worker.daemon = True
    worker.start()
    try:
        for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
            monkeypatch.setenv(name, f"http://127.0.0.1:{proxy.server_port}")
        for name in ("NO_PROXY", "no_proxy"):
            monkeypatch.delenv(name, raising=False)
        assert asyncio.run(http.client().claim()) is None
        assert seen == []
        assert [request[1] for request in http.requests] == ["/agent/v1/commands/claim"]
    finally:
        proxy.shutdown()
        proxy.server_close()
        worker.join(timeout=2)


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirect_does_not_forward_bearer(http: HttpHarness, status: int) -> None:
    http.replies["/agent/v1/commands/claim"] = (status, {"detail": BEARER})
    with pytest.raises(ClientError) as failed:
        asyncio.run(http.client().claim())
    assert failed.value.code == "REDIRECT_REJECTED" and failed.value.status == status
    assert len(http.requests) == 1 and http.requests[0][1] != "/stolen-token"
    assert BEARER not in str(failed.value)


@pytest.mark.parametrize("code", ["LEASE_EXPIRED", "LEASE_MISMATCH", "COMMAND_FINISHED"])
def test_domain_error_keeps_only_safe_code(http: HttpHarness, code: str) -> None:
    http.replies["/agent/v1/commands/claim"] = (409, {"code": code, "detail": BEARER})
    with pytest.raises(ClientError) as failed:
        asyncio.run(http.client().claim())
    assert failed.value.code == code and failed.value.status == 409
    assert BEARER not in str(failed.value) and BEARER not in repr(failed.value)


def test_untrusted_error_code_is_not_printed(http: HttpHarness) -> None:
    http.replies["/agent/v1/commands/claim"] = (500, {"code": BEARER, "detail": BEARER})
    with pytest.raises(ClientError, match="HTTP_ERROR") as failed:
        asyncio.run(http.client().claim())
    assert BEARER not in str(failed.value)


@pytest.mark.parametrize("status,code", [(200, "RESPONSE_TOO_LARGE"), (500, "HTTP_ERROR")])
def test_response_limit_is_enforced(http: HttpHarness, status: int, code: str) -> None:
    http.replies["/agent/v1/commands/claim"] = (status, b"x" * 65)
    with pytest.raises(ClientError, match=code) as failure:
        asyncio.run(http.client(max_response_bytes=64).claim())
    assert failure.value.status == (500 if status == 500 else None)


def host_fixture() -> dict[str, object]:
    return {
        "id": str(uuid4()),
        "name": "Узел Low",
        "capabilities": ["low"],
        "last_seen": "2026-10-10T12:00:10Z",
        "status": "online",
        "created_at": "2026-10-10T12:00:00+00:00",
    }


def test_heartbeat_uses_bearer_and_existing_host_read(http: HttpHarness) -> None:
    host = host_fixture()
    http.replies["/agent/v1/heartbeat"] = (200, host)
    result = asyncio.run(http.client().heartbeat())
    assert result.id == host["id"] and result.capabilities == ("low",)
    assert result.last_seen is not None and result.last_seen.tzinfo is UTC
    assert http.requests[0][3] is None
    assert http.requests[0][2]["Authorization"] == "Bearer " + BEARER
    validate_schema(host, "HostRead")


@pytest.mark.parametrize(
    "patch",
    [
        {"id": 42},
        {"id": "bad-uuid"},
        {"extra": True},
        {"name": None},
        {"name": "bad\x00name"},
        {"name": "\ud800"},
        {"capabilities": "low"},
        {"capabilities": ["unknown"]},
        {"capabilities": [None]},
        {"status": "unknown"},
        {"created_at": "2026-10-10"},
        {"last_seen": "2026-10-10T12:00:10"},
        {"last_seen": False},
    ],
)
def test_heartbeat_rejects_malformed_host_read(http: HttpHarness, patch: dict[str, object]) -> None:
    http.replies["/agent/v1/heartbeat"] = (200, {**host_fixture(), **patch})
    with pytest.raises(ClientError, match="INVALID_RESPONSE"):
        asyncio.run(http.client().heartbeat())


def test_host_nullable_timestamp_and_all_published_statuses() -> None:
    for status in ("online", "offline", "revoked"):
        result = HostRead.parse({**host_fixture(), "status": status, "last_seen": None})
        assert result.status == status and result.last_seen is None
    with pytest.raises(ClientError, match="INVALID_RESPONSE"):
        HostRead.parse({key: value for key, value in host_fixture().items() if key != "last_seen"})


def test_timeout_returns_safe_network_error(http: HttpHarness) -> None:
    http.delay = 0.2
    with pytest.raises(ClientError, match="NETWORK_ERROR"):
        asyncio.run(http.client(timeout=0.03).claim())


@pytest.mark.parametrize("body", [b"not-json-private-secret", [], {}, {"command": None, "x": 1}])
def test_bad_response_is_safe(http: HttpHarness, body: object) -> None:
    http.replies["/agent/v1/commands/claim"] = (200, body)
    with pytest.raises(ClientError, match="INVALID_RESPONSE") as failed:
        asyncio.run(http.client().claim())
    assert "private-secret" not in str(failed.value)


@pytest.mark.parametrize("status,code", [(200, "INVALID_RESPONSE"), (500, "HTTP_ERROR")])
def test_deeply_nested_json_returns_safe_error(http: HttpHarness, status: int, code: str) -> None:
    """Лимит байтов не исключает переполнение глубины декодера JSON."""
    http.replies["/agent/v1/commands/claim"] = (status, b"[" * 10_000 + b"]" * 10_000)
    with pytest.raises(ClientError) as failed:
        asyncio.run(http.client().claim())
    assert failed.value.code == code
    assert failed.value.status == (500 if status == 500 else None)
    assert BEARER not in str(failed.value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("generation", True),
        ("expected_generation", True),
        ("generation", 7),
        ("revision", 0),
        ("reconcile", "false"),
        ("expected_instance_id", "bad-secret-value"),
        ("command_id", "bad-secret-value"),
        ("action", "shell"),
        ("lease_token", "bad-secret-value"),
        ("lease_expires_at", "2026-10-10T12:00:00"),
        ("lease_expires_at", "2026-10-10T12:06:00Z"),
    ],
)
def test_command_validation_rejects_drift_without_leaking(field: str, value: object) -> None:
    command = command_fixture()
    command[field] = value
    with pytest.raises(ContractError) as failed:
        ClaimedCommand.parse(command)
    assert "bad-secret-value" not in str(failed.value)


def test_fingerprint_excludes_renewable_lease_but_includes_config_and_hard_deadline() -> None:
    initial = command_fixture()
    claimed = ClaimedCommand.parse(initial)
    redelivery = copy.deepcopy(initial)
    redelivery["lease_token"] = "hf_lease_" + "1" * 64
    redelivery["lease_expires_at"] = "2026-10-10T12:02:00Z"
    assert claimed.fingerprint == ClaimedCommand.parse(redelivery).fingerprint
    changed_deadline = copy.deepcopy(redelivery)
    changed_deadline["execution_expires_at"] = "2026-10-10T12:09:00Z"
    assert claimed.fingerprint != ClaimedCommand.parse(changed_deadline).fingerprint
    same_deadline = copy.deepcopy(redelivery)
    same_deadline["execution_expires_at"] = datetime.fromisoformat(
        initial["execution_expires_at"].replace("Z", "+00:00")
    ).isoformat()
    assert claimed.fingerprint == ClaimedCommand.parse(same_deadline).fingerprint
    redelivery["config"]["services"][0]["banner"] = "changed"
    assert claimed.fingerprint != ClaimedCommand.parse(redelivery).fingerprint
    assert "config" not in claimed.identity() and "lease_token" not in claimed.identity()
    assert len(claimed.identity()["config_sha256"]) == 64


def test_timezone_offset_is_normalized_to_utc() -> None:
    command = command_fixture()
    command["lease_expires_at"] = "2026-10-10T15:01:00+03:00"
    assert ClaimedCommand.parse(command).lease_expires_at.hour == 12


@pytest.mark.parametrize("closed", [False, True])
def test_http_error_with_broken_body_never_exposes_network_detail(monkeypatch, closed) -> None:
    from http.client import HTTPException
    from unittest.mock import Mock
    from urllib.error import HTTPError

    client = HttpCommandClient("https://center.test", "test-only-token")
    body = Mock()
    body.closed = closed
    body.read1.side_effect = HTTPException("PRIVATE_NETWORK_DETAIL")

    def fail(*args, **kwargs):
        raise HTTPError("https://center.test", 500, "failure", {}, body)

    monkeypatch.setattr(client._opener, "open", fail)
    with pytest.raises(ClientError) as failed:
        client._request("POST", "/agent/v1/commands/claim", {})
    assert failed.value.code == "NETWORK_ERROR"
    assert "PRIVATE_NETWORK_DETAIL" not in str(failed.value)


def test_renew_and_ack_wrong_command_id_are_rejected(http: HttpHarness) -> None:
    command = command_fixture()
    wrong_id = str(uuid4())
    http.replies[f"/agent/v1/commands/{command['command_id']}/renew"] = (
        200,
        {
            "command_id": wrong_id,
            "lease_expires_at": command["lease_expires_at"],
            "execution_expires_at": command["execution_expires_at"],
        },
    )
    http.replies[f"/agent/v1/commands/{command['command_id']}/ack"] = (
        200,
        {**job_fixture(command), "job_id": wrong_id},
    )
    with pytest.raises(ClientError, match="INVALID_RESPONSE"):
        asyncio.run(http.client().renew(command["command_id"], command["lease_token"]))
    with pytest.raises(ClientError, match="INVALID_RESPONSE"):
        asyncio.run(http.client().ack(command["command_id"], ack_fixture()))


@pytest.mark.parametrize(
    "patch",
    [
        {"state": "requested"},
        {"state": "running"},
        {"state": "expired"},
        {"state": "failed"},
        {"deployment_id": str(uuid4())},
        {"generation": 2},
        {"completed_at": None},
        {"error": "private-response-secret"},
        {"error_code": "PRIVATE_RESPONSE_SECRET"},
    ],
)
def test_ack_200_must_confirm_exact_terminal_result(
    http: HttpHarness, patch: dict[str, object]
) -> None:
    command = command_fixture()
    http.replies[f"/agent/v1/commands/{command['command_id']}/ack"] = (
        200,
        {**job_fixture(command), **patch},
    )
    with pytest.raises(ClientError, match="INVALID_RESPONSE") as failed:
        asyncio.run(http.client().ack(command["command_id"], ack_fixture()))
    assert "private-response-secret" not in str(failed.value)


def test_failed_ack_confirms_same_error(http: HttpHarness) -> None:
    command = command_fixture()
    acknowledgement = {
        **ack_fixture(),
        "outcome": "failed",
        "runtime_state": "failed",
        "error": {"code": "START_FAILED", "message": "Ошибка запуска"},
    }
    job = {
        **job_fixture(command),
        "state": "failed",
        "error_code": "START_FAILED",
        "error": "Ошибка запуска",
    }
    path = f"/agent/v1/commands/{command['command_id']}/ack"
    http.replies[path] = (200, job)
    assert (
        asyncio.run(http.client().ack(command["command_id"], acknowledgement))["state"] == "failed"
    )
    http.replies[path] = (200, {**job, "error": "Чужая ошибка"})
    with pytest.raises(ClientError, match="INVALID_RESPONSE"):
        asyncio.run(http.client().ack(command["command_id"], acknowledgement))


def test_lease_reply_rejects_extension_past_execution_deadline() -> None:
    command = command_fixture()
    with pytest.raises(ContractError):
        LeaseReply.parse(
            {
                "command_id": command["command_id"],
                "lease_expires_at": "2026-10-10T12:06:00Z",
                "execution_expires_at": command["execution_expires_at"],
            }
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1",
        "http://remote.example",
        "https://name:password@example.org",
        "https://example.org/?secret=bad",
        "https://example.org/#secret",
        "https://example.org/?",
        "https://example.org/#",
        "https://example.org\\evil",
        "https://example.org/\rsecret",
        "https://example.org:bad",
        "file:///private",
    ],
)
def test_base_url_rejects_unsafe_forms(url: str) -> None:
    with pytest.raises(ValueError):
        HttpCommandClient(url, BEARER)


@pytest.mark.parametrize("url", ["http://remote.example", "http://192.0.2.1"])
def test_explicit_http_still_requires_loopback(url: str) -> None:
    with pytest.raises(ValueError):
        HttpCommandClient(url, BEARER, allow_http=True)


@pytest.mark.parametrize("timeout", [True, 0, -1, float("nan"), float("inf"), 10**1000])
def test_timeout_validation(timeout: object) -> None:
    with pytest.raises(ValueError):
        HttpCommandClient("https://example.org", BEARER, timeout=timeout)  # type: ignore[arg-type]


@pytest.mark.parametrize("limit", [True, 0, -1, 1_048_577])
def test_response_limit_validation(limit: object) -> None:
    with pytest.raises(ValueError):
        HttpCommandClient("https://example.org", BEARER, max_response_bytes=limit)  # type: ignore[arg-type]


# --- Регрессия P2: медленные заголовки не оставляют работающий сетевой поток -----------------


class SlowHeaders:
    """Loopback-«центр»: принимает запрос и присылает заголовки по байту, не заканчивая их.

    Фиксирует момент, когда клиент разорвал соединение. До исправления поток urllib
    продолжал читать после таймаута asyncio, пока сервер не переставал слать байты.
    """

    def __init__(self, drip_seconds: float = 6.0) -> None:
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.closed_at: float | None = None
        self.closed = threading.Event()
        self._drip_seconds = drip_seconds
        self._worker = threading.Thread(target=self._serve, daemon=True)
        self._worker.start()

    def _serve(self) -> None:
        connection, _ = self.listener.accept()
        with connection:
            received = b""
            while b"\r\n\r\n" not in received:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                received += chunk
            finish = time.monotonic() + self._drip_seconds
            while time.monotonic() < finish:
                try:
                    connection.sendall(b"X")
                    readable, _, _ = select.select([connection], [], [], 0.05)
                    if readable and not connection.recv(1):
                        break
                except OSError:
                    break
            else:
                return
            self.closed_at = time.monotonic()
            self.closed.set()

    def close(self) -> None:
        self.listener.close()


def test_slow_headers_do_not_keep_the_network_thread_after_timeout() -> None:
    """Регрессия P2: по общему сроку запрос разрывает соединение, а не только перестаёт ждать."""
    server = SlowHeaders()
    try:
        client = HttpCommandClient(f"http://127.0.0.1:{server.port}", BEARER, allow_http=True)
        client._timeout = 0.5
        started = time.monotonic()
        with pytest.raises(ClientError) as failed:
            asyncio.run(client.heartbeat())
        assert failed.value.code == "NETWORK_ERROR"
        assert server.closed.wait(3.0), "клиент не разорвал соединение после срока"
        assert server.closed_at is not None and server.closed_at - started < 2.0
    finally:
        server.close()


def test_http_requests_never_occupy_the_default_asyncio_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Сеть выполняется в отдельном ограниченном пуле: общий пул остаётся Docker CLI."""
    seen: list[str] = []

    def request(self: HttpCommandClient, *args: object) -> object:
        seen.append(threading.current_thread().name)
        return {"command": None}

    monkeypatch.setattr(HttpCommandClient, "_request", request)
    client = HttpCommandClient("https://center.test", BEARER)
    assert asyncio.run(client.claim()) is None
    assert seen and seen[0].startswith("honeyforge-http")
