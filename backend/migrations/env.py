"""Миграции работают с той же PostgreSQL-конфигурацией, что и приложение."""

from alembic import context
from sqlalchemy import create_engine

from honeyforge import models
from honeyforge.config import get_settings

metadata = models.User.metadata
url = get_settings().database_url.get_secret_value()
if context.is_offline_mode():
    context.configure(url=url, target_metadata=metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine(url, connect_args={"connect_timeout": 5})
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()
