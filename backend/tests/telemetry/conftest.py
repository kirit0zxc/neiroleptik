"""Изолированная схема поверх существующего защищённого backend Engine."""

from collections.abc import Iterator
from uuid import uuid4

import pytest
from sqlalchemy import Engine, event, insert
from sqlalchemy.schema import CreateSchema, DropSchema

from honeyforge.telemetry.tables import TelemetryTables


@pytest.fixture(scope="session")
def engine(engine: Engine) -> Iterator[Engine]:
    """Реальный общий Engine; ожидания SQL ограничены и при сбое тестовых потоков."""

    def bound_waits(dbapi_connection, connection_record, connection_proxy):
        with dbapi_connection.cursor() as cursor:
            cursor.execute("SET lock_timeout = '10s'")
            cursor.execute("SET statement_timeout = '15s'")
        dbapi_connection.commit()

    event.listen(engine, "checkout", bound_waits)
    try:
        yield engine
    finally:
        event.remove(engine, "checkout", bound_waits)


@pytest.fixture
def tables(engine: Engine) -> Iterator[TelemetryTables]:
    schema = "telemetry_test_" + uuid4().hex
    tables = TelemetryTables(schema)
    with engine.begin() as connection:
        connection.execute(CreateSchema(schema))
        tables.metadata.create_all(connection)
        connection.execute(insert(tables.state).values(id=1, last_seq=0))
    try:
        yield tables
    finally:
        with engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
