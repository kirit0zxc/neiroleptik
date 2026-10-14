"""Поведение MVP на настоящем HTTP, авторизации и изолированной PostgreSQL."""

import base64
import csv
import io
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from agent.telemetry.low_journal.models import JournalBinding, JournalRecord
from agent.telemetry.low_journal.production import LowProductionMapper
from agent.telemetry.schemas import Event
from conftest import login, profile_payload, register_host
from fastapi.testclient import TestClient
from sqlalchemy import event as sql_event
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from honeyforge.common import DomainError
from honeyforge.investigation.iocs import IocExporter
from honeyforge.investigation.projector import InvestigationProjector
from honeyforge.investigation.service import InvestigationService
from honeyforge.main import create_app
from honeyforge.models import Deployment, RuntimeInstance
from honeyforge.telemetry.masking import PublicPolicy
from honeyforge.telemetry.presentation import PublicEventPresenter
from honeyforge.telemetry.publisher import OutboxPublisher
from honeyforge.telemetry.storage import EventStorage
from honeyforge.telemetry.types import PreparedEvent


@pytest.fixture
def investigation(settings, engine, actors):
    app = create_app(settings)
    runtime = app.state.telemetry
    with engine.begin() as db:
        names = ",".join(
            'hf_telemetry."' + t.name + '"' for t in runtime.tables.metadata.sorted_tables
        )
        db.execute(text(f"TRUNCATE {names} CASCADE"))
        db.execute(runtime.tables.state.insert().values(id=1, last_seq=0))
    # Без lifespan publisher вызывается явно для проверки границ commit.
    client = TestClient(app, headers={"Origin": "http://localhost:5173"})
    login(client)
    host = register_host(client)
    profile = client.post("/api/v1/profiles", json=profile_payload()).json()
    deployment = client.post(
        "/api/v1/deployments",
        json={"host_id": host["host_id"], "profile_id": profile["id"], "revision": 1},
    ).json()
    instance = uuid4()
    with Session(engine) as db:
        stored = db.get(Deployment, UUID(deployment["id"]))
        db.add(
            RuntimeInstance(
                id=instance, deployment_id=stored.id, profile_revision_id=stored.profile_revision_id
            )
        )
        db.commit()
    yield client, runtime, host, deployment, instance
    client.close()
    app.state.engine.dispose()


def document(ctx, **changes):
    _, _, host, deployment, instance = ctx
    value = dict(
        schema_version=1,
        event_id=str(uuid4()),
        agent_id=host["host_id"],
        deployment_id=deployment["id"],
        instance_id=str(instance),
        session_id=None,
        source_seq=1,
        occurred_at="2026-10-11T12:00:10Z",
        type="auth.attempt",
        source={"ip": "2001:db8::1", "port": 2345},
        service={"protocol": "http", "port": 8080},
        data={"success": False},
        artifact_ids=[],
        profile_revision=1,
    )
    return value | changes


def post(ctx, events):
    client, _, host, *_ = ctx
    return client.post(
        "/agent/v1/events",
        headers={"Authorization": "Bearer " + host["agent_token"]},
        json={"schema_version": 1, "batch_id": str(uuid4()), "events": events},
    )


def store(ctx, events):
    response = post(ctx, events)
    assert response.status_code == 200, response.text
    assert not response.json()["rejected"], response.text
    assert ctx[1].publisher.publish_once(500) == len(events)


def service(ctx, engine):
    r = ctx[1]
    return InvestigationService(
        engine, r.tables, r.investigation, PublicEventPresenter(PublicPolicy()), b"x" * 32
    )


@pytest.mark.parametrize(
    "role,download_status", [("viewer", 403), ("operator", 200), ("admin", 200)]
)
def test_role_matrix_http(investigation, engine, role, download_status):
    ctx = investigation
    artifact = uuid4()
    store(ctx, [document(ctx, type="artifact.observed", artifact_ids=[str(artifact)])])
    with engine.begin() as db:
        db.execute(
            update(ctx[1].investigation.artifacts).values(
                state="available", content=b"proof", sha256="a" * 64, size_bytes=5
            )
        )
    client = ctx[0]
    client.cookies.clear()
    assert client.get(f"/api/v1/artifacts/{artifact}/content").status_code == 401
    assert client.get("/api/v1/artifacts").status_code == 401
    assert (
        client.get("/api/v1/iocs.csv?from=2026-10-11T00:00:00Z&to=2026-10-12T00:00:00Z").status_code
        == 401
    )
    login(client, role)
    assert client.get("/api/v1/artifacts").json()["items"][0]["event_refs_truncated"] is False
    assert client.get(f"/api/v1/artifacts/{artifact}").status_code == 200
    assert client.get("/api/v1/events").status_code == 200
    ref = client.get("/api/v1/events").json()["items"][0]
    assert (
        client.post(
            "/api/v1/events/lookup",
            json={"refs": [{"agent_id": ref["agent_id"], "event_id": ref["event_id"]}]},
        ).status_code
        == 200
    )
    response = client.get(f"/api/v1/artifacts/{artifact}/content")
    assert response.status_code == download_status
    if download_status == 200:
        assert response.content == b"proof"
        assert response.headers["content-disposition"].startswith("attachment;")
    csv_response = client.get(
        "/api/v1/iocs.csv", params={"from": "2026-10-11T00:00:00Z", "to": "2026-10-12T00:00:00Z"}
    )
    assert csv_response.status_code == 200
    assert b"success" not in csv_response.content


@pytest.mark.parametrize("state,status", [("pending", 409), ("failed", 409), ("expired", 410)])
def test_artifact_states(investigation, engine, state, status):
    ctx = investigation
    artifact = uuid4()
    store(ctx, [document(ctx, type="artifact.observed", artifact_ids=[str(artifact)])])
    with engine.begin() as db:
        db.execute(update(ctx[1].investigation.artifacts).values(state=state))
    response = ctx[0].get(f"/api/v1/artifacts/{artifact}/content")
    assert response.status_code == status
    assert response.json()["code"] == "ARTIFACT_" + state.upper()


def test_artifact_nul_name_does_not_block_publication(investigation, engine):
    ctx = investigation
    artifact = uuid4()
    original = document(
        ctx, type="artifact.observed", artifact_ids=[str(artifact)], data={"name": "a\x00b.txt"}
    )
    store(ctx, [original, document(ctx)])
    assert ctx[1].publisher.publish_once(500) == 0
    assert len(ctx[0].get("/api/v1/events").json()["items"]) == 2
    assert ctx[0].get(f"/api/v1/artifacts/{artifact}").json()["name"] == "a\ufffdb.txt"
    with engine.connect() as db:
        raw = db.scalar(
            select(ctx[1].tables.events.c.canonical_bytes).where(
                ctx[1].tables.events.c.event_id == UUID(original["event_id"])
            )
        )
    assert json.loads(bytes(raw))["data"]["name"] == "a\x00b.txt"


@pytest.mark.parametrize("stored_state", ["available", "pending", "failed", "expired"])
@pytest.mark.parametrize("offset", [-1, 0, 1, None])
def test_artifact_expiration_consistent(investigation, engine, monkeypatch, stored_state, offset):
    ctx = investigation
    artifact = uuid4()
    store(ctx, [document(ctx, type="artifact.observed", artifact_ids=[str(artifact)])])
    now = datetime(2026, 10, 11, 12, 30, tzinfo=UTC)
    monkeypatch.setattr("honeyforge.investigation.service.utcnow", lambda: now)
    with engine.begin() as db:
        db.execute(
            update(ctx[1].investigation.artifacts).values(
                state=stored_state,
                content=b"proof",
                sha256="a" * 64,
                size_bytes=5,
                expires_at=None if offset is None else now + timedelta(seconds=offset),
            )
        )
    expected = "expired" if offset is not None and offset <= 0 else stored_state
    client = ctx[0]
    assert client.get("/api/v1/artifacts").json()["items"][0]["state"] == expected
    assert client.get(f"/api/v1/artifacts/{artifact}").json()["state"] == expected
    download = client.get(f"/api/v1/artifacts/{artifact}/content")
    assert download.status_code == (
        200 if expected == "available" else 410 if expected == "expired" else 409
    )
    if expected != "available":
        assert download.json()["code"] == "ARTIFACT_" + expected.upper()


def test_artifact_metadata_sql_keyset(investigation, engine):
    ctx = investigation
    identities = [UUID(int=n) for n in (1, 2, 3, 4)]
    session_id = str(uuid4())
    store(
        ctx,
        [
            document(ctx, type="artifact.observed", session_id=session_id, artifact_ids=[str(v)])
            for v in identities
        ]
        + [document(ctx, type="artifact.observed", artifact_ids=[str(uuid4())])],
    )
    with engine.begin() as db:
        db.execute(update(ctx[1].investigation.artifacts).values(content=b"x" * 100_000))
    statements = []

    def capture(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().startswith("SELECT") and "hf_telemetry.artifacts" in statement:
            statements.append((statement, parameters))

    sql_event.listen(engine, "before_cursor_execute", capture)
    try:
        read = service(ctx, engine)
        page = read.artifacts(UUID(session_id), UUID(ctx[3]["id"]), None, 1)
        seen = []
        while True:
            seen.extend(v["id"] for v in page["items"])
            if page["next_cursor"] is None:
                break
            page = read.artifacts(UUID(session_id), UUID(ctx[3]["id"]), page["next_cursor"], 1)
        assert seen == list(reversed(identities))
        assert read.artifact(identities[0])["id"] == identities[0]
    finally:
        sql_event.remove(engine, "before_cursor_execute", capture)
    assert len(statements) == 5
    for statement, _parameters in statements:
        assert "content" not in statement
    for statement, parameters in statements[:4]:
        assert "ORDER BY" in statement and "LIMIT" in statement
        assert 2 in parameters.values()
    assert "observed_at, hf_telemetry.artifacts.id) <" in statements[1][0]
    first = read.artifacts(UUID(session_id), None, None, 1)
    with pytest.raises(DomainError) as error:
        read.artifacts(None, None, first["next_cursor"], 1)
    assert error.value.code == "INVALID_CURSOR"


@pytest.mark.parametrize("scope_size,valid", [(37, True), (38, False), (200, False)])
def test_scoped_ipv6_rejection_is_per_event(investigation, scope_size, valid):
    ctx = investigation
    scoped = document(ctx, source={"ip": "fe80::1%" + "a" * scope_size, "port": 1})
    good = document(ctx)
    response = post(ctx, [scoped, good])
    assert response.status_code == 200, response.text
    ack = response.json()
    assert good["event_id"] in ack["accepted_ids"]
    if valid:
        assert scoped["event_id"] in ack["accepted_ids"]
        assert ack["rejected"] == []
    else:
        assert scoped["event_id"] not in ack["accepted_ids"]
        assert len(ack["rejected"]) == 1
        assert ack["rejected"][0]["code"] == "INVALID_EVENT"
    assert ctx[1].publisher.publish_once(500) == (2 if valid else 1)


def test_null_source_ingest_db_rest_ws_lookup_timeline(investigation, engine, settings):
    ctx = investigation
    session = str(uuid4())
    event = document(
        ctx, source=None, session_id=session, data={"success": False, "password": "closed-password"}
    )
    original = document(ctx)
    store(ctx, [event, original])
    with engine.connect() as db:
        rows = db.execute(select(ctx[1].tables.events)).mappings().all()
        row = next(r for r in rows if str(r["event_id"]) == event["event_id"])
        assert row["source_ip"] is None
        assert json.loads(bytes(row["canonical_bytes"]))["source"] is None
    client = ctx[0]
    history = client.get("/api/v1/events").json()["items"]
    assert len(history) == 2
    assert next(e for e in history if e["event_id"] == event["event_id"])["source"] is None
    filtered = client.get("/api/v1/events", params={"source_ip": "2001:db8::1"}).json()
    assert len(filtered["items"]) == 1
    timeline = client.get(
        "/api/v1/events", params={"session_id": session, "sort": "occurred_at", "order": "asc"}
    ).json()
    assert len(timeline["items"]) == 1
    lookup = client.post(
        "/api/v1/events/lookup",
        json={"refs": [{"agent_id": event["agent_id"], "event_id": event["event_id"]}]},
    ).json()
    assert lookup["items"] == timeline["items"]
    assert "closed-password" not in json.dumps(lookup)
    assert lookup["items"][0]["data"]["password_present"] is True
    with client.websocket_connect("/api/v1/stream") as socket:
        assert socket.receive_json()["type"] == "hello"
        second = document(ctx, source=None)
        store(ctx, [second])
        live = socket.receive_json()
        assert live["event"]["source"] is None
    assert client.get("/api/v1/alerts").json()["items"] == []


@pytest.mark.parametrize(
    "source", ["missing", {}, {"ip": "bad", "port": 1}, {"ip": "192.0.2.1", "port": -1}]
)
def test_invalid_source_rejected(investigation, source):
    event = document(investigation)
    if source == "missing":
        del event["source"]
    else:
        event["source"] = source
    response = post(investigation, [event])
    assert response.status_code == 200
    assert response.json()["rejected"][0]["code"] == "INVALID_EVENT"
    assert response.json()["accepted_ids"] == []


def test_token_repeat_restart_secret_and_unknown_session(investigation, engine):
    ctx = investigation
    token = str(uuid4())
    event = document(
        ctx,
        type="token.triggered",
        data={"token_id": token, "url": "https://secret/token", "value": "hidden"},
    )
    store(ctx, [document(ctx, session_id=str(uuid4())), event])
    assert post(ctx, [event]).json()["accepted_ids"] == [event["event_id"]]
    assert ctx[1].publisher.publish_once() == 0
    restarted = OutboxPublisher(
        engine, ctx[1].tables, InvestigationProjector(ctx[1].tables, ctx[1].investigation)
    )
    assert restarted.publish_once() == 0
    values = ctx[0].get("/api/v1/honeytokens").json()
    assert values["items"][0]["trigger_count"] == 1
    assert values["items"][0]["kind"] == "url"
    assert "hidden" not in json.dumps(values)
    trigger = ctx[0].get(f"/api/v1/honeytokens/{token}/triggers").json()["items"][0]
    assert trigger["session_id"] is None
    assert trigger["event_refs"] == [{"agent_id": event["agent_id"], "event_id": event["event_id"]}]
    assert "secret" not in ctx[0].get("/api/v1/events").text


def test_alert_threshold_late_session_refs_and_restart(investigation, engine):
    ctx = investigation
    session = str(uuid4())
    events = [document(ctx, session_id=session) for _ in range(101)]
    store(ctx, events[:4])
    assert ctx[0].get("/api/v1/alerts").json()["items"] == []
    store(ctx, events[4:5])
    first = ctx[0].get("/api/v1/alerts").json()["items"][0]
    assert first["count"] == 5 and first["session_id"] == session
    store(ctx, events[5:6])
    sixth = ctx[0].get("/api/v1/alerts").json()["items"][0]
    assert sixth["id"] == first["id"] and sixth["count"] == 6
    assert sixth["created_at"] == first["created_at"]
    store(ctx, events[6:100])
    hundred = ctx[0].get("/api/v1/alerts").json()["items"][0]
    assert len(hundred["event_refs"]) == 100 and not hundred["event_refs_truncated"]
    events[100]["session_id"] = None
    store(ctx, events[100:])
    last = ctx[0].get("/api/v1/alerts").json()["items"][0]
    assert last["count"] == 101 and last["event_refs_truncated"]
    assert last["session_id"] is None and last["created_at"] == first["created_at"]
    assert (
        last["event_refs"]
        == sorted(
            [dict(agent_id=e["agent_id"], event_id=e["event_id"]) for e in events],
            key=lambda r: (r["agent_id"], r["event_id"]),
        )[:100]
    )
    assert post(ctx, events[:100]).status_code == 200
    assert (
        OutboxPublisher(
            engine, ctx[1].tables, InvestigationProjector(ctx[1].tables, ctx[1].investigation)
        ).publish_once()
        == 0
    )
    assert ctx[0].get("/api/v1/alerts").json()["items"][0]["count"] == 101


def test_alert_utc_boundaries_success_null_ip_and_different_sessions(investigation):
    ctx = investigation
    a, b = str(uuid4()), str(uuid4())
    events = [
        document(ctx, occurred_at="2026-10-11T15:00:59+03:00", session_id=a) for _ in range(4)
    ]
    store(ctx, events + [document(ctx, occurred_at="2026-10-11T12:01:00Z")])
    assert ctx[0].get("/api/v1/alerts").json()["items"] == []
    store(ctx, [document(ctx, data={"success": True}), document(ctx, source=None)])
    assert ctx[0].get("/api/v1/alerts").json()["items"] == []
    store(ctx, [document(ctx, occurred_at="2026-10-11T12:00:00Z", session_id=b)])
    alert = ctx[0].get("/api/v1/alerts").json()["items"][0]
    assert alert["count"] == 5 and alert["session_id"] is None
    assert alert["window"] == {"from": "2026-10-11T12:00:00Z", "to": "2026-10-11T12:01:00Z"}


def test_concurrent_publish_durable_counts(investigation, engine):
    ctx = investigation
    events = [document(ctx) for _ in range(30)]
    assert post(ctx, events).status_code == 200
    gate = Barrier(2)

    def run():
        gate.wait(timeout=10)
        return OutboxPublisher(
            engine, ctx[1].tables, InvestigationProjector(ctx[1].tables, ctx[1].investigation)
        ).publish_once(500)

    with ThreadPoolExecutor(2) as pool:
        jobs = [pool.submit(run) for _ in range(2)]
        assert sorted(j.result(timeout=20) for j in jobs) == [0, 30]
    assert ctx[0].get("/api/v1/alerts").json()["items"][0]["count"] == 30


def test_snapshot_timeline_cursor_and_filter_errors(investigation):
    ctx = investigation
    session = str(uuid4())
    store(
        ctx,
        [
            document(ctx, session_id=session, occurred_at=f"2026-10-11T12:00:0{i}Z")
            for i in range(3)
        ],
    )
    params = dict(session_id=session, sort="occurred_at", order="asc", limit=1)
    first = ctx[0].get("/api/v1/events", params=params).json()
    store(ctx, [document(ctx, session_id=session, occurred_at="2026-10-11T12:00:00Z")])
    second = ctx[0].get("/api/v1/events", params=params | {"cursor": first["next_cursor"]})
    assert second.status_code == 200
    assert datetime.fromisoformat(second.json()["items"][0]["occurred_at"]) == datetime(
        2026, 10, 11, 12, 0, 1, tzinfo=UTC
    )
    assert ctx[0].get("/api/v1/events", params={"cursor": "bad"}).json()["code"] == "INVALID_CURSOR"
    error = ctx[0].get("/api/v1/events", params={"source_ip": "bad"})
    assert error.status_code == 422 and "code" not in error.json()


def test_ioc_http_period_types_filter_and_no_secrets(investigation):
    ctx = investigation
    events = [
        document(
            ctx,
            type="artifact.observed",
            source=None,
            data={
                "sha256": "A" * 64,
                "source_url": "HTTPS://EXAMPLE.COM:443/",
                "username": "hidden",
                "user_agent": "hidden",
                "payload": "http://secret/",
            },
        ),
        document(ctx, source={"ip": "192.0.2.1", "port": 1}),
        document(
            ctx,
            type="token.triggered",
            data={"token_id": str(uuid4()), "source_url": "https://secret/token"},
        ),
    ]
    store(ctx, events)
    params = {"from": "2026-10-11T12:00:10Z", "to": "2026-10-11T12:00:11Z"}
    response = ctx[0].get("/api/v1/iocs.csv", params=params)
    assert response.status_code == 200
    assert response.content.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" in response.content
    assert b"hidden" not in response.content and b"secret" not in response.content
    rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert {r["indicator_type"] for r in rows} == {"url", "sha256", "ipv4", "ipv6"}
    assert "https://example.com/" in {r["indicator_value"] for r in rows}
    assert (
        ctx[0]
        .get("/api/v1/iocs.csv", params=params | {"deployment_id": str(uuid4())})
        .content.decode("utf-8-sig")
        .count("\r\n")
        == 1
    )
    assert (
        ctx[0].get("/api/v1/iocs.csv", params=params | {"to": "2026-10-11T12:00:10Z"}).status_code
        == 422
    )
    assert ctx[0].get("/api/v1/iocs.csv", params={"from": params["from"]}).status_code == 422
    assert (
        ctx[0]
        .get(
            "/api/v1/iocs.csv",
            params={"from": "2026-10-11T12:00:11Z", "to": "2026-10-12T00:00:00Z"},
        )
        .content.decode("utf-8-sig")
        .count("\r\n")
        == 1
    )


@pytest.mark.parametrize("days,status", [(31, 200), (32, 422)])
def test_ioc_31_day_bound(investigation, days, status):
    start = datetime(2026, 10, 1, tzinfo=UTC)
    assert (
        investigation[0]
        .get(
            "/api/v1/iocs.csv",
            params={"from": start.isoformat(), "to": (start + timedelta(days=days)).isoformat()},
        )
        .status_code
        == status
    )


def test_ioc_limit_dedup_sort_and_csv_cells():
    exporter = IocExporter()
    event = dict(
        type="auth.attempt",
        source={"ip": "192.0.2.1"},
        data={},
        agent_id=str(uuid4()),
        deployment_id=str(uuid4()),
        session_id=None,
        occurred_at="2026-10-11T12:00:00Z",
    )
    events = [event | {"event_id": str(UUID(int=i))} for i in range(100000)]
    content = exporter.render(events + events[:1])
    assert len(list(csv.reader(io.StringIO(content.decode("utf-8-sig"))))) == 100001
    with pytest.raises(DomainError) as error:
        exporter.render(events + [event | {"event_id": str(UUID(int=100000))}])
    assert error.value.status == 422
    for value in ("=1", "+1", "-1", "@x", " \t=1", "\r@x"):
        assert exporter.cell(value) == "'" + value
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    values = ["имя,два", '"кавычки"', "строка\nдва"]
    writer.writerow([exporter.cell(v) for v in values])
    assert next(csv.reader(io.StringIO(buffer.getvalue()))) == values


@pytest.mark.parametrize(
    "url",
    [
        "https://u:p@example.com/",
        "https://example.com/?token=x",
        "https://example.com/#x",
        "https://example.com/secret",
        "file:///etc/passwd",
        "https://example.com/?",
    ],
)
def test_sensitive_urls_are_not_exported(url):
    assert IocExporter.safe_url(url) is None


def test_packaged_agent_schema_matches_contract():
    from importlib.resources import files
    from pathlib import Path

    packaged = json.loads(
        files("honeyforge.telemetry")
        .joinpath("agent-event-v1.schema.json")
        .read_text(encoding="utf-8")
    )
    root = Path(__file__).resolve().parents[2]
    assert packaged == json.loads((root / "contracts/agent-event-v1.schema.json").read_text())


def prepared(events):
    return [
        PreparedEvent(
            i,
            e.agent_id,
            e.event_id,
            e.deployment_id,
            e.instance_id,
            e.profile_revision,
            e.canonical_json().encode(),
        )
        for i, e in enumerate(Event.model_validate(v) for v in events)
    ]


def test_same_event_id_different_agents_and_concurrent_delivery(investigation, engine):
    ctx = investigation
    token = str(uuid4())
    identity = str(uuid4())
    first = document(ctx, type="token.triggered", event_id=identity, data={"token_id": token})
    second = first | {"agent_id": str(uuid4())}
    batch = prepared([first, second])
    gate = Barrier(2)

    def run():
        gate.wait(timeout=10)
        return EventStorage(engine, ctx[1].tables).store(batch)

    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(run) for _ in range(2)]
        for future in futures:
            assert all(r.status == "accepted" for r in future.result(timeout=20))
    assert ctx[1].publisher.publish_once() == 2
    assert ctx[0].get("/api/v1/honeytokens").json()["items"][0]["trigger_count"] == 2
    refs = [
        v["event_refs"][0]
        for v in ctx[0].get(f"/api/v1/honeytokens/{token}/triggers").json()["items"]
    ]
    assert len({(r["agent_id"], r["event_id"]) for r in refs}) == 2


def test_alert_group_dimensions_and_rule_versions(investigation, engine):
    ctx = investigation
    cases = [
        {},
        {"source": {"ip": "192.0.2.55", "port": 9}},
        {"deployment_id": str(uuid4())},
        {"occurred_at": "2026-10-11T12:01:10Z"},
    ]
    events = [document(ctx, **changes) for changes in cases for _ in range(5)]
    EventStorage(engine, ctx[1].tables).store(prepared(events))
    assert ctx[1].publisher.publish_once(500) == 20
    alerts = ctx[0].get("/api/v1/alerts").json()["items"]
    assert len(alerts) == 4 and all(a["count"] == 5 for a in alerts)
    EventStorage(engine, ctx[1].tables).store(prepared([document(ctx) for _ in range(5)]))
    publisher = OutboxPublisher(
        engine,
        ctx[1].tables,
        InvestigationProjector(ctx[1].tables, ctx[1].investigation, rule_version=2),
    )
    assert publisher.publish_once() == 5
    alerts = ctx[0].get("/api/v1/alerts").json()["items"]
    assert len(alerts) == 5 and {a["rule_version"] for a in alerts} == {1, 2}


def test_projector_rollback_and_restart(investigation, engine, monkeypatch):
    ctx = investigation
    events = [document(ctx) for _ in range(5)]
    assert post(ctx, events).status_code == 200

    def fail(stage, session):
        if stage == "before_commit":
            raise RuntimeError("Имитированный отказ commit")

    monkeypatch.setattr(ctx[1].publisher, "_checkpoint", fail)
    with pytest.raises(RuntimeError):
        ctx[1].publisher.publish_once()
    assert ctx[0].get("/api/v1/alerts").json()["items"] == []
    assert ctx[0].get("/api/v1/events").json()["items"] == []
    restored = OutboxPublisher(
        engine, ctx[1].tables, InvestigationProjector(ctx[1].tables, ctx[1].investigation)
    )
    assert restored.publish_once() == 5
    assert restored.publish_once() == 0
    assert ctx[0].get("/api/v1/alerts").json()["items"][0]["count"] == 5


def test_artifact_truncation_expiry_and_missing_content(investigation, engine):
    ctx = investigation
    artifact = str(uuid4())
    events = [document(ctx, type="artifact.observed", artifact_ids=[artifact]) for _ in range(101)]
    store(ctx, events[:100])
    row = ctx[0].get("/api/v1/artifacts").json()["items"][0]
    assert len(row["event_refs"]) == 100 and not row["event_refs_truncated"]
    store(ctx, events[100:])
    row = ctx[0].get("/api/v1/artifacts").json()["items"][0]
    assert row["event_refs_truncated"] and len(row["event_refs"]) == 100
    assert (
        row["event_refs"]
        == sorted(
            [dict(agent_id=e["agent_id"], event_id=e["event_id"]) for e in events],
            key=lambda r: (r["agent_id"], r["event_id"]),
        )[:100]
    )
    with engine.begin() as db:
        db.execute(update(ctx[1].investigation.artifacts).values(state="available"))
    response = ctx[0].get(f"/api/v1/artifacts/{artifact}/content")
    assert response.status_code == 409 and response.json()["code"] == "ARTIFACT_PENDING"
    with engine.begin() as db:
        db.execute(
            update(ctx[1].investigation.artifacts).values(
                expires_at=datetime(2020, 1, 1, tzinfo=UTC)
            )
        )
    assert ctx[0].get(f"/api/v1/artifacts/{artifact}/content").status_code == 410


def test_weak_alert_pagination_and_top_sources(investigation, engine):
    ctx = investigation
    store(
        ctx,
        [document(ctx, source=None)]
        + [document(ctx) for _ in range(5)]
        + [document(ctx, occurred_at="2026-10-11T12:01:10Z") for _ in range(5)],
    )
    first = ctx[0].get("/api/v1/alerts", params={"limit": 1}).json()
    other = next(
        a
        for a in ctx[0].get("/api/v1/alerts").json()["items"]
        if a["id"] != first["items"][0]["id"]
    )
    store(ctx, [document(ctx, occurred_at=other["window"]["from"])])
    second = (
        ctx[0].get("/api/v1/alerts", params={"limit": 1, "cursor": first["next_cursor"]}).json()
    )
    assert second["items"][0]["count"] == 6
    assert first["items"][0]["id"] != second["items"][0]["id"]
    values = service(ctx, engine).top_sources(
        datetime(2026, 10, 11, tzinfo=UTC), datetime(2026, 10, 12, tzinfo=UTC)
    )
    assert values == [{"ip": "2001:db8::1", "count": 11}]


def test_ioc_concurrent_arrival_after_snapshot(investigation, monkeypatch):
    ctx = investigation
    store(ctx, [document(ctx)])
    original = IocExporter.render

    def concurrent(exporter, events):
        # SQL snapshot уже материализован; новая committed публикация не входит в ответ.
        store(ctx, [document(ctx, source={"ip": "192.0.2.100", "port": 1})])
        return original(exporter, events)

    monkeypatch.setattr(IocExporter, "render", concurrent)
    response = ctx[0].get(
        "/api/v1/iocs.csv", params={"from": "2026-10-11T00:00:00Z", "to": "2026-10-12T00:00:00Z"}
    )
    assert response.status_code == 200
    assert "192.0.2.100" not in response.text and "2001:db8::1" in response.text


def test_ioc_http_100000_and_100001_before_response(investigation, engine):
    ctx = investigation
    event = Event.model_validate(document(ctx)).model_dump(mode="json")
    now = datetime.now(UTC)
    t = ctx[1].tables
    # Массовая фикстура не заменяет проверку ingest, отдельно проверенную выше.
    for offset in range(0, 100000, 5000):
        rows = []
        for i in range(offset + 1, offset + 5001):
            identity = UUID(int=i)
            canonical = json.dumps(event | {"event_id": str(identity)}).encode()
            rows.append(
                dict(
                    id=i,
                    agent_id=UUID(event["agent_id"]),
                    event_id=identity,
                    deployment_id=UUID(event["deployment_id"]),
                    instance_id=UUID(event["instance_id"]),
                    profile_revision=1,
                    canonical_bytes=canonical,
                    canonical_version=1,
                    digest=b"x",
                    received_at=now,
                    occurred_at=datetime.fromisoformat(event["occurred_at"]),
                    source_ip=event["source"]["ip"],
                    event_type=event["type"],
                )
            )
        with engine.begin() as db:
            db.execute(t.events.insert(), rows)
            db.execute(
                t.publications.insert(),
                [dict(stream_seq=r["id"], event_pk=r["id"], published_at=now) for r in rows],
            )
    params = {"from": "2026-10-11T00:00:00Z", "to": "2026-10-12T00:00:00Z"}
    response = ctx[0].get("/api/v1/iocs.csv", params=params)
    assert response.status_code == 200
    assert response.content.count(b"\r\n") == 100001
    extra = rows[-1] | {
        "id": 100001,
        "event_id": UUID(int=100001),
        "canonical_bytes": json.dumps(event | {"event_id": str(UUID(int=100001))}).encode(),
    }
    with engine.begin() as db:
        db.execute(t.events.insert().values(**extra))
        db.execute(
            t.publications.insert().values(stream_seq=100001, event_pk=100001, published_at=now)
        )
    response = ctx[0].get("/api/v1/iocs.csv", params=params)
    assert response.status_code == 422
    assert "content-disposition" not in response.headers
    assert response.headers["content-type"].startswith("application/json")


@pytest.mark.parametrize("start", ["2026-10-11", "1791720000", "2026-10-11T00:00:00"])
def test_ioc_rfc3339_required(investigation, start):
    assert (
        investigation[0]
        .get("/api/v1/iocs.csv", params={"from": start, "to": "2026-10-12T00:00:00Z"})
        .status_code
        == 422
    )


@pytest.mark.parametrize(
    "stamp",
    ["2026-10-11T12:00:10Z", "2026-10-11T15:00:10.123Z", "2026-10-11T15:00:10.123456+03:00"],
)
def test_production_canonical_agent_parity(investigation, stamp):
    value = document(investigation, occurred_at=stamp)
    expected = Event.model_validate(value).canonical_json().encode()
    assert investigation[1].ingress.prepare(0, value).canonical_bytes == expected


@pytest.mark.parametrize(
    "field",
    [
        "schema_version",
        "source_seq",
        "profile_revision",
        "source.port",
        "service.port",
        "artifact_ids",
    ],
)
def test_production_strict_agent_constraints(investigation, field):
    value = document(investigation)
    if field == "artifact_ids":
        identity = str(uuid4())
        value[field] = [identity, identity.upper()]
    elif "." in field:
        parent, key = field.split(".")
        value[parent][key] = 1.0
    else:
        value[field] = 1.0
    with pytest.raises(ValueError):
        Event.model_validate(value)
    response = post(investigation, [value])
    assert response.status_code == 200
    assert response.json()["accepted_ids"] == []
    assert response.json()["rejected"][0]["code"] == "INVALID_EVENT"


@pytest.mark.parametrize("stamp", ["9999-12-31T23:59:59-23:59", "0001-01-01T00:00:00+23:59"])
def test_production_utc_overflow_rejected(investigation, stamp):
    response = post(investigation, [document(investigation, occurred_at=stamp)])
    assert response.status_code == 200
    assert response.json()["rejected"][0]["code"] == "INVALID_EVENT"


def test_nested_public_payload_hidden_rest_ws_lookup(investigation):
    ctx = investigation
    event = document(ctx, data={"username": {"password": "nested-secret"}, "success": False})
    store(ctx, [event])
    client = ctx[0]
    history = client.get("/api/v1/events").json()["items"][0]
    lookup = client.post(
        "/api/v1/events/lookup",
        json={"refs": [{"agent_id": event["agent_id"], "event_id": event["event_id"]}]},
    ).json()["items"][0]
    assert history == lookup
    assert history["data"] == {"success": False, "password_present": False}
    with client.websocket_connect("/api/v1/stream") as socket:
        socket.receive_json()
        store(ctx, [document(ctx, data=event["data"])])
        assert socket.receive_json()["event"]["data"] == history["data"]


@pytest.mark.parametrize("protocol", ["tcp", "http"])
def test_low_mapper_production_ingest_rest_ws_lookup(investigation, protocol):
    """Один DTO через production ingress/PostgreSQL/publisher и публичные маршруты."""
    ctx = investigation
    client, _, host, deployment, instance = ctx
    payload = b"GET /secret?token=secret HTTP/1.1\r\nCookie: secret\r\n\r\n"
    row = {
        "record": "event",
        "seq": 7,
        "event": "request",
        "timestamp": "2026-10-13T00:00:00Z",
        "connection_id": str(uuid4()),
        "protocol": protocol,
        "service": {"protocol": protocol, "port": 8080},
        "peer": None,
        "payload_b64": base64.b64encode(payload).decode("ascii"),
    }
    raw = (json.dumps(row) + "\n").encode("ascii")
    binding = JournalBinding(deployment["id"], str(instance), 1)
    record = JournalRecord("event", str(uuid4()), 512, 512 + len(raw), raw)
    mapper = LowProductionMapper(host["host_id"])
    event = mapper.convert(binding, record).model_dump(mode="json")
    with client.websocket_connect("/api/v1/stream") as socket:
        socket.receive_json()
        store(ctx, [event])
        live = socket.receive_json()["event"]
    history = client.get("/api/v1/events", params={"type": protocol + ".request"})
    assert history.status_code == 200
    assert history.json()["items"] == [live]
    lookup = client.post(
        "/api/v1/events/lookup",
        json={"refs": [{"agent_id": event["agent_id"], "event_id": event["event_id"]}]},
    ).json()["items"]
    assert lookup == [live]
    assert live["data"]["capture_completeness"] == "unknown"
    assert live["data"]["captured_bytes"] == len(payload)
    assert "secret" not in json.dumps(live) and "payload_b64" not in live["data"]
    assert "low_seq" not in live["data"]
    assert post(ctx, [event]).json()["accepted_ids"] == [event["event_id"]]
    assert ctx[1].publisher.publish_once(500) == 0


def test_artifact_read_masks_persisted_secret_url(investigation, engine):
    ctx = investigation
    identity = uuid4()
    store(ctx, [document(ctx, type="artifact.observed", artifact_ids=[str(identity)])])
    with engine.begin() as db:
        db.execute(
            update(ctx[1].investigation.artifacts).values(
                source_url="https://user:secret@example.com/?secret=closed"
            )
        )
    assert ctx[0].get("/api/v1/artifacts").json()["items"][0]["source_url"] is None
    assert ctx[0].get(f"/api/v1/artifacts/{identity}").json()["source_url"] is None


@pytest.mark.parametrize("prefix", ["\x00", "\x1f", "\u2003", "\ufeff", "\t"])
def test_csv_formula_control_prefix(prefix):
    assert IocExporter.cell(prefix + "=1") == "'" + prefix + "=1"


@pytest.mark.parametrize("stamp", ["9999-12-31T23:59:59-23:59", "0001-01-01T00:00:00+23:59"])
def test_csv_utc_overflow_is_validation_error(investigation, stamp):
    response = investigation[0].get(
        "/api/v1/iocs.csv", params={"from": stamp, "to": "2026-10-12T00:00:00Z"}
    )
    assert response.status_code == 422
    assert "content-disposition" not in response.headers


def test_production_lifespan_publishes_without_manual_call(investigation):
    import time

    ctx = investigation
    event = document(ctx, source=None)
    with TestClient(ctx[0].app, headers={"Origin": "http://localhost:5173"}) as client:
        login(client)
        live_ctx = (client, *ctx[1:])
        assert post(live_ctx, [event]).json()["accepted_ids"] == [event["event_id"]]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            items = client.get("/api/v1/events").json()["items"]
            if items:
                assert items[0]["event_id"] == event["event_id"]
                assert items[0]["source"] is None
                break
            time.sleep(0.05)
        else:
            pytest.fail("Production poller did not publish the committed event")


@pytest.mark.parametrize(
    "stamp", ["9999-12-31T23:59:00Z", "9999-12-31T23:59:59.999999Z", "9999-12-31T22:59:30-01:00"]
)
def test_unrepresentable_minute_window_rejected_at_ingress(investigation, stamp):
    value = document(investigation, occurred_at=stamp)
    with pytest.raises(ValueError):
        Event.model_validate(value)
    response = post(investigation, [value])
    assert response.status_code == 200
    assert response.json()["rejected"][0]["code"] == "INVALID_EVENT"
    assert investigation[0].get("/api/v1/alerts").status_code == 200


def test_last_representable_minute_alert(investigation):
    ctx = investigation
    store(ctx, [document(ctx, occurred_at="9999-12-31T23:58:59.999999Z") for _ in range(5)])
    response = ctx[0].get("/api/v1/alerts")
    assert response.status_code == 200
    assert response.json()["items"][0]["window"]["to"] == "9999-12-31T23:59:00Z"
