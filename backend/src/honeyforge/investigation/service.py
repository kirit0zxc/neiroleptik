"""Read API читает проекции и неизменяемые основания в коротком SQL snapshot."""

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, func, literal, select, tuple_

from honeyforge.common import DomainError, utcnow
from honeyforge.investigation.iocs import IocExporter
from honeyforge.investigation.tables import InvestigationTables
from honeyforge.telemetry.presentation import PublicEventPresenter
from honeyforge.telemetry.read_contract import PublishedEvent
from honeyforge.telemetry.tables import TelemetryTables


class InvestigationService:
    """Изменяемые сущности имеют слабую keyset пагинацию без обещания снимка."""

    def __init__(
        self,
        engine: Engine,
        telemetry: TelemetryTables,
        tables: InvestigationTables,
        presenter: PublicEventPresenter,
        key: bytes,
    ) -> None:
        self.engine, self.telemetry, self.tables = engine, telemetry, tables
        self.presenter, self.key = presenter, key

    def _cursor(self, scope: str, cursor: str | None) -> tuple[Any, ...] | None:
        """Один формат подписи для страниц в памяти и SQL keyset."""
        if cursor is not None:
            try:
                if len(cursor) > 4096:
                    raise ValueError
                raw = base64.b64decode(
                    cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True
                )
                if not hmac.compare_digest(raw[:32], hmac.digest(self.key, raw[32:], "sha256")):
                    raise ValueError
                data = json.loads(raw[32:])
                if data["scope"] != scope:
                    raise ValueError
                return tuple(data["last"])
            except (ValueError, KeyError, TypeError):
                raise DomainError(422, "Некорректный курсор", "INVALID_CURSOR") from None
        return None

    def page(
        self, values: list[dict[str, Any]], scope: str, cursor: str | None, limit: int
    ) -> dict[str, Any]:
        if not 1 <= limit <= 200:
            raise DomainError(422, "Некорректный размер страницы")
        before = self._cursor(scope, cursor)
        if before is not None:
            values = [v for v in values if tuple(v["_key"]) < before]
        values.sort(key=lambda v: tuple(v["_key"]), reverse=True)
        visible = values[:limit]
        next_cursor = None
        if len(values) > limit:
            payload = json.dumps(
                {"scope": scope, "last": visible[-1]["_key"]}, separators=(",", ":")
            ).encode()
            next_cursor = (
                base64.urlsafe_b64encode(hmac.digest(self.key, payload, hashlib.sha256) + payload)
                .decode()
                .rstrip("=")
            )
        return {
            "items": [{k: v for k, v in row.items() if k != "_key"} for row in visible],
            "next_cursor": next_cursor,
        }

    def alerts(
        self,
        deployment: UUID | None = None,
        state: str | None = None,
        severity: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        if state not in {None, "open"} or severity not in {None, "high"}:
            raise DomainError(422, "MVP допускает только open/high")
        t, e = self.tables, self.telemetry.events
        statement = select(t.groups).where(t.groups.c.count >= 5)
        if deployment:
            statement = statement.where(t.groups.c.deployment_id == deployment)
        result = []
        with self.engine.connect().execution_options(isolation_level="REPEATABLE READ") as db:
            for row in db.execute(statement).mappings():
                refs = [
                    dict(agent_id=str(r.agent_id), event_id=str(r.event_id))
                    for r in db.execute(
                        select(e.c.agent_id, e.c.event_id)
                        .join(t.bases, e.c.id == t.bases.c.event_pk)
                        .where(t.bases.c.group_id == row["id"])
                        .order_by(e.c.agent_id, e.c.event_id)
                        .limit(100)
                    )
                ]
                result.append(
                    dict(
                        id=row["id"],
                        rule=row["rule"],
                        rule_version=row["rule_version"],
                        deployment_id=row["deployment_id"],
                        source_ip=row["source_ip"],
                        session_id=row["session_id"],
                        count=row["count"],
                        created_at=row["created_at"],
                        updated_at=row["updated_at"],
                        state="open",
                        severity="high",
                        title=f"{row['count']} неудачных входов за 60 с",
                        window={
                            "from": row["window_start"],
                            "to": row["window_start"] + timedelta(seconds=60),
                        },
                        event_refs=refs,
                        event_refs_truncated=row["count"] > 100,
                        _key=[row["created_at"].isoformat(), str(row["id"])],
                    )
                )
        return self.page(result, f"alerts:{deployment}:{state}:{severity}", cursor, limit)

    def artifacts(
        self, session_id: UUID | None, deployment: UUID | None, cursor: str | None, limit: int
    ) -> dict[str, Any]:
        t = self.tables.artifacts
        if not 1 <= limit <= 200:
            raise DomainError(422, "Некорректный размер страницы")
        scope = f"artifacts:{session_id}:{deployment}"
        before = self._cursor(scope, cursor)
        # Бинарное содержимое не входит даже в SQL SELECT метаданных.
        statement = select(*(column for column in t.c if column.name != "content"))
        if session_id:
            statement = statement.where(t.c.session_id == session_id)
        if deployment:
            statement = statement.where(t.c.deployment_id == deployment)
        if before is not None:
            try:
                if len(before) != 2:
                    raise ValueError
                stamp, identity = datetime.fromisoformat(before[0]), UUID(before[1])
                if stamp.tzinfo is None:
                    raise ValueError
            except (ValueError, TypeError, AttributeError):
                raise DomainError(422, "Некорректный курсор", "INVALID_CURSOR") from None
            statement = statement.where(
                tuple_(t.c.observed_at, t.c.id) < tuple_(literal(stamp), literal(identity))
            )
        statement = statement.order_by(t.c.observed_at.desc(), t.c.id.desc()).limit(limit + 1)
        with self.engine.connect() as db:
            values = [dict(row) for row in db.execute(statement).mappings()]
        now = utcnow()
        for value in values:
            value["state"] = self._artifact_state(value, now)
            value["source_url"] = IocExporter.safe_url(value["source_url"])
            value["_key"] = [value["observed_at"].astimezone(UTC).isoformat(), str(value["id"])]
        return self.page(values, scope, None, limit)

    @staticmethod
    def _artifact_state(row: Any, now: datetime) -> str:
        """Срок имеет одинаковый приоритет в метаданных и скачивании."""
        if row["expires_at"] is not None and row["expires_at"] <= now:
            return "expired"
        return str(row["state"])

    def download(self, identity: UUID) -> bytes:
        t = self.tables.artifacts
        with self.engine.connect() as db:
            row = db.execute(select(t).where(t.c.id == identity)).mappings().first()
        if row is None:
            raise DomainError(404, "Артефакт не найден")
        state = self._artifact_state(row, utcnow())
        if state != "available":
            raise DomainError(
                410 if state == "expired" else 409,
                "Содержимое недоступно",
                f"ARTIFACT_{state.upper()}",
            )
        if row["content"] is None or row["sha256"] is None or row["size_bytes"] is None:
            raise DomainError(409, "Содержимое ещё не собрано", "ARTIFACT_PENDING")
        return bytes(row["content"])

    def artifact(self, identity: UUID) -> dict[str, Any]:
        t = self.tables.artifacts
        with self.engine.connect() as db:
            row = (
                db.execute(
                    select(*(column for column in t.c if column.name != "content")).where(
                        t.c.id == identity
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise DomainError(404, "Артефакт не найден")
        result = dict(row)
        result["state"] = self._artifact_state(result, utcnow())
        result["source_url"] = IocExporter.safe_url(result["source_url"])
        return result

    def tokens(self, deployment: UUID | None, cursor: str | None, limit: int) -> dict[str, Any]:
        t, e = self.tables, self.telemetry.events
        result = []
        with self.engine.connect().execution_options(isolation_level="REPEATABLE READ") as db:
            statement = select(t.tokens)
            if deployment:
                statement = statement.where(t.tokens.c.deployment_id == deployment)
            for row in db.execute(statement).mappings():
                count, last = db.execute(
                    select(func.count(), func.max(e.c.occurred_at))
                    .join(t.triggers, e.c.id == t.triggers.c.event_pk)
                    .where(
                        t.triggers.c.token_id == row["id"],
                        t.triggers.c.deployment_id == row["deployment_id"],
                    )
                ).one()
                result.append(
                    dict(
                        id=row["id"],
                        deployment_id=row["deployment_id"],
                        kind="url",
                        label=str(row["id"]),
                        created_at=row["created_at"],
                        last_triggered_at=last,
                        trigger_count=count,
                        _key=[row["created_at"].isoformat(), str(row["id"])],
                    )
                )
        return self.page(result, f"tokens:{deployment}", cursor, limit)

    def triggers(self, identity: UUID, cursor: str | None, limit: int) -> dict[str, Any]:
        e, t = self.telemetry.events, self.tables.triggers
        with self.engine.connect() as db:
            if (
                db.execute(
                    select(self.tables.tokens.c.id).where(self.tables.tokens.c.id == identity)
                ).first()
                is None
            ):
                raise DomainError(404, "Токен не найден")
            rows = (
                db.execute(
                    select(e).join(t, e.c.id == t.c.event_pk).where(t.c.token_id == identity)
                )
                .mappings()
                .all()
            )
        result = []
        for row in rows:
            event = json.loads(bytes(row["canonical_bytes"]))
            result.append(
                dict(
                    at=row["occurred_at"],
                    source=event["source"],
                    session_id=event.get("session_id"),
                    event_refs=[dict(agent_id=event["agent_id"], event_id=event["event_id"])],
                    _key=[row["occurred_at"].isoformat(), event["agent_id"], event["event_id"]],
                )
            )
        return self.page(result, f"triggers:{identity}", cursor, limit)

    def lookup(self, refs: list[tuple[UUID, UUID]]) -> dict[str, Any]:
        e, p = self.telemetry.events, self.telemetry.publications
        items, unavailable = [], []
        with self.engine.connect().execution_options(isolation_level="REPEATABLE READ") as db:
            for agent, event in dict.fromkeys(refs):
                row = (
                    db.execute(
                        select(e, p.c.stream_seq)
                        .join(p, e.c.id == p.c.event_pk)
                        .where(e.c.agent_id == agent, e.c.event_id == event)
                    )
                    .mappings()
                    .first()
                )
                if row is None:
                    unavailable.append(dict(agent_id=str(agent), event_id=str(event)))
                else:
                    items.append(
                        self.presenter.present(
                            PublishedEvent(
                                row["stream_seq"],
                                row["received_at"],
                                json.loads(bytes(row["canonical_bytes"])),
                            )
                        )
                    )
        return dict(items=items, unavailable=unavailable)

    def csv(self, start: datetime, end: datetime, deployment: UUID | None) -> bytes:
        start, end = IocExporter.period(start, end)
        e, p = self.telemetry.events, self.telemetry.publications
        statement = (
            select(e.c.canonical_bytes)
            .join(p, e.c.id == p.c.event_pk)
            .where(e.c.occurred_at >= start, e.c.occurred_at < end)
        )
        if deployment:
            statement = statement.where(e.c.deployment_id == deployment)
        with self.engine.connect().execution_options(isolation_level="REPEATABLE READ") as db:
            rows = db.execution_options(stream_results=True, yield_per=500).execute(statement)
            events = (json.loads(bytes(v)) for v in rows.scalars())
            # Подсчёт и файл используют один cursor/snapshot; оригиналы не копятся в памяти.
            return IocExporter().render(events)

    def top_sources(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        """Неизвестный IP участвует в истории, но не создаёт строку топа источников."""
        e, p = self.telemetry.events, self.telemetry.publications
        count = func.count().label("count")
        statement = (
            select(e.c.source_ip, count)
            .join(p, e.c.id == p.c.event_pk)
            .where(e.c.source_ip.is_not(None), e.c.occurred_at >= start, e.c.occurred_at < end)
            .group_by(e.c.source_ip)
            .order_by(count.desc(), e.c.source_ip)
            .limit(5)
        )
        with self.engine.connect() as db:
            return [
                dict(ip=row["source_ip"], count=row["count"])
                for row in db.execute(statement).mappings()
            ]
