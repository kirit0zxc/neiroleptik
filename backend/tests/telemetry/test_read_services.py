"""Observable проверки независимого уровня R01–R20/R26–R28/R32–R33."""

import asyncio
import base64
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import jsonschema
import pytest

from honeyforge.common import DomainError
from honeyforge.telemetry.cursors import CursorCodec
from honeyforge.telemetry.history import CatchUpService, HistoryService
from honeyforge.telemetry.presentation import PublicEventPresenter
from honeyforge.telemetry.read_contract import HistoryFilters, PublishedEvent
from honeyforge.telemetry.stream import LiveStreamService

NOW = datetime(2026, 10, 10, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[3]


class TestReader:
    """Только синтетические committed fixtures; pending намеренно не включён."""

    __test__ = False

    def __init__(self, rows: list[PublishedEvent]) -> None:
        self.rows, self.floor = rows, None

    def watermark(self) -> int:
        return max((row.seq for row in self.rows), default=0)

    def retention_floor(self) -> int | None:
        return self.floor

    def history(
        self,
        filters: HistoryFilters,
        watermark: int,
        before: tuple[datetime, str, str] | None,
        limit: int,
    ) -> list[PublishedEvent]:
        rows = [
            r
            for r in self.rows
            if r.seq <= watermark
            and (before is None or r.key < before)
            and (filters.deployment_id is None or r.event["deployment_id"] == filters.deployment_id)
            and (filters.source_ip is None or r.event["source"]["ip"] == filters.source_ip)
            and (filters.event_type is None or r.event["type"] == filters.event_type)
            and (
                filters.start is None
                or datetime.fromisoformat(r.event["occurred_at"]) >= filters.start
            )
            and (
                filters.end is None or datetime.fromisoformat(r.event["occurred_at"]) < filters.end
            )
        ]
        return sorted(rows, key=lambda r: r.key, reverse=True)[:limit]

    def stream(self, after: int, until: int, limit: int) -> list[PublishedEvent]:
        return sorted([r for r in self.rows if after < r.seq <= until], key=lambda r: r.seq)[:limit]


def record(
    seq: int,
    event_id: int | None = None,
    agent: int = 1,
    received: datetime = NOW,
    kind: str = "session.command",
) -> PublishedEvent:
    value = {
        "schema_version": 1,
        "event_id": str(UUID(int=event_id or seq)),
        "agent_id": str(UUID(int=agent)),
        "deployment_id": str(UUID(int=10)),
        "instance_id": str(UUID(int=11)),
        "session_id": None,
        "source_seq": 0,
        "occurred_at": NOW.isoformat(),
        "type": kind,
        "source": {"ip": "192.0.2.1", "port": 123},
        "service": {"protocol": "ssh", "port": 22},
        "data": {"input": "<script>\u001b[31m\u001b]0;test\u0007", "truncated": True},
        "artifact_ids": [],
        "profile_revision": 1,
    }
    return PublishedEvent(seq, received, value)


def mask(value: dict[str, Any]) -> dict[str, Any]:
    if value["type"] == "auth.attempt" and "password" in value["data"]:
        value["data"]["password"] = "••••"
    return value


def services(rows: list[PublishedEvent]) -> tuple[TestReader, HistoryService, CatchUpService]:
    reader = TestReader(rows)
    presenter = PublicEventPresenter(mask)
    return (
        reader,
        HistoryService(reader, CursorCodec(b"k" * 32), presenter),
        CatchUpService(reader, presenter),
    )


@pytest.mark.parametrize("count", [0, 1, 2, 3, 4])
def test_history_exact_page_and_snapshot(count: int) -> None:
    reader, history, _ = services([record(i) for i in range(1, count + 1)])
    first = history.page(HistoryFilters(), limit=2)
    ids = [e["event_id"] for e in first["items"]]
    reader.rows.append(record(100, received=NOW - timedelta(seconds=1)))
    cursor = first["next_cursor"]
    while cursor:
        page = history.page(HistoryFilters(), cursor, 2)
        ids.extend(e["event_id"] for e in page["items"])
        cursor = page["next_cursor"]
    assert ids == [str(UUID(int=i)) for i in range(count, 0, -1)]
    assert str(UUID(int=100)) in [e["event_id"] for e in history.page(HistoryFilters())["items"]]


def test_history_ties_pair_identity() -> None:
    _, history, _ = services([record(1, event_id=7, agent=1), record(2, event_id=7, agent=2)])
    first = history.page(HistoryFilters(), limit=1)
    second = history.page(HistoryFilters(), first["next_cursor"], 1)
    assert first["items"][0]["agent_id"] == str(UUID(int=2))
    assert second["items"][0]["agent_id"] == str(UUID(int=1))
    assert second["next_cursor"] is None


def test_filter_boundaries_intersection_and_normalization() -> None:
    _, history, _ = services([record(1)])
    filters = history.filters(
        str(UUID(int=10)),
        "192.0.2.1",
        "session.command",
        NOW.isoformat(),
        (NOW + timedelta(seconds=1)).isoformat(),
    )
    assert len(history.page(filters)["items"]) == 1
    assert history.page(history.filters(end=NOW.isoformat()))["items"] == []
    for f in [
        history.filters(deployment_id=str(UUID(int=99))),
        history.filters(source_ip="192.0.2.2"),
        history.filters(event_type="http.request"),
    ]:
        assert history.page(f)["items"] == []
    assert history.filters(start="2026-10-10T03:00:00+03:00").start == NOW


@pytest.mark.parametrize(
    "kwargs",
    [
        {"deployment_id": "bad"},
        {"source_ip": "invalid"},
        {"event_type": "tcp.request"},
        {"start": "2026-10-10"},
        {"start": "2026-10-10T00:00:00"},
        {"start": NOW.isoformat(), "end": NOW.isoformat()},
    ],
)
def test_invalid_filters(kwargs: dict[str, str]) -> None:
    with pytest.raises(DomainError) as error:
        HistoryService.filters(**kwargs)
    assert error.value.status == 422


@pytest.mark.parametrize("limit", [0, 201, -1, True])
def test_invalid_history_limit(limit: int) -> None:
    _, history, _ = services([])
    with pytest.raises(DomainError):
        history.page(HistoryFilters(), limit=limit)


def test_cursor_tampering_versions_and_filters() -> None:
    _, history, _ = services([record(1), record(2)])
    cursor = history.page(HistoryFilters(), limit=1)["next_cursor"]
    for broken in ["", cursor[:-10], "!" + cursor, "A" * 5000]:
        with pytest.raises(DomainError) as error:
            history.page(HistoryFilters(), broken)
        assert error.value.status == 422
    with pytest.raises(DomainError):
        history.page(history.filters(source_ip="192.0.2.2"), cursor)
    # Валидная подпись несовместимой версии всё равно не разрешает чтение.
    import hmac

    payload = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))[32:])
    payload["v"] = 2
    raw = json.dumps(payload).encode()
    signed = base64.urlsafe_b64encode(hmac.digest(b"k" * 32, raw, "sha256") + raw).decode()
    with pytest.raises(DomainError):
        history.page(HistoryFilters(), signed)


def test_catchup_bigint_gaps_fixed_watermark_and_exact_last_page() -> None:
    start = 2**53 + 1
    reader, _, catchup = services([record(start), record(start + 3), record(start + 9)])
    page = catchup.page(str(start - 1), limit=1)
    assert page["items"][0]["stream_seq"] == str(start)
    reader.rows.append(record(start + 20))
    second = catchup.page(page["next_cursor"], page["watermark"], 2)
    assert [r["stream_seq"] for r in second["items"]] == [str(start + 3), str(start + 9)]
    assert second["next_cursor"] is None
    assert second["watermark"] == page["watermark"]
    assert catchup.page(str(start + 9), str(start + 9))["items"] == []
    assert set(second["items"][0]) == {"stream_seq", "event"}


@pytest.mark.parametrize("value", ["-1", "9223372036854775808", "1.0", "1e1", "١", "", "9" * 100])
def test_invalid_seq(value: str) -> None:
    _, _, catchup = services([])
    with pytest.raises(DomainError) as error:
        catchup.page(value)
    assert error.value.status == 422


def test_future_reverse_empty_and_retention_policy() -> None:
    reader, _, catchup = services([])
    assert catchup.page() == {"items": [], "watermark": "0", "next_cursor": None}
    for after, until in [("0", "1"), ("1", "0")]:
        with pytest.raises(DomainError):
            catchup.page(after, until)
    reader.rows.append(record(10))
    reader.floor = 5
    with pytest.raises(DomainError) as error:
        catchup.page("4")
    assert error.value.status == 410
    assert catchup.page("5")["items"][0]["stream_seq"] == "10"


def test_projection_original_and_wire_fields() -> None:
    row = record(1, kind="auth.attempt")
    row.event["data"] = {"username": "test", "password": "secret", "success": False}
    row.event["canonical_bytes"] = "internal"
    original = deepcopy(row.event)
    _, history, catchup = services([row])
    event = history.page(HistoryFilters())["items"][0]
    assert event["data"]["password"] == "••••"
    assert "canonical_bytes" not in event
    assert row.event == original
    assert catchup.page()["items"][0]["event"] == event
    schema = json.loads((ROOT / "contracts/event-v1.schema.json").read_text(encoding="utf-8"))
    jsonschema.validate(event, schema, format_checker=jsonschema.FormatChecker())
    for kind, data in [
        ("session.command", {"input": "<b>\u001b]0;x\u0007", "truncated": True}),
        ("session.output", {"output": "\u001b[31mtext"}),
        ("http.request", {"method": "GET", "path": "/"}),
    ]:
        row = record(2, kind=kind)
        row.event["data"] = data
        _, history, _ = services([row])
        assert history.page(HistoryFilters())["items"][0]["data"] == data


def test_live_hello_gap_two_clients_and_cancellation() -> None:
    async def scenario() -> None:
        reader, history, _ = services([record(1)])
        stream = LiveStreamService(reader, history.presenter, poll_seconds=0.001, page_size=1)
        output: list[list[dict[str, Any]]] = [[], []]
        ready = [asyncio.Event(), asyncio.Event()]
        received = [asyncio.Event(), asyncio.Event()]
        tasks = []
        for index in range(2):

            async def send(value: dict[str, Any], i: int = index) -> None:
                output[i].append(value)
                (ready[i] if value["type"] == "hello" else received[i]).set()

            tasks.append(asyncio.create_task(stream.run(send, lambda: None)))
        try:
            await asyncio.wait_for(asyncio.gather(*(e.wait() for e in ready)), 1)
            assert all(o == [{"type": "hello", "watermark": "1"}] for o in output)
            reader.rows.append(record(3))
            await asyncio.wait_for(asyncio.gather(*(e.wait() for e in received)), 1)
            assert all(o[1]["stream_seq"] == "3" for o in output)
            assert all(o[1]["event"]["data"]["input"] for o in output)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        assert reader.watermark() == 3

    asyncio.run(scenario())


def test_live_timeout_and_idle_revocation() -> None:
    async def scenario() -> None:
        reader, history, _ = services([])
        stream = LiveStreamService(reader, history.presenter, poll_seconds=0.001, send_seconds=0.01)

        async def slow(value: dict[str, Any]) -> None:
            await asyncio.sleep(10)

        with pytest.raises(TimeoutError):
            await stream.run(slow, lambda: None)
        revoked = False
        sent = []

        def auth() -> None:
            if revoked:
                raise DomainError(401, "Сессия отозвана")

        async def send(value: dict[str, Any]) -> None:
            nonlocal revoked
            sent.append(value)
            revoked = True

        with pytest.raises(DomainError) as error:
            await stream.run(send, auth)
        assert error.value.status == 401
        assert sent == [{"type": "hello", "watermark": "0"}]

    asyncio.run(scenario())


@pytest.mark.parametrize("limit", [0, 501, True])
def test_invalid_catchup_limit(limit: int) -> None:
    _, _, catchup = services([])
    with pytest.raises(DomainError) as error:
        catchup.page(limit=limit)
    assert error.value.status == 422


def test_live_masks_secrets_without_changing_original() -> None:
    async def scenario() -> None:
        reader, history, _ = services([])
        row = record(1, kind="auth.attempt")
        row.event["data"] = {"username": "test", "password": "secret", "success": True}
        original = deepcopy(row.event)
        stream = LiveStreamService(reader, history.presenter, poll_seconds=0.001)
        got_event = asyncio.Event()
        sent = []

        async def send(value: dict[str, Any]) -> None:
            sent.append(value)
            if value["type"] == "hello":
                reader.rows.append(row)
            else:
                got_event.set()

        task = asyncio.create_task(stream.run(send, lambda: None))
        try:
            await asyncio.wait_for(got_event.wait(), 1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert sent[0] == {"type": "hello", "watermark": "0"}
        assert sent[1]["event"]["data"]["password"] == "••••"
        assert row.event == original

    asyncio.run(scenario())
