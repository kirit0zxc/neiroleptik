"""Ограниченный HTTP-транспорт команд: Bearer не передаётся по redirects."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import math
import re
import time
from http.client import HTTPException
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, OpenerDirector, Request, build_opener
from uuid import UUID

from agent.runtime.contract import AgentConfig, ConfigParser, ContractError
from agent.runtime.executor.protocol import ClaimedCommand, LeaseReply, validate_job
from agent.runtime.executor.protocol import CommandClient as CommandClient

_ERROR_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")


class _ResponseReader(Protocol):
    def read1(self, size: int) -> bytes: ...


class ClientError(Exception):
    """Ошибка содержит только код и HTTP status, без тела ответа, URL и токенов."""

    def __init__(self, code: str, status: int | None = None) -> None:
        self.code = code
        self.status = status
        super().__init__(f"Командный API: {code}" + (f" (HTTP {status})" if status else ""))


class _NoRedirects(HTTPRedirectHandler):
    """urllib не должен повторять авторизованный запрос на другом адресе."""

    def redirect_request(
        self,
        req: Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        return None


class HttpCommandClient:
    """Один bounded запрос выполняется в отдельном потоке, без отмены HTTP I/O."""

    def __init__(
        self,
        base_url: str,
        bearer_token: str,
        *,
        timeout: float = 10.0,
        max_response_bytes: int = 1_048_576,
        allow_http: bool = False,
    ) -> None:
        try:
            parsed = urlsplit(base_url)
            hostname = parsed.hostname
            _ = parsed.port
        except (ValueError, TypeError):
            raise ValueError("Некорректный адрес командного API") from None
        if (
            not isinstance(base_url, str)
            or not hostname
            or parsed.username is not None
            or parsed.password is not None
            or "?" in base_url
            or "#" in base_url
            or "\\" in base_url
            or any(ord(c) <= 32 or ord(c) == 127 for c in base_url)
        ):
            raise ValueError("Некорректный адрес командного API")
        loopback = hostname.lower() == "localhost"
        try:
            loopback = loopback or ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            pass
        if parsed.scheme != "https" and not (parsed.scheme == "http" and allow_http and loopback):
            raise ValueError("Нужен HTTPS; HTTP разрешается явно только для loopback")
        if (
            not isinstance(bearer_token, str)
            or not bearer_token
            or any(ord(c) <= 32 or ord(c) >= 127 for c in bearer_token)
        ):
            raise ValueError("Некорректный Bearer token")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("Таймаут должен быть положительным конечным числом")
        try:
            valid_timeout = math.isfinite(timeout) and timeout > 0
        except OverflowError:
            valid_timeout = False
        if not valid_timeout:
            raise ValueError("Таймаут должен быть положительным конечным числом")
        if type(max_response_bytes) is not int or not 1 <= max_response_bytes <= 1_048_576:
            raise ValueError("Предел ответа должен быть от 1 байта до 1 MiB")
        self._base_url = base_url.rstrip("/")
        self._bearer_token = bearer_token
        self._timeout = float(timeout)
        self._maximum = max_response_bytes
        self._opener: OpenerDirector = build_opener(_NoRedirects())

    @staticmethod
    def _command_id(value: str) -> str:
        try:
            return str(UUID(value))
        except (ValueError, AttributeError, TypeError):
            raise ValueError("Некорректный command_id") from None

    def _read(self, response: _ResponseReader, deadline: float) -> bytes:
        """Медленный поток ограничен общим сроком и суммарным числом байтов."""
        chunks: list[bytes] = []
        remaining = self._maximum + 1
        while remaining:
            if time.monotonic() >= deadline:
                raise ClientError("NETWORK_ERROR")
            chunk = response.read1(min(remaining, 65_536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        body = b"".join(chunks)
        if len(body) > self._maximum:
            raise ClientError("RESPONSE_TOO_LARGE")
        return body

    def _request(self, method: str, path: str, payload: dict[str, object] | None) -> object:
        try:
            encoded = (
                None
                if payload is None
                else json.dumps(payload, allow_nan=False, separators=(",", ":")).encode()
            )
        except (ValueError, TypeError, UnicodeError):
            raise ClientError("INVALID_REQUEST") from None
        request = Request(
            self._base_url + path,
            data=encoded,
            headers={
                "Authorization": "Bearer " + self._bearer_token,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method=method,
        )
        deadline = time.monotonic() + self._timeout
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                # read1 возвращает одну доступную порцию; медленная непрерывная передача
                # не должна обходить общий таймаут, сбрасывая socket timeout каждым байтом.
                body = self._read(response, deadline)
                if response.status != 200:
                    raise ClientError("HTTP_ERROR", response.status)
        except HTTPError as error:
            try:
                with error:
                    if 300 <= error.code < 400:
                        raise ClientError("REDIRECT_REJECTED", error.code) from None
                    body = self._read(error, deadline)
            except (OSError, HTTPException, ValueError, UnicodeError):
                raise ClientError("NETWORK_ERROR") from None
            code = "HTTP_ERROR"
            if len(body) <= self._maximum:
                try:
                    parsed_error = json.loads(body)
                    remote_code = (
                        parsed_error.get("code") if isinstance(parsed_error, dict) else None
                    )
                    if isinstance(remote_code, str) and _ERROR_CODE.fullmatch(remote_code):
                        code = remote_code
                except (ValueError, UnicodeError, RecursionError):
                    pass
            raise ClientError(code, error.code) from None
        except (URLError, TimeoutError, OSError, HTTPException, UnicodeError, ValueError):
            raise ClientError("NETWORK_ERROR") from None
        try:
            return json.loads(body)
        except (ValueError, UnicodeError, RecursionError):
            raise ClientError("INVALID_RESPONSE") from None

    async def _send(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None,
    ) -> object:
        """Ожидание ограничено общим сроком, даже если DNS/TLS всё ещё завершается."""
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._request, method, path, payload),
                self._timeout,
            )
        except TimeoutError:
            raise ClientError("NETWORK_ERROR") from None

    async def claim(self, command_id: str | None = None) -> ClaimedCommand | None:
        """Восстановление известной команды не выбирает случайный следующий job."""
        payload: dict[str, object] = {}
        if command_id is not None:
            payload["command_id"] = self._command_id(command_id)
        value = await self._send("POST", "/agent/v1/commands/claim", payload)
        try:
            if not isinstance(value, dict) or set(value) != {"command"}:
                raise ContractError("claim")
            command = value["command"]
            if command is None:
                return None
            result = ClaimedCommand.parse(command)
            if command_id is not None and result.command_id != payload["command_id"]:
                raise ContractError("command.command_id")
            return result
        except ContractError:
            raise ClientError("INVALID_RESPONSE") from None

    async def renew(self, command_id: str, lease_token: str) -> LeaseReply:
        command_id = self._command_id(command_id)
        value = await self._send(
            "POST",
            f"/agent/v1/commands/{command_id}/renew",
            {"lease_token": lease_token},
        )
        try:
            result = LeaseReply.parse(value)
            if result.command_id != command_id:
                raise ContractError("lease.command_id")
            return result
        except ContractError:
            raise ClientError("INVALID_RESPONSE") from None

    async def ack(self, command_id: str, payload: dict[str, object]) -> dict[str, object]:
        command_id = self._command_id(command_id)
        value = await self._send("POST", f"/agent/v1/commands/{command_id}/ack", payload)
        try:
            return validate_job(value, command_id, payload)
        except ContractError:
            raise ClientError("INVALID_RESPONSE") from None

    async def config(self) -> AgentConfig:
        """История центра нужна для обычного stop, если локальный журнал ещё пуст."""
        value = await self._send("GET", "/agent/v1/config", None)
        try:
            return ConfigParser.parse(value)
        except ContractError:
            raise ClientError("INVALID_RESPONSE") from None
