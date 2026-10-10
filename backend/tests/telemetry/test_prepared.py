"""Изолированные проверки внутреннего входа без имитации PostgreSQL."""

from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from uuid import UUID

import pytest

from honeyforge.db import Base
from honeyforge.telemetry.tables import TelemetryTables
from honeyforge.telemetry.types import PreparedEvent


@pytest.fixture(autouse=True)
def clean_database():
    """Этот модуль не обращается к БД; общий cleanup ему не требуется."""


class TestPreparedInput:
    def make(self):
        value = (Path(__file__).parent / "fixtures/base.json").read_bytes()
        return PreparedEvent(
            0,
            UUID("00000000-0000-4000-8000-000000000001"),
            UUID("cbedf64d-96bc-5f0d-9be6-b514f8c4a543"),
            UUID("00000000-0000-4000-8000-000000000002"),
            UUID("00000000-0000-4000-8000-000000000003"),
            1,
            value,
        )

    def test_original_is_detached_and_input_frozen(self):
        event = self.make()
        original = event.original()
        original["data"].clear()
        assert event.original()["data"]
        with pytest.raises(FrozenInstanceError):
            event.index = 1

    @pytest.mark.parametrize(
        "change",
        [
            dict(index=-1),
            dict(index=True),
            dict(agent_id=UUID(int=99)),
            dict(profile_revision=2),
            dict(profile_revision=True),
        ],
    )
    def test_projection_mismatch_rejected(self, change):
        with pytest.raises(ValueError):
            replace(self.make(), **change)

    def test_schema_is_explicit_and_metadata_separate(self):
        before = set(Base.metadata.tables)
        tables = TelemetryTables("telemetry_test_" + "a" * 32)
        assert set(Base.metadata.tables) == before
        assert all(t.schema == tables.metadata.schema for t in tables.metadata.tables.values())
        for schema in ["public", "", "telemetry_test_unsafe;DROP SCHEMA public"]:
            with pytest.raises(ValueError):
                TelemetryTables(schema)
