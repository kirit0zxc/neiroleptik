"""Строгая валидация исходного ASCII JSONL без преобразования в Event."""

from __future__ import annotations

import base64
import binascii
import json
import re
from datetime import datetime
from ipaddress import ip_address
from typing import Any

from .models import JournalReadError, is_uuid

MAX_JSON_DEPTH = 32
_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?Z")
_HEADER = {
    "record",
    "schema",
    "journal_id",
    "journal_seq",
    "previous_journal_id",
    "instance_id",
    "deployment_id",
    "profile_revision",
    "opened_at",
    "first_seq",
}
_EVENT = {
    "record",
    "seq",
    "event",
    "timestamp",
    "connection_id",
    "protocol",
    "service",
    "peer",
    "payload_b64",
}
_FOOTER = {"record", "journal_id", "last_seq", "closed_at", "reason"}


def _integer(value: object, minimum: int, maximum: int | None = None) -> bool:
    return type(value) is int and value >= minimum and (maximum is None or value <= maximum)


def _timestamp(value: object) -> bool:
    if not isinstance(value, str) or _TIMESTAMP.fullmatch(value) is None:
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


class RecordValidator:
    """Проверяет форму одной полной строки; порядок и привязку проверяет поток."""

    @staticmethod
    def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    @staticmethod
    def _constant(value: str) -> None:
        raise ValueError

    @staticmethod
    def _check_depth(text: str) -> None:
        """Ограничивает вложенность до JSON-парсера, учитывая скобки внутри строк."""
        depth = 0
        quoted = escaped = False
        for character in text:
            if quoted:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    quoted = False
            elif character == '"':
                quoted = True
            elif character in "[{":
                depth += 1
                if depth > MAX_JSON_DEPTH:
                    raise ValueError
            elif character in "]}":
                depth -= 1

    def parse(self, raw: bytes, offset: int) -> dict[str, Any]:
        """Ожидаемые отказы скрывают сообщения парсера и цепочки исключений."""
        try:
            if not raw.endswith(b"\n") or b"\r" in raw or b"\n" in raw[:-1]:
                raise ValueError
            text = raw.decode("ascii")
            self._check_depth(text)
            document = json.loads(
                text, object_pairs_hook=self._unique, parse_constant=self._constant
            )
            if not isinstance(document, dict):
                raise ValueError
            self._shape(document, offset)
        except (ValueError, RecursionError) as error:
            if isinstance(error, JournalReadError):
                raise
            raise JournalReadError("INVALID_RECORD", offset) from None
        return document

    def _shape(self, row: dict[str, Any], offset: int) -> None:
        kind = row.get("record")
        if kind == "header":
            if set(row) != _HEADER or not isinstance(row["schema"], str):
                raise ValueError
            if row["schema"] != "honeyforge-low-journal/1":
                raise JournalReadError("UNSUPPORTED_SCHEMA", offset)
            if not (
                all(is_uuid(row[key]) for key in ("journal_id", "instance_id", "deployment_id"))
                and all(
                    _integer(row[key], 1)
                    for key in ("journal_seq", "first_seq", "profile_revision")
                )
                and (row["previous_journal_id"] is None or is_uuid(row["previous_journal_id"]))
                and (row["journal_seq"] != 1 or row["previous_journal_id"] is None)
                and _timestamp(row["opened_at"])
            ):
                raise ValueError
        elif kind == "event":
            if set(row) != _EVENT:
                raise ValueError
            self._event(row)
        elif kind == "footer":
            if not (
                set(row) == _FOOTER
                and is_uuid(row["journal_id"])
                and _integer(row["last_seq"], 0)
                and _timestamp(row["closed_at"])
                and row["reason"] in ("rotation", "shutdown")
            ):
                raise ValueError
        else:
            raise ValueError

    @staticmethod
    def _event(row: dict[str, Any]) -> None:
        service, peer = row["service"], row["peer"]
        if not (
            _integer(row["seq"], 1)
            and row["event"] in ("connection", "request")
            and _timestamp(row["timestamp"])
            and is_uuid(row["connection_id"])
            and row["protocol"] in ("tcp", "http")
            and isinstance(service, dict)
            and set(service) == {"protocol", "port"}
            and service["protocol"] == row["protocol"]
            and _integer(service["port"], 1, 65535)
        ):
            raise ValueError
        if peer is not None:
            if not (
                isinstance(peer, dict)
                and set(peer) == {"host", "port"}
                and isinstance(peer["host"], str)
                and _integer(peer["port"], 0, 65535)
            ):
                raise ValueError
            ip_address(peer["host"])
        payload = row["payload_b64"]
        if not isinstance(payload, str) or (row["event"] == "connection" and payload != ""):
            raise ValueError
        try:
            decoded = base64.b64decode(payload, validate=True)
        except binascii.Error:
            raise ValueError from None
        # Повторное кодирование проверяет также лишний padding и ненулевые pad bits.
        if base64.b64encode(decoded).decode("ascii") != payload:
            raise ValueError
