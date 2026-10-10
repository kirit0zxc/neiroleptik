"""Независимые тесты используют синтетический источник только в тестах."""

import pytest


@pytest.fixture(autouse=True)
def clean_database() -> None:
    """Этот уровень не обращается к БД и не выдаёт себя за PostgreSQL интеграцию."""
