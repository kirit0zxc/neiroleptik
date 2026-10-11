"""Проекции обновляются в транзакции publisher под общей блокировкой журнала."""

import json
from datetime import UTC, datetime
from ipaddress import ip_address
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from honeyforge.investigation.iocs import IocExporter
from honeyforge.investigation.tables import InvestigationTables
from honeyforge.telemetry.tables import TelemetryTables


class InvestigationProjector:
    """Обработанность задаётся publication/outbox; rollback откатывает и проекции."""

    def __init__(
        self, telemetry: TelemetryTables, tables: InvestigationTables, rule_version: int = 1
    ) -> None:
        self.telemetry, self.tables, self.rule_version = telemetry, tables, rule_version

    def apply(self, session: Session, event_pk: int, now: datetime) -> None:
        row = (
            session.execute(
                select(self.telemetry.events).where(self.telemetry.events.c.id == event_pk)
            )
            .mappings()
            .one()
        )
        event = json.loads(bytes(row["canonical_bytes"]))
        source = event["source"]
        if (
            event["type"] == "auth.attempt"
            and event["data"].get("success") is False
            and source is not None
        ):
            self._alert(session, event_pk, event, now)
        if event["type"] == "token.triggered":
            self._token(session, event_pk, event, now)
        if event["type"] == "artifact.observed":
            self._artifacts(session, event, now)

    def _alert(self, session: Session, event_pk: int, event: dict[str, Any], now: datetime) -> None:
        groups = self.tables.groups
        start = (
            datetime.fromisoformat(event["occurred_at"])
            .astimezone(UTC)
            .replace(second=0, microsecond=0)
        )
        key = dict(
            rule="auth.bruteforce",
            rule_version=self.rule_version,
            deployment_id=UUID(event["deployment_id"]),
            source_ip=str(ip_address(event["source"]["ip"])),
            window_start=start,
        )
        identity = uuid4()
        session.execute(
            pg_insert(groups)
            .values(
                id=identity,
                **key,
                count=0,
                session_id=event.get("session_id"),
                created_at=None,
                updated_at=now,
            )
            .on_conflict_do_nothing(constraint="uq_alert_group")
        )
        group = (
            session.execute(
                select(groups)
                .where(*(groups.c[name] == value for name, value in key.items()))
                .with_for_update()
            )
            .mappings()
            .one()
        )
        # База уникальна независимо от повтора вызова, а не только доставки.
        added = session.execute(
            pg_insert(self.tables.bases)
            .values(event_pk=event_pk, group_id=group["id"])
            .on_conflict_do_nothing()
            .returning(self.tables.bases.c.event_pk)
        ).scalar_one_or_none()
        if added is None:
            return
        count = group["count"] + 1
        known_session = event.get("session_id")
        session_id = group["session_id"]
        if count == 1:
            session_id = UUID(known_session) if known_session else None
        elif known_session is None or str(session_id) != known_session:
            session_id = None
        session.execute(
            update(groups)
            .where(groups.c.id == group["id"])
            .values(
                count=count,
                session_id=session_id,
                created_at=group["created_at"] or (now if count >= 5 else None),
                updated_at=now,
            )
        )

    def _token(self, session: Session, event_pk: int, event: dict[str, Any], now: datetime) -> None:
        if event["data"].get("kind", "url") != "url":
            return
        try:
            identity = UUID(event["data"]["token_id"])
        except (KeyError, ValueError, TypeError, AttributeError):
            return
        deployment = UUID(event["deployment_id"])
        session.execute(
            pg_insert(self.tables.tokens)
            .values(id=identity, deployment_id=deployment, created_at=now)
            .on_conflict_do_nothing()
        )
        owner = session.execute(
            select(self.tables.tokens.c.deployment_id).where(self.tables.tokens.c.id == identity)
        ).scalar_one()
        if owner != deployment:
            return
        session.execute(
            pg_insert(self.tables.triggers)
            .values(event_pk=event_pk, token_id=identity, deployment_id=deployment)
            .on_conflict_do_nothing()
        )

    def _artifacts(self, session: Session, event: dict[str, Any], now: datetime) -> None:
        ref = {"agent_id": event["agent_id"], "event_id": event["event_id"]}
        for value in event["artifact_ids"]:
            identity = UUID(value)
            existing = (
                session.execute(
                    select(self.tables.artifacts)
                    .where(self.tables.artifacts.c.id == identity)
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            if existing:
                if str(existing["deployment_id"]) != event["deployment_id"]:
                    continue
                refs = sorted(
                    existing["event_refs"] + [ref], key=lambda r: (r["agent_id"], r["event_id"])
                )
                refs = list({(r["agent_id"], r["event_id"]): r for r in refs}.values())
                session.execute(
                    update(self.tables.artifacts)
                    .where(self.tables.artifacts.c.id == identity)
                    .values(
                        event_refs=refs[:100],
                        event_refs_truncated=(existing["event_refs_truncated"] or len(refs) > 100),
                    )
                )
                continue
            name = event["data"].get("name")
            session.execute(
                insert(self.tables.artifacts).values(
                    id=identity,
                    deployment_id=event["deployment_id"],
                    session_id=event.get("session_id"),
                    state="pending",
                    name=name if isinstance(name, str) else str(identity),
                    source_url=IocExporter.safe_url(event["data"].get("source_url")),
                    observed_at=datetime.fromisoformat(event["occurred_at"]),
                    event_refs=[ref],
                    event_refs_truncated=False,
                )
            )
