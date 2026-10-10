"""Настоящие медленные соединения не удерживают доставку после общего срока."""

import select
import socket
import threading
import time

import pytest
from pydantic import SecretStr
from test_sender import AGENT, ScriptTransport, accepted_response, seed

from agent.telemetry.sender import HttpBatchTransport, TelemetrySender, TransportError
from agent.telemetry.spool import SpoolRepository


class SlowResponse:
    """Тестовый центр держит поток до закрытия клиента, а не до собственного таймаута."""

    def __init__(self, phase):
        self.phase = phase
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.listener.settimeout(2)
        self.port = self.listener.getsockname()[1]
        self.closed = threading.Event()
        self.stop = threading.Event()
        self.worker = threading.Thread(target=self.serve)
        self.worker.start()

    def serve(self):
        try:
            connection, _ = self.listener.accept()
            with connection:
                connection.settimeout(2)
                if self.phase == "tls":
                    connection.recv(8192)  # ClientHello; TLS-сервер намеренно не отвечает.
                else:
                    request = b""
                    while b"\r\n\r\n" not in request:
                        chunk = connection.recv(8192)
                        if not chunk:
                            self.closed.set()
                            return
                        request += chunk
                    headers = {
                        "headers": b"HTTP/1.1 200 OK\r\nX-Slow: ",
                        "body": (
                            b"HTTP/1.1 200 OK\r\nContent-Length: 4096\r\nConnection: close\r\n\r\n"
                        ),
                        "chunked": (
                            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n"
                            b"Connection: close\r\n\r\n1;ext="
                        ),
                    }
                    connection.sendall(headers[self.phase])
                finish = time.monotonic() + 3
                while not self.stop.is_set() and time.monotonic() < finish:
                    if self.phase != "tls":
                        connection.sendall(b"x")
                    readable, _, _ = select.select([connection], [], [], 0.01)
                    if readable and not connection.recv(8192):
                        self.closed.set()
                        return
        except OSError:
            self.closed.set()

    def close(self):
        self.stop.set()
        self.listener.close()
        self.worker.join(timeout=3)
        assert not self.worker.is_alive()


@pytest.mark.parametrize("phase", ["headers", "body", "chunked", "tls"])
def test_deadline_interrupts_socket_in_every_response_phase(phase):
    server = SlowResponse(phase)
    scheme = "https" if phase == "tls" else "http"
    transport = HttpBatchTransport(
        f"{scheme}://127.0.0.1:{server.port}/events",
        SecretStr("hf_agent_test-only-token"),
        timeout=0.2,
    )
    try:
        started = time.monotonic()
        with pytest.raises(TransportError):
            transport.post(b"{}")
        assert time.monotonic() - started < 1.5
        assert server.closed.wait(1), "таймаут обязан разорвать реальное соединение"
    finally:
        server.close()


def test_real_timeout_keeps_spool_and_next_attempt_can_deliver(tmp_path):
    server = SlowResponse("chunked")
    spool = SpoolRepository(tmp_path / "deadline.sqlite3", AGENT)
    try:
        seed(spool)
        transport = HttpBatchTransport(
            f"http://127.0.0.1:{server.port}/events",
            SecretStr("hf_agent_test-only-token"),
            timeout=0.2,
        )
        sender = TelemetrySender(spool, transport)
        assert sender.step().state == "retry"
        assert spool.count() == 1
        assert server.closed.wait(1)
        # Новый цикл после сбоя читает те же сохранённые события; удаление — только по ACK.
        healthy = TelemetrySender(spool, ScriptTransport(accepted_response))
        assert healthy.step().state == "delivered"
        assert spool.count() == 0
    finally:
        spool.close()
        server.close()
