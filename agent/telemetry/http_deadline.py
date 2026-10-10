"""Общий срок TCP/TLS/HTTP с владением сокетом до конца чтения ответа."""

import contextlib
import socket
import threading
import time
from types import TracebackType


class RequestDeadline:
    """Таймер прерывает I/O через дубликат сокета, даже после передачи его TLS/response.

    Системный getaddrinfo синхронный и не отменяется переносимым способом. После
    его возврата срок проверяется до подключения; сетевые worker-потоки не создаются.
    """

    def __init__(self, seconds: float) -> None:
        self.until = time.monotonic() + seconds
        self._lock = threading.Lock()
        self._socket: socket.socket | None = None
        self._expired = False
        self._timer = threading.Timer(seconds, self._expire)
        self._timer.daemon = True

    def remaining(self) -> float:
        remaining = self.until - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Истёк срок HTTP-запроса")
        return remaining

    def __enter__(self) -> "RequestDeadline":
        self._timer.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._timer.cancel()
        self._timer.join()
        self._release()

    def _release(self) -> None:
        with self._lock:
            if self._socket is not None:
                self._socket.close()
                self._socket = None

    def _expire(self) -> None:
        with self._lock:
            self._expired = True
            if self._socket is not None:
                # shutdown действует и на оригинальный дескриптор, включая SSL.
                with contextlib.suppress(OSError):
                    self._socket.shutdown(socket.SHUT_RDWR)

    def connect(
        self,
        address: tuple[str, int],
        timeout: float | object = None,
        source_address: tuple[str, int] | None = None,
    ) -> socket.socket:
        """Адаптер фабрики HTTPConnection; каждый адрес получает остаток общего срока."""
        self.remaining()
        addresses = socket.getaddrinfo(*address, type=socket.SOCK_STREAM)
        self.remaining()
        last_error: OSError = OSError("Нет адресов центра")
        for family, kind, protocol, _, target in addresses:
            sock = socket.socket(family, kind, protocol)
            try:
                sock.settimeout(self.remaining())
                if source_address is not None:
                    sock.bind(source_address)
                sock.connect(target)
                with self._lock:
                    if self._expired:
                        raise TimeoutError("Истёк срок HTTP-запроса")
                    self._socket = sock.dup()
                sock.settimeout(self.remaining())
                return sock
            except OSError as error:
                last_error = error
                self._release()
                sock.close()
                self.remaining()
        raise last_error
