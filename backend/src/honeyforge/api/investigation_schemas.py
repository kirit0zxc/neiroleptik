"""Публичные DTO расследования без секретных значений и внутренних PK."""

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

EventTimestamp = Annotated[str, Field(json_schema_extra={"format": "date-time"})]


class ReadModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReplayChunk(ReadModel):
    """Текстовые байты TTY; frontend не исполняет управляющие последовательности."""

    seq: int = Field(ge=0, le=9007199254740991)
    offset_ms: int = Field(ge=0)
    direction: Literal["in", "out"]
    data_base64: str
    truncated: bool


class ReplayPage(ReadModel):
    """Конец доступной страницы отдельно от завершения записи."""

    session_id: UUID
    started_at: datetime
    chunks: list[ReplayChunk]
    next_cursor: str | None
    tail_cursor: str | None
    complete: bool


class Endpoint(ReadModel):
    ip: str
    port: Annotated[int, Field(ge=0, le=65535)]


class Service(ReadModel):
    protocol: Literal["tcp", "http", "ssh"]
    port: Annotated[int, Field(ge=1, le=65535)]


class EventRef(ReadModel):
    agent_id: UUID
    event_id: UUID


class EventV1(ReadModel):
    schema_version: Literal[1]
    event_id: UUID
    agent_id: UUID
    deployment_id: UUID
    instance_id: UUID
    session_id: UUID | None
    source_seq: Annotated[int, Field(ge=0, le=9007199254740991)]
    occurred_at: EventTimestamp
    received_at: EventTimestamp
    type: Literal[
        "connection.opened",
        "auth.attempt",
        "session.command",
        "session.output",
        "http.request",
        "tcp.request",
        "artifact.observed",
        "token.triggered",
        "session.closed",
        "runtime.state",
    ]
    source: Endpoint | None
    service: Service
    data: dict[str, Any]
    artifact_ids: list[UUID]
    profile_revision: Annotated[int, Field(ge=1)]


class EventPage(ReadModel):
    items: list[EventV1]
    next_cursor: str | None


class StreamEvent(ReadModel):
    stream_seq: str = Field(pattern=r"^[0-9]+$")
    event: EventV1


class CatchUpPage(ReadModel):
    items: list[StreamEvent]
    watermark: str = Field(pattern=r"^[0-9]+$")
    next_cursor: str | None


class AgentRejection(ReadModel):
    index: int
    event_id: UUID | None
    code: Literal[
        "INVALID_EVENT",
        "EVENT_ID_CONFLICT",
        "DEPLOYMENT_MISMATCH",
        "INSTANCE_NOT_REGISTERED",
        "TEMPORARILY_UNAVAILABLE",
    ]
    retryable: bool


class AgentAck(ReadModel):
    batch_id: UUID
    accepted_ids: list[UUID]
    rejected: list[AgentRejection]


class LookupInput(ReadModel):
    refs: Annotated[
        list[EventRef], Field(min_length=1, max_length=100, json_schema_extra={"uniqueItems": True})
    ]

    @field_validator("refs")
    @classmethod
    def unique_refs(cls, refs: list[EventRef]) -> list[EventRef]:
        if len({(r.agent_id, r.event_id) for r in refs}) != len(refs):
            raise ValueError("Повтор ссылки на событие")
        return refs


class LookupRead(ReadModel):
    items: list[EventV1]
    unavailable: list[EventRef]


class ArtifactRead(ReadModel):
    id: UUID
    session_id: UUID | None
    deployment_id: UUID
    state: Literal["pending", "available", "failed", "expired"]
    name: str
    source_url: str | None
    size_bytes: int | None
    sha256: str | None
    mime: str | None
    observed_at: datetime
    expires_at: datetime | None
    error: str | None
    event_refs: Annotated[list[EventRef], Field(max_length=100)]
    event_refs_truncated: bool


class ArtifactPage(ReadModel):
    items: list[ArtifactRead]
    next_cursor: str | None


class HoneytokenRead(ReadModel):
    id: UUID
    deployment_id: UUID
    kind: Literal["url"]
    label: str
    created_at: datetime
    last_triggered_at: datetime | None
    trigger_count: Annotated[int, Field(ge=0)]


class HoneytokenPage(ReadModel):
    items: list[HoneytokenRead]
    next_cursor: str | None


class HoneytokenTrigger(ReadModel):
    at: datetime
    source: Endpoint | None
    session_id: UUID | None
    event_refs: Annotated[list[EventRef], Field(min_length=1, max_length=1)]


class TriggerPage(ReadModel):
    items: list[HoneytokenTrigger]
    next_cursor: str | None


class Window(ReadModel):
    from_: datetime = Field(alias="from")
    to: datetime


class AlertRead(ReadModel):
    id: UUID
    rule: Literal["auth.bruteforce"]
    rule_version: Annotated[int, Field(ge=1)]
    severity: Literal["high"]
    state: Literal["open"]
    title: str
    window: Window
    count: Annotated[int, Field(ge=5)]
    deployment_id: UUID
    session_id: UUID | None
    source_ip: str
    created_at: datetime
    updated_at: datetime
    event_refs: Annotated[list[EventRef], Field(max_length=100)]
    event_refs_truncated: bool


class AlertPage(ReadModel):
    items: list[AlertRead]
    next_cursor: str | None
