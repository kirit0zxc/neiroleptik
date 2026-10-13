"""Event v1 и пакет доставки; JSON Schema генерируется из этих моделей."""

import json
import re
from datetime import UTC, datetime
from ipaddress import ip_address
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

MAX_DATA_BYTES = 8192
MAX_BATCH_BYTES = 256 * 1024
MAX_BATCH_EVENTS = 100
Port = Annotated[StrictInt, Field(ge=1, le=65535)]
EventType = Literal[
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
RejectionCode = Literal[
    "INVALID_EVENT",
    "EVENT_ID_CONFLICT",
    "DEPLOYMENT_MISMATCH",
    "INSTANCE_NOT_REGISTERED",
    "TEMPORARILY_UNAVAILABLE",
]
RETRYABLE_CODES = frozenset({"INSTANCE_NOT_REGISTERED", "TEMPORARILY_UNAVAILABLE"})


def compact_json(value: object) -> str:
    """Каноническая запись нужна для сравнения повторов и подсчёта UTF-8 байтов."""
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


class ContractModel(BaseModel):
    """Лишние поля отклоняются, чтобы агент не подменял серверные метаданные."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class Source(ContractModel):
    ip: Annotated[StrictStr, Field(json_schema_extra={"format": "ipvanyaddress"})]
    port: Annotated[StrictInt, Field(ge=0, le=65535)]

    @field_validator("ip")
    @classmethod
    def validate_ip(cls, value: str) -> str:
        """Строгая строка исключает преобразование boolean и чисел в выдуманный IP."""
        return str(ip_address(value))


class Service(ContractModel):
    protocol: Literal["tcp", "http", "ssh"]
    port: Port


class Event(ContractModel):
    schema_version: Annotated[StrictInt, Field(ge=1, le=1)] = 1
    event_id: UUID
    agent_id: UUID
    deployment_id: UUID
    instance_id: UUID
    session_id: UUID | None = None
    source_seq: Annotated[StrictInt, Field(ge=0, le=9007199254740991)]
    occurred_at: AwareDatetime
    type: EventType
    source: Source | None
    service: Service
    data: dict[str, JsonValue]
    artifact_ids: list[UUID]
    profile_revision: Annotated[StrictInt, Field(ge=1)]

    @field_validator("artifact_ids")
    @classmethod
    def unique_artifacts(cls, value: list[UUID]) -> list[UUID]:
        if len(value) != len(set(value)):
            raise ValueError("artifact_ids содержит повторы")
        return value

    @field_validator("occurred_at", mode="before")
    @classmethod
    def reject_numeric_timestamp(cls, value: object) -> object:
        """Контракт использует ISO 8601, не неявное угадывание секунд/миллисекунд."""
        if isinstance(value, (int, float, bool)):
            raise ValueError("occurred_at должен быть ISO 8601 с часовым поясом")
        if isinstance(value, str) and not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})",
            value,
        ):
            raise ValueError(
                "occurred_at должен быть ISO 8601 с timezone и точностью до микросекунд"
            )
        return value

    @field_validator("occurred_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        try:
            normalized = value.astimezone(UTC)
        except OverflowError:
            raise ValueError("Время выходит за диапазон UTC") from None
        # Конец фиксированного минутного окна тоже должен помещаться в datetime.
        if normalized >= datetime(9999, 12, 31, 23, 59, tzinfo=UTC):
            raise ValueError("Время не допускает представимый конец минутного окна")
        return normalized

    @field_validator("data")
    @classmethod
    def validate_data(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        if len(compact_json(value).encode("utf-8")) > MAX_DATA_BYTES:
            raise ValueError("data превышает 8192 байта UTF-8")
        return value

    def canonical_json(self) -> str:
        return compact_json(self.model_dump(mode="json"))


class Batch(ContractModel):
    schema_version: Annotated[StrictInt, Field(ge=1, le=1)] = 1
    batch_id: UUID = Field(default_factory=uuid4)
    events: Annotated[list[Event], Field(min_length=1, max_length=MAX_BATCH_EVENTS)]

    @model_validator(mode="after")
    def validate_batch(self) -> "Batch":
        if len({event.agent_id for event in self.events}) != 1:
            raise ValueError("Один batch принадлежит одному агенту")
        if len({event.event_id for event in self.events}) != len(self.events):
            raise ValueError("event_id повторяется в batch")
        if len(self.to_bytes()) > MAX_BATCH_BYTES:
            raise ValueError("Batch превышает 256 КиБ")
        return self

    def to_bytes(self) -> bytes:
        return compact_json(self.model_dump(mode="json")).encode("utf-8")


class RejectedEvent(ContractModel):
    """Позиция связывает отказ с исходным событием даже при неразобранном ID."""

    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                {
                    "if": {
                        "properties": {"code": {"enum": [code for code in sorted(RETRYABLE_CODES)]}}
                    },
                    "then": {"properties": {"retryable": {"const": True}}},
                    "else": {"properties": {"retryable": {"const": False}}},
                },
                {
                    "if": {"properties": {"event_id": {"type": "null"}}},
                    "then": {"properties": {"code": {"const": "INVALID_EVENT"}}},
                },
            ]
        }
    )

    index: Annotated[StrictInt, Field(ge=0)]
    event_id: UUID | None
    code: RejectionCode
    retryable: StrictBool

    @model_validator(mode="after")
    def validate_rejection(self) -> "RejectedEvent":
        if self.retryable != (self.code in RETRYABLE_CODES):
            raise ValueError("Код отказа не соответствует retryable")
        if self.event_id is None and self.code != "INVALID_EVENT":
            raise ValueError("null event_id допустим только для INVALID_EVENT")
        return self


class Ack(ContractModel):
    batch_id: UUID
    accepted_ids: list[UUID]
    rejected: list[RejectedEvent]

    @model_validator(mode="after")
    def validate_ids(self) -> "Ack":
        rejected_ids = [item.event_id for item in self.rejected if item.event_id]
        if len(self.accepted_ids) != len(set(self.accepted_ids)) or len(rejected_ids) != len(
            set(rejected_ids)
        ):
            raise ValueError("ACK содержит повторяющиеся ID")
        # Пересечение результатов проверяет spool после разрешения исходного index:
        # только batch позволяет одинаково проверить UUID и null ID.
        if len({item.index for item in self.rejected}) != len(self.rejected):
            raise ValueError("ACK содержит повторяющиеся индексы")
        return self
