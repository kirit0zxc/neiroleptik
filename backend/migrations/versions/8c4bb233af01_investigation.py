"""Committed телеметрия и расследование; неизменяемый снимок DDL этапа."""

from alembic import op

revision = "8c4bb233af01"
down_revision = "4fa62bdeddd4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Новая схема сохраняет прежние control таблицы и данные."""
    op.execute("CREATE SCHEMA hf_telemetry")
    op.execute("""
CREATE TABLE hf_telemetry.alert_groups (
	id UUID NOT NULL, 
	rule VARCHAR NOT NULL, 
	rule_version INTEGER NOT NULL, 
	deployment_id UUID NOT NULL, 
	source_ip VARCHAR(45) NOT NULL, 
	window_start TIMESTAMP WITH TIME ZONE NOT NULL, 
	count INTEGER NOT NULL, 
	session_id UUID, 
	created_at TIMESTAMP WITH TIME ZONE, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_alert_group UNIQUE (rule, rule_version, deployment_id, source_ip, window_start)
)
""")
    op.execute("""
CREATE TABLE hf_telemetry.artifacts (
	id UUID NOT NULL, 
	deployment_id UUID NOT NULL, 
	session_id UUID, 
	state VARCHAR NOT NULL, 
	name VARCHAR NOT NULL, 
	source_url VARCHAR, 
	observed_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	expires_at TIMESTAMP WITH TIME ZONE, 
	sha256 VARCHAR(64), 
	size_bytes INTEGER, 
	mime VARCHAR, 
	error VARCHAR, 
	content BYTEA, 
	event_refs JSON NOT NULL, 
	event_refs_truncated BOOLEAN NOT NULL, 
	PRIMARY KEY (id)
)
""")
    op.execute("""
CREATE TABLE hf_telemetry.events (
	id BIGSERIAL NOT NULL, 
	agent_id UUID NOT NULL, 
	event_id UUID NOT NULL, 
	deployment_id UUID NOT NULL, 
	instance_id UUID NOT NULL, 
	profile_revision INTEGER NOT NULL, 
	canonical_bytes BYTEA NOT NULL, 
	canonical_version INTEGER NOT NULL, 
	digest BYTEA NOT NULL, 
	received_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	occurred_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	source_ip VARCHAR(45), 
	session_id UUID, 
	event_type VARCHAR(64) NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_event_identity UNIQUE (agent_id, event_id)
)
""")
    op.execute(
        "CREATE INDEX ix_event_history ON hf_telemetry.events (received_at, event_id, agent_id)"
    )
    op.execute(
        "CREATE INDEX ix_event_timeline ON hf_telemetry.events "
        "(session_id, occurred_at, event_id, agent_id)"
    )
    op.execute("""
CREATE TABLE hf_telemetry.publisher_state (
	id INTEGER NOT NULL, 
	last_seq BIGINT NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT singleton_counter CHECK (id = 1 AND last_seq >= 0)
)
""")
    op.execute("""
CREATE TABLE hf_telemetry.tokens (
	id UUID NOT NULL, 
	deployment_id UUID NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
)
""")
    op.execute("""
CREATE TABLE hf_telemetry.alert_bases (
	event_pk BIGINT NOT NULL, 
	group_id UUID NOT NULL, 
	PRIMARY KEY (event_pk), 
	FOREIGN KEY(event_pk) REFERENCES hf_telemetry.events (id), 
	FOREIGN KEY(group_id) REFERENCES hf_telemetry.alert_groups (id)
)
""")
    op.execute("""
CREATE TABLE hf_telemetry.outbox (
	event_pk BIGINT NOT NULL, 
	processed_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (event_pk), 
	FOREIGN KEY(event_pk) REFERENCES hf_telemetry.events (id)
)
""")
    op.execute("""
CREATE TABLE hf_telemetry.publications (
	stream_seq BIGINT NOT NULL, 
	event_pk BIGINT NOT NULL, 
	published_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (stream_seq), 
	CONSTRAINT positive_stream_seq CHECK (stream_seq > 0), 
	UNIQUE (event_pk), 
	FOREIGN KEY(event_pk) REFERENCES hf_telemetry.events (id)
)
""")
    op.execute("""
CREATE TABLE hf_telemetry.token_triggers (
	event_pk BIGINT NOT NULL, 
	token_id UUID NOT NULL, 
	deployment_id UUID NOT NULL, 
	PRIMARY KEY (event_pk), 
	FOREIGN KEY(event_pk) REFERENCES hf_telemetry.events (id)
)
""")
    op.execute("INSERT INTO hf_telemetry.publisher_state (id, last_seq) VALUES (1, 0)")


def downgrade() -> None:
    """Явный downgrade удаляет только данные нового этапа."""
    op.execute("DROP SCHEMA hf_telemetry CASCADE")
