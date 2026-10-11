"""Миграции работают с той же PostgreSQL-конфигурацией, что и приложение."""

from alembic import context
from sqlalchemy import create_engine

from honeyforge import models
from honeyforge.config import get_settings
from honeyforge.investigation.tables import InvestigationTables
from honeyforge.telemetry.tables import TelemetryTables

telemetry = TelemetryTables()
InvestigationTables(telemetry)
metadata = [models.User.metadata, telemetry.metadata]


def include_name(name: str | None, type_: str, parent_names: dict[str, str | None]) -> bool:
    """Тестовые схемы не являются кандидатами на удаление при autogenerate/check."""
    return type_ != "schema" or name in {None, "hf_telemetry"}


url = get_settings().database_url.get_secret_value()
if context.is_offline_mode():
    context.configure(
        url=url,
        target_metadata=metadata,
        literal_binds=True,
        include_schemas=True,
        include_name=include_name,
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine(url, connect_args={"connect_timeout": 5})
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=metadata,
            compare_type=True,
            include_schemas=True,
            include_name=include_name,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()
