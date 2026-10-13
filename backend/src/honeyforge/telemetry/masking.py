"""Единая безопасная публичная проекция для REST, WS и lookup."""

import re
from typing import Any
from uuid import UUID

from honeyforge.investigation.iocs import IocExporter

FIELDS = {
    "auth.attempt": ("username", "success"),
    "token.triggered": ("token_id",),
    "artifact.observed": ("name", "sha256"),
    "runtime.state": ("state",),
    "session.command": ("input", "truncated"),
    "session.output": ("output", "truncated"),
}


class PublicPolicy:
    """Неизвестные поля и произвольный terminal payload могут содержать credentials."""

    def __call__(self, public: dict[str, Any]) -> dict[str, Any]:
        raw = public["data"]
        # Разрешённое имя не разрешает вложенный произвольный JSON с секретами.
        public["data"] = {
            key: raw[key]
            for key in FIELDS.get(public["type"], ())
            if key in raw and type(raw[key]) is (bool if key in {"success", "truncated"} else str)
        }
        if "token_id" in public["data"]:
            try:
                public["data"]["token_id"] = str(UUID(public["data"]["token_id"]))
            except ValueError:
                del public["data"]["token_id"]
        if "sha256" in public["data"]:
            digest = public["data"]["sha256"]
            if re.fullmatch(r"[a-fA-F0-9]{64}", digest):
                public["data"]["sha256"] = digest.lower()
            else:
                del public["data"]["sha256"]
        if public["type"] == "auth.attempt":
            public["data"]["password_present"] = bool(raw.get("password"))
        if public["type"] in {"http.request", "tcp.request"}:
            # Capture не доказывает полноту обмена. Ни старые flags, ни входной
            # status/response не разрешают показать выдуманный результат ответа.
            data: dict[str, Any] = {}
            count = raw.get("captured_bytes")
            if type(count) is int and 0 <= count <= 9007199254740991:
                data["captured_bytes"] = count
            if type(raw.get("mapper_truncated")) is bool:
                data["mapper_truncated"] = raw["mapper_truncated"]
            if raw.get("capture_completeness") == "unknown":
                data["capture_completeness"] = "unknown"
            if public["type"] == "http.request":
                method = raw.get("method")
                if isinstance(method, str) and method in {
                    "GET",
                    "HEAD",
                    "POST",
                    "PUT",
                    "DELETE",
                    "OPTIONS",
                    "PATCH",
                    "TRACE",
                    "CONNECT",
                }:
                    data["method"] = method
                state = raw.get("http_parse_state")
                if isinstance(state, str) and state in {"parsed", "incomplete", "invalid", "limited"}:
                    data["http_parse_state"] = state
                if isinstance(raw.get("path"), str):
                    data["path"] = "/" if raw["path"] == "/" else "[скрыто]"
            public["data"] = data
        if public["type"] == "artifact.observed":
            public["data"]["source_url"] = IocExporter.safe_url(raw.get("source_url"))
        return public
