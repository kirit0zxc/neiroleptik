"""Потеря ответа и недоверенный ACK не должны удалять доказательства из spool."""

import http.client
import json
import socket
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import SecretStr

from agent.telemetry.adapters.low import SourceRecord
from agent.telemetry.deliver import main
from agent.telemetry.schemas import Ack, Batch, Event, RejectedEvent
from agent.telemetry.sender import (
    MAX_ACK_BYTES,
    HttpBatchTransport,
    HttpResponse,
    ResponseTooLarge,
    TelemetrySender,
    TransportError,
)
from agent.telemetry.spool import SpoolRepository

AGENT = "00000000-0000-4000-8000-000000000001"
TOKEN = "hf_agent_test-only-token"


@pytest.fixture
def spool(tmp_path):
    repository = SpoolRepository(tmp_path / "sender.sqlite3", AGENT)
    yield repository
    repository.close()


def seed(spool, count=1):
    """Синтетическая фикстура не подменяет проверку фактического журнала Low."""
    fixture = Path(__file__).resolve().parents[2] / "contracts/fixtures/telemetry/ws-event.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))["event"]
    payload.pop("received_at")
    events = []
    for index in range(count):
        event = Event.model_validate({**payload, "event_id": str(uuid4()), "source_seq": index})
        offset = spool.checkpoint("test-source")
        spool.append(SourceRecord("test-source", offset, offset + 1, event))
        events.append(event)
    return events


def accepted_response(payload):
    batch = Batch.model_validate_json(payload)
    return HttpResponse(
        200,
        Ack(
            batch_id=batch.batch_id,
            accepted_ids=[event.event_id for event in batch.events],
            rejected=[],
        )
        .model_dump_json()
        .encode(),
    )


class ScriptTransport:
    """Ответы сценария известны заранее; сохраняем реальные отправленные байты."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.calls = []

    def post(self, payload):
        self.calls.append(payload)
        action = self.steps.pop(0)
        if isinstance(action, Exception):
            raise action
        return action(payload) if callable(action) else action


class Clock:
    """Управляемое время исключает реальные задержки в проверках backoff."""

    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


def sender_for(spool, transport, clock):
    return TelemetrySender(
        spool, transport, clock=clock, wall_clock=lambda: 1000.0, jitter=lambda low, high: high
    )


def test_lost_ack_retries_exact_bytes_after_deadline(spool):
    events = seed(spool, 3)
    transport = ScriptTransport(TransportError(), accepted_response)
    clock = Clock()
    sender = sender_for(spool, transport, clock)
    assert sender.step().retry_in == 1
    assert spool.count() == 3
    assert sender.step().state == "waiting"
    assert len(transport.calls) == 1
    clock.value = 1
    result = sender.step()
    assert (result.state, result.accepted) == ("delivered", 3)
    assert transport.calls[0] == transport.calls[1]
    assert Batch.model_validate_json(transport.calls[0]).events == events
    assert spool.count() == 0
    assert spool.checkpoint("test-source") == 3
    assert sender.step().state == "idle"


@pytest.mark.parametrize("status", [301, 307, 400, 401, 403, 404, 409, 422])
def test_http_rejection_pauses_until_explicit_resume(spool, status):
    seed(spool)
    transport = ScriptTransport(HttpResponse(status, b"secret error"), accepted_response)
    sender = sender_for(spool, transport, Clock())
    result = sender.step()
    assert result.state == "paused"
    assert result.http_status == status
    assert "secret" not in repr(result)
    assert sender.step() == result
    assert len(transport.calls) == 1
    assert spool.count() == 1
    sender.resume()
    assert sender.step().state == "delivered"
    assert transport.calls[0] == transport.calls[1]


@pytest.mark.parametrize(
    ("status", "header", "delay"),
    [
        (429, "20", 20),
        (503, format_datetime(datetime.fromtimestamp(1030, UTC), usegmt=True), 30),
        (500, "20", 1),
        (503, "-20", 1),
        (429, "999999999", 86400),
        (503, "invalid", 1),
    ],
)
def test_server_retry_after_is_respected(spool, status, header, delay):
    seed(spool)
    transport = ScriptTransport(HttpResponse(status, b"", header), accepted_response)
    clock = Clock()
    sender = sender_for(spool, transport, clock)
    assert sender.step().retry_in == delay
    clock.value = delay - 0.01
    assert sender.step().state == "waiting"
    clock.value = delay
    assert sender.step().state == "delivered"


def test_backoff_is_capped_and_resets_after_success(spool):
    seed(spool)
    transport = ScriptTransport(*[HttpResponse(503, b"")] * 9, accepted_response)
    clock = Clock()
    sender = sender_for(spool, transport, clock)
    for expected in [1, 2, 4, 8, 16, 32, 60, 60, 60]:
        result = sender.step()
        assert result.retry_in == expected
        clock.value += expected
    assert sender.step().state == "delivered"
    seed(spool)
    transport.steps.append(TransportError())
    assert sender.step().retry_in == 1


@pytest.mark.parametrize(
    "damage",
    ["partial", "wrong_batch", "foreign_id", "duplicate_key", "nan", "oversized", "unicode"],
)
def test_invalid_ack_never_changes_spool(spool, damage):
    seed(spool, 2)

    def broken(payload):
        response = accepted_response(payload)
        ack = json.loads(response.body)
        if damage == "partial":
            ack["accepted_ids"].pop()
        elif damage == "wrong_batch":
            ack["batch_id"] = str(uuid4())
        elif damage == "foreign_id":
            ack["accepted_ids"][0] = str(uuid4())
        elif damage == "duplicate_key":
            return HttpResponse(200, response.body[:-1] + b',"rejected":[]}')
        elif damage == "nan":
            return HttpResponse(200, response.body[:-1] + b',"extra":NaN}')
        elif damage == "oversized":
            return HttpResponse(200, b" " * (MAX_ACK_BYTES + 1))
        elif damage == "unicode":
            return HttpResponse(200, b"\xff")
        return HttpResponse(200, json.dumps(ack).encode())

    clock = Clock()
    transport = ScriptTransport(broken, accepted_response)
    sender = sender_for(spool, transport, clock)
    assert sender.step().error_code == "INVALID_ACK"
    assert (spool.count(), spool.quarantine_count(), spool.checkpoint("test-source")) == (2, 0, 2)
    clock.value = 1
    assert sender.step().accepted == 2
    assert transport.calls[0] == transport.calls[1]


def test_mixed_ack_retries_only_pending_after_backoff(spool):
    events = seed(spool, 3)

    def mixed(payload):
        batch = Batch.model_validate_json(payload)
        ack = Ack(
            batch_id=batch.batch_id,
            accepted_ids=[events[0].event_id],
            rejected=[
                RejectedEvent(index=1, event_id=None, code="INVALID_EVENT", retryable=False),
                RejectedEvent(
                    index=2,
                    event_id=events[2].event_id,
                    code="INSTANCE_NOT_REGISTERED",
                    retryable=True,
                ),
            ],
        )
        return HttpResponse(200, ack.model_dump_json().encode())

    clock = Clock()
    transport = ScriptTransport(mixed, accepted_response)
    sender = sender_for(spool, transport, clock)
    result = sender.step()
    assert (result.accepted, result.quarantined, result.error_code) == (1, 1, "EVENTS_RETRYABLE")
    assert spool.count() == 1
    assert spool.list_quarantine()[0].event == events[1]
    clock.value = 1
    assert sender.step().accepted == 1
    assert Batch.model_validate_json(transport.calls[1]).events == [events[2]]


def test_restart_keeps_event_identity_and_payload(tmp_path):
    path = tmp_path / "restart.sqlite3"
    spool = SpoolRepository(path, AGENT)
    seed(spool, 2)
    first = ScriptTransport(TransportError())
    sender_for(spool, first, Clock()).step()
    spool.close()
    spool = SpoolRepository(path, AGENT)
    try:
        second = ScriptTransport(accepted_response)
        assert sender_for(spool, second, Clock()).step().accepted == 2
        assert (
            Batch.model_validate_json(first.calls[0]).events
            == Batch.model_validate_json(second.calls[0]).events
        )
    finally:
        spool.close()


@contextmanager
def http_server(reply):
    """Настоящий loopback HTTP: проверяем заголовки, обрыв ответа и redirect."""
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = self.rfile.read(int(self.headers["Content-Length"]))
            requests.append((self.path, dict(self.headers), payload))
            reply(self, payload, len(requests))

        def log_message(self, *_args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/agent/v1/events", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_real_http_response_loss_then_ack(spool):
    seed(spool)
    observed_events = set()

    def reply(handler, payload, attempt):
        observed_events.update(
            event.event_id for event in Batch.model_validate_json(payload).events
        )
        if attempt == 1:
            handler.connection.shutdown(socket.SHUT_RDWR)
            return
        body = accepted_response(payload).body
        handler.send_response(200)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    with http_server(reply) as (url, requests):
        clock = Clock()
        sender = sender_for(spool, HttpBatchTransport(url, SecretStr(TOKEN)), clock)
        assert sender.step().error_code == "TRANSPORT_ERROR"
        assert spool.count() == 1
        clock.value = 1
        assert sender.step().accepted == 1
        assert requests[0][2] == requests[1][2]
        assert requests[0][1]["Authorization"] == "Bearer " + TOKEN
        assert requests[0][1]["Content-Type"] == "application/json"
    assert len(observed_events) == 1


def test_real_http_redirect_does_not_send_credentials_elsewhere():
    def redirect(handler, payload, attempt):
        handler.send_response(307)
        handler.send_header("Location", "/other")
        handler.end_headers()

    with http_server(redirect) as (url, requests):
        response = HttpBatchTransport(url, SecretStr(TOKEN)).post(b"{}")
        assert response.status == 307
        assert len(requests) == 1


def test_real_http_ack_size_is_bounded():
    def oversized(handler, payload, attempt):
        handler.send_response(200)
        handler.send_header("Content-Length", str(MAX_ACK_BYTES + 1))
        handler.end_headers()
        handler.wfile.write(b" " * (MAX_ACK_BYTES + 1))

    with http_server(oversized) as (url, _):
        with pytest.raises(ResponseTooLarge):
            HttpBatchTransport(url, SecretStr(TOKEN)).post(b"{}")


def test_truncated_http_body_cannot_ack_even_if_json_is_complete(spool):
    seed(spool)

    def truncated(handler, payload, attempt):
        body = accepted_response(payload).body
        handler.send_response(200)
        handler.send_header("Content-Length", str(len(body) + 1))
        handler.end_headers()
        handler.wfile.write(body)

    with http_server(truncated) as (url, _):
        sender = sender_for(spool, HttpBatchTransport(url, SecretStr(TOKEN)), Clock())
        assert sender.step().error_code == "TRANSPORT_ERROR"
        assert spool.count() == 1


def test_cli_delivers_existing_spool_and_does_not_log_secrets(tmp_path, monkeypatch, capsys):
    path = tmp_path / "cli.sqlite3"
    spool = SpoolRepository(path, AGENT)
    events = seed(spool)
    spool.close()

    def reply(handler, payload, attempt):
        body = accepted_response(payload).body
        handler.send_response(200)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    monkeypatch.setenv("HF_AGENT_TOKEN", TOKEN)
    with http_server(reply) as (url, _):
        assert main(["--spool", str(path), "--agent-id", AGENT, "--endpoint", url, "--once"]) == 0
    output = capsys.readouterr().out
    assert json.loads(output)["accepted"] == 1
    assert TOKEN not in output
    assert str(events[0].event_id) not in output
    spool = SpoolRepository(path, AGENT)
    try:
        assert spool.count() == 0
    finally:
        spool.close()


def test_cli_missing_token_does_not_create_spool(tmp_path, monkeypatch):
    path = tmp_path / "missing.sqlite3"
    monkeypatch.delenv("HF_AGENT_TOKEN", raising=False)
    with pytest.raises(SystemExit) as error:
        main(["--spool", str(path), "--agent-id", AGENT, "--endpoint", "http://localhost/events"])
    assert error.value.code == 2
    assert not path.exists()


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/events",
        "https://user:secret@example.com/events",
        "https://example.com/events?token=secret",
        "https://example.com/events#fragment",
        "file:///tmp/events",
        "https://example.com/\nevents",
    ],
)
def test_invalid_endpoint_is_rejected_before_network(url):
    with pytest.raises(ValueError):
        HttpBatchTransport(url, SecretStr(TOKEN))


@pytest.mark.parametrize(
    ("url", "expected_host", "expected_port", "connection_type"),
    [
        ("http://[::1]/events", "::1", 80, http.client.HTTPConnection),
        ("https://[2001:db8::123]/events", "2001:db8::123", 443, http.client.HTTPSConnection),
        ("http://[::1]:8080/events", "::1", 8080, http.client.HTTPConnection),
        ("https://[2001:db8::123]:8443/events", "2001:db8::123", 8443, http.client.HTTPSConnection),
        ("http://127.0.0.1/events", "127.0.0.1", 80, http.client.HTTPConnection),
        ("https://192.0.2.1/events", "192.0.2.1", 443, http.client.HTTPSConnection),
        ("http://localhost/events", "localhost", 80, http.client.HTTPConnection),
        ("https://center.example/events", "center.example", 443, http.client.HTTPSConnection),
        ("http://127.0.0.1:8080/events", "127.0.0.1", 8080, http.client.HTTPConnection),
        ("https://192.0.2.1:8443/events", "192.0.2.1", 8443, http.client.HTTPSConnection),
        ("http://localhost:8080/events", "localhost", 8080, http.client.HTTPConnection),
        ("https://center.example:8443/events", "center.example", 8443, http.client.HTTPSConnection),
        ("http://[::1]:0/events", "::1", 0, http.client.HTTPConnection),
    ],
)
def test_endpoint_creates_connection_with_correct_host_and_port(
    monkeypatch, url, expected_host, expected_port, connection_type
):
    """Настоящий конструктор разбирает адрес; подключение подменено до DNS и сети."""
    connections = []

    def capture_connection(connection):
        connections.append(connection)
        raise OSError("Подключение подменено тестом")

    monkeypatch.setattr(http.client.HTTPConnection, "connect", capture_connection)
    with pytest.raises(TransportError):
        HttpBatchTransport(url, SecretStr(TOKEN)).post(b"{}")

    assert len(connections) == 1
    connection = connections[0]
    assert type(connection) is connection_type
    assert (connection.host, connection.port) == (expected_host, expected_port)
