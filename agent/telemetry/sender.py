"""Доставка долговечной очереди: HTTP-ответ сам по себе не подтверждает события."""

import http.client
import ipaddress
import json
import math
import random
import re
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC
from email.utils import parsedate_to_datetime
from typing import Literal, Protocol
from urllib.parse import urlsplit

from pydantic import SecretStr

from agent.telemetry.schemas import Ack, Batch
from agent.telemetry.spool import SpoolRepository

MAX_ACK_BYTES = 256 * 1024


class TransportError(Exception):
    """Связь прервана: сервер мог сохранить события, поэтому нужен безопасный повтор."""


class ResponseTooLarge(Exception):
    """Недоверенный ответ превысил ограничение памяти и не может стать ACK."""


@dataclass(frozen=True)
class HttpResponse:
    """Транспорт возвращает только данные, необходимые для решения о повторе."""

    status: int
    body: bytes
    retry_after: str | None = None


class BatchTransport(Protocol):
    """Транспорт не меняет batch и не применяет ACK самостоятельно."""

    def post(self, payload: bytes) -> HttpResponse:
        """Отправить один неизменяемый снимок с ограниченным временем ожидания."""
        ...


class HttpBatchTransport:
    """Прямой HTTP(S) без redirect/proxy, чтобы Bearer не покидал заданный центр."""

    def __init__(self, endpoint: str, token: SecretStr, timeout: float = 10.0) -> None:
        if not math.isfinite(timeout) or not 0 < timeout <= 300 or isinstance(timeout, bool):
            raise ValueError("Таймаут должен быть в пределах (0, 300] секунд")
        if any(ord(char) <= 32 or ord(char) == 127 for char in endpoint):
            raise ValueError("Адрес центра содержит недопустимые символы")
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Нужен HTTP(S) endpoint без credentials, query и fragment")
        try:
            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = parsed.hostname == "localhost"
        if parsed.scheme == "http" and not loopback:
            raise ValueError("HTTP разрешён только для локального dev-центра; нужен HTTPS")
        if not re.fullmatch(r"hf_agent_[A-Za-z0-9_-]{1,119}", token.get_secret_value()):
            raise ValueError("Некорректный формат agent token")
        self._host = parsed.hostname
        self._port = parsed.port
        self._path = parsed.path or "/"
        self._tls = parsed.scheme == "https"
        self._token = token
        self._timeout = timeout

    def post(self, payload: bytes) -> HttpResponse:
        connection: http.client.HTTPConnection
        if self._tls:
            connection = http.client.HTTPSConnection(
                self._host, self._port, timeout=self._timeout, context=ssl.create_default_context()
            )
        else:
            connection = http.client.HTTPConnection(self._host, self._port, timeout=self._timeout)
        try:
            connection.request(
                "POST",
                self._path,
                body=payload,
                headers={
                    "Authorization": "Bearer " + self._token.get_secret_value(),
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
            response = connection.getresponse()
            # Тела HTTP-ошибок не нужны для классификации и могут содержать секреты.
            body = response.read(MAX_ACK_BYTES + 1) if response.status == 200 else b""
            if len(body) > MAX_ACK_BYTES:
                raise ResponseTooLarge("Ответ центра превышает лимит ACK")
            if response.status == 200 and response.length not in {None, 0}:
                raise TransportError("HTTP-ответ оборван до заявленной границы тела")
            return HttpResponse(response.status, body, response.getheader("Retry-After"))
        except (OSError, http.client.HTTPException):
            raise TransportError("Не удалось получить полный ответ центра") from None
        finally:
            connection.close()


@dataclass(frozen=True)
class SendResult:
    """Наблюдаемый результат без payload, токена и текстов ошибок внешнего сервера."""

    state: Literal["idle", "waiting", "delivered", "retry", "paused"]
    accepted: int = 0
    quarantined: int = 0
    retry_in: float = 0.0
    error_code: str | None = None
    http_status: int | None = None


class TelemetrySender:
    """Один вызывающий поток; повтор до ACK сохраняет исходные байты и batch_id."""

    def __init__(
        self,
        spool: SpoolRepository,
        transport: BatchTransport,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        jitter: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self._spool = spool
        self._transport = transport
        self._clock = clock
        self._wall_clock = wall_clock
        self._jitter = jitter
        self._batch: Batch | None = None
        self._failures = 0
        self._deadline = 0.0
        self._paused: SendResult | None = None

    def resume(self) -> None:
        """Вызывающий исправляет конфигурацию/доступ и явно разрешает новую попытку."""
        self._paused = None
        self._deadline = 0.0
        self._failures = 0

    def step(self) -> SendResult:
        """Не более одного POST; исключение локальной БД не выдаётся за сетевой успех."""
        if self._paused is not None:
            return self._paused
        remaining = self._deadline - self._clock()
        if remaining > 0:
            return SendResult("waiting", retry_in=remaining)
        if self._batch is None:
            self._batch = self._spool.next_batch()
        if self._batch is None:
            return SendResult("idle")
        try:
            response = self._transport.post(self._batch.to_bytes())
        except TransportError:
            return self._retry("TRANSPORT_ERROR")
        except ResponseTooLarge:
            return self._retry("INVALID_ACK")
        if response.status == 429 or 500 <= response.status <= 599:
            minimum = (
                self._retry_after(response.retry_after) if response.status in {429, 503} else 0
            )
            return self._retry("HTTP_RETRY", status=response.status, minimum=minimum)
        if response.status != 200:
            code = "AUTH_REQUIRED" if response.status in {401, 403} else "HTTP_REJECTED"
            self._paused = SendResult("paused", error_code=code, http_status=response.status)
            return self._paused
        try:
            if len(response.body) > MAX_ACK_BYTES:
                raise ValueError("Превышен лимит ACK")
            raw = json.loads(
                response.body.decode("utf-8"),
                object_pairs_hook=self._unique_object,
                parse_constant=self._invalid_constant,
            )
            ack = Ack.model_validate(raw)
            accepted = self._spool.acknowledge(self._batch, ack)
        except (ValueError, RecursionError):
            return self._retry("INVALID_ACK", status=200)
        self._batch = None
        quarantined = sum(not item.retryable for item in ack.rejected)
        if any(item.retryable for item in ack.rejected):
            return self._retry(
                "EVENTS_RETRYABLE", accepted=accepted, quarantined=quarantined, status=200
            )
        self._failures = 0
        self._deadline = 0.0
        return SendResult("delivered", accepted, quarantined, http_status=200)

    def _retry(
        self,
        code: str,
        *,
        accepted: int = 0,
        quarantined: int = 0,
        status: int | None = None,
        minimum: float = 0.0,
    ) -> SendResult:
        ceiling = min(60.0, 2.0 ** min(self._failures, 6))
        delay = max(minimum, self._jitter(ceiling / 2, ceiling))
        self._failures += 1
        self._deadline = self._clock() + delay
        return SendResult("retry", accepted, quarantined, delay, code, status)

    def _retry_after(self, value: str | None) -> float:
        if value is None:
            return 0.0
        try:
            if re.fullmatch(r"[0-9]{1,9}", value.strip()):
                seconds = float(value)
            else:
                date = parsedate_to_datetime(value)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=UTC)
                seconds = date.timestamp() - self._wall_clock()
            return min(86400.0, max(0.0, seconds))
        except (ValueError, TypeError, OverflowError):
            return 0.0

    @staticmethod
    def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        """Повтор ключа недопустим даже при одинаковых значениях."""
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Неоднозначный ACK")
            result[key] = value
        return result

    @staticmethod
    def _invalid_constant(value: str) -> object:
        """JSON допускает только конечные числа."""
        raise ValueError("Некорректная константа JSON")
