"""Образ без dev-зависимостей обязан поддерживать рабочий WebSocket endpoint."""

import re
import tomllib
from pathlib import Path


def test_websocket_transport_is_a_runtime_dependency():
    """Регрессия: библиотека в dev работала в тестах, но исчезала в Docker."""
    root = Path(__file__).resolve().parents[2]
    project = tomllib.loads((root / "backend/pyproject.toml").read_text(encoding="utf-8"))
    dependencies = [item.lower().replace(" ", "") for item in project["project"]["dependencies"]]
    assert any(
        re.match(r"^(websockets|wsproto)(?:[<>=!~;\[]|$)", dependency)
        or dependency.startswith("uvicorn[standard]")
        for dependency in dependencies
    ), "WebSocket transport must be installed by uv sync --no-dev"
