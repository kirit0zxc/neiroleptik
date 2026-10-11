"""Метаинформация committed телеметрии для Alembic и изолированных тестов."""

import re

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    Uuid,
)


class TelemetryTables:
    """DDL выполняет Alembic; тестовые схемы требуют уникального имени."""

    def __init__(self, schema: str = "hf_telemetry") -> None:
        if schema != "hf_telemetry" and not re.fullmatch(r"telemetry_test_[a-f0-9]{32}", schema):
            raise ValueError("Допускается рабочая hf_telemetry или изолированная тестовая схема")
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
            Column("source_ip", String(45), nullable=True),
            Column("session_id", Uuid, nullable=True),
            Column("event_type", String(64), nullable=False),
            UniqueConstraint("agent_id", "event_id", name="uq_event_identity"),
        )
        Index(
            "ix_event_timeline",
            self.events.c.session_id,
            self.events.c.occurred_at,
            self.events.c.event_id,
            self.events.c.agent_id,
        )
        Index(
            "ix_event_history",
            self.events.c.received_at,
            self.events.c.event_id,
            self.events.c.agent_id,
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
