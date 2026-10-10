"""Перегенерация golden через единственный действующий агентский канонизатор.

Запуск из корня репозитория Python-окружением backend с закреплённым Pydantic.
Этот скрипт не импортируется backend-приложением или PostgreSQL-тестами.
"""

import json
from pathlib import Path

from agent.telemetry.schemas import Event

ROOT = Path(__file__).resolve().parents[4]
TARGET = Path(__file__).parent
original = json.loads(
    (ROOT / "contracts/fixtures/telemetry/agent-event.json").read_text(encoding="utf-8")
)
variants = {
    "base": original["data"],
    "unicode": {"input": "е\u0301/é/雪"},
    "integer": {"value": 1},
    "float": {"value": 1.0},
    "negative_zero": {"value": -0.0},
    "positive_zero": {"value": 0.0},
    "array_ab": {"items": ["a", "b"]},
    "array_ba": {"items": ["b", "a"]},
    "password_a": {"username": "test", "password": "synthetic-a", "success": False},
    "password_b": {"username": "test", "password": "synthetic-b", "success": False},
}
for name, data in variants.items():
    event = Event.model_validate({**original, "data": data})
    (TARGET / f"{name}.json").write_bytes(event.canonical_json().encode("utf-8"))
