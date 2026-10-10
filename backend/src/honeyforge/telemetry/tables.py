"""Отдельная метаинформация прототипа без регистрации в общей Alembic Base."""

import re

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    Uuid,
)


class TelemetryTables:
    """Все обращения явно квалифицируются схемой; DDL выполняет только тест."""

    def __init__(self, schema: str) -> None:
        if not re.fullmatch(r"telemetry_test_[a-f0-9]{32}", schema):
            raise ValueError("Прототип допускает только изолированную тестовую схему")
        self.metadata = MetaData(schema=schema)
        self.events = Table(
            "events",
            self.metadata,
            Column("id", BigInteger, primary_key=True),
            Column("agent_id", Uuid, nullable=False),
            Column("event_id", Uuid, nullable=False),
            Column("deployment_id", Uuid, nullable=False),
            Column("instance_id", Uuid, nullable=False),
            Column("profile_revision", Integer, nullable=False),
            Column("canonical_bytes", LargeBinary, nullable=False),
            Column("canonical_version", Integer, nullable=False),
            Column("digest", LargeBinary, nullable=False),
            Column("received_at", DateTime(timezone=True), nullable=False),
            Column("occurred_at", DateTime(timezone=True), nullable=False),
            Column("source_ip", String(45), nullable=False),
            Column("event_type", String(64), nullable=False),
            UniqueConstraint("agent_id", "event_id", name="uq_event_identity"),
        )
        self.outbox = Table(
            "outbox",
            self.metadata,
            Column("event_pk", BigInteger, ForeignKey(self.events.c.id), primary_key=True),
            Column("processed_at", DateTime(timezone=True)),
        )
        self.publications = Table(
            "publications",
            self.metadata,
            Column("stream_seq", BigInteger, primary_key=True, autoincrement=False),
            Column(
                "event_pk", BigInteger, ForeignKey(self.events.c.id), nullable=False, unique=True
            ),
            Column("published_at", DateTime(timezone=True), nullable=False),
            CheckConstraint("stream_seq > 0", name="positive_stream_seq"),
        )
        self.state = Table(
            "publisher_state",
            self.metadata,
            Column("id", Integer, primary_key=True, autoincrement=False),
            Column("last_seq", BigInteger, nullable=False),
            CheckConstraint("id = 1 AND last_seq >= 0", name="singleton_counter"),
        )
