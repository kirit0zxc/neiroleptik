"""Долговечные проекции расследования в той же схеме, что committed события."""

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Table,
    UniqueConstraint,
    Uuid,
)

from honeyforge.telemetry.tables import TelemetryTables


class InvestigationTables:
    """Проекции меняются атомарно с публикацией; DDL выполняет Alembic."""

    def __init__(self, telemetry: TelemetryTables) -> None:
        m = telemetry.metadata
        self.groups = Table(
            "alert_groups",
            m,
            Column("id", Uuid, primary_key=True),
            Column("rule", String, nullable=False),
            Column("rule_version", Integer, nullable=False),
            Column("deployment_id", Uuid, nullable=False),
            Column("source_ip", String(45), nullable=False),
            Column("window_start", DateTime(timezone=True), nullable=False),
            Column("count", Integer, nullable=False),
            Column("session_id", Uuid),
            Column("created_at", DateTime(timezone=True)),
            Column("updated_at", DateTime(timezone=True), nullable=False),
            UniqueConstraint(
                "rule",
                "rule_version",
                "deployment_id",
                "source_ip",
                "window_start",
                name="uq_alert_group",
            ),
        )
        self.bases = Table(
            "alert_bases",
            m,
            Column("event_pk", ForeignKey(telemetry.events.c.id), primary_key=True),
            Column("group_id", ForeignKey(self.groups.c.id), nullable=False),
        )
        self.tokens = Table(
            "tokens",
            m,
            Column("id", Uuid, primary_key=True),
            Column("deployment_id", Uuid, nullable=False),
            Column("created_at", DateTime(timezone=True), nullable=False),
        )
        self.triggers = Table(
            "token_triggers",
            m,
            Column("event_pk", ForeignKey(telemetry.events.c.id), primary_key=True),
            Column("token_id", Uuid, nullable=False),
            Column("deployment_id", Uuid, nullable=False),
        )
        self.artifacts = Table(
            "artifacts",
            m,
            Column("id", Uuid, primary_key=True),
            Column("deployment_id", Uuid, nullable=False),
            Column("session_id", Uuid),
            Column("state", String, nullable=False),
            Column("name", String, nullable=False),
            Column("source_url", String),
            Column("observed_at", DateTime(timezone=True), nullable=False),
            Column("expires_at", DateTime(timezone=True)),
            Column("sha256", String(64)),
            Column("size_bytes", Integer),
            Column("mime", String),
            Column("error", String),
            Column("content", LargeBinary),
            Column("event_refs", JSON, nullable=False),
            Column("event_refs_truncated", Boolean, nullable=False),
        )
