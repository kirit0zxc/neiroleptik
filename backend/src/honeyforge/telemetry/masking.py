"""Единая безопасная публичная проекция для REST, WS и lookup."""

import re
from typing import Any
from uuid import UUID

from honeyforge.investigation.iocs import IocExporter

FIELDS = {
    "auth.attempt": ("username", "success"),
    "http.request": ("method", "path", "truncated"),
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
        if public["type"] == "http.request" and isinstance(raw.get("path"), str):
            # Даже path способен включать query credentials: общая лента не передаёт query.
            public["data"]["path"] = raw["path"].split("?", 1)[0].split("#", 1)[0]
        if public["type"] == "artifact.observed":
            public["data"]["source_url"] = IocExporter.safe_url(raw.get("source_url"))
        return public
