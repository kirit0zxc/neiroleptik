"""Replay читает только опубликованные чанки и не закрывает неполную доставку."""

import base64
from uuid import uuid4

from test_commands import RuntimeHarness
from test_investigation import document, investigation, store  # noqa: F401


def test_medium_reset_api(admin, engine):
    runtime = RuntimeHarness(admin, engine, "medium")
    _, first = runtime.start()
    assert runtime.request("reset").status_code == 202
    reset = runtime.claim()
    result = runtime.receipt(reset)
    assert result["instance_id"] != first["instance_id"]
    assert runtime.ack(reset, result).status_code == 200
    assert runtime.state()["state"] == "running"


def test_replay_orders_concurrent_channels_by_time(investigation):  # noqa: F811
    """Порядок закрытия каналов не должен переставлять их записанный вывод."""
    ctx = investigation
    session = uuid4()
    early, late = uuid4(), uuid4()
    events = []
    for recording, seq, stamp, payload in (
        (late, 0, "2026-10-15T12:00:01Z", b"late"),
        (early, 8192, "2026-10-15T14:00:00+02:00", b"early"),
    ):
        events.append(
            document(
                ctx,
                type="session.output",
                session_id=str(session),
                data={
                    "replay": {
                        "seq": seq,
                        "capture_seq": 0,
                        "recording_id": str(recording),
                        "offset_ms": 0,
                        "started_at": stamp,
                        "direction": "out",
                        "data_base64": base64.b64encode(payload).decode(),
                        "truncated": False,
                        "final": True,
                    }
                },
            )
        )
    events.append(
        document(
            ctx,
            type="session.closed",
            session_id=str(session),
            data={"replay_recordings": [str(early), str(late)]},
        )
    )
    store(ctx, events)
    path = f"/api/v1/sessions/{session}/replay"
    first = ctx[0].get(path, params={"limit": 1}).json()
    second = ctx[0].get(path, params={"after": first["next_cursor"]}).json()
    assert base64.b64decode(first["chunks"][0]["data_base64"]) == b"early"
    assert base64.b64decode(second["chunks"][0]["data_base64"]) == b"late"
    assert [first["chunks"][0]["offset_ms"], second["chunks"][0]["offset_ms"]] == [0, 1000]


def test_replay_manifest_waits_for_missing_chunk_and_pages(investigation):  # noqa: F811
    ctx = investigation
    client = ctx[0]
    session, recording = uuid4(), uuid4()

    def chunk(seq, final=False):
        return document(
            ctx,
            type="session.output",
            session_id=str(session),
            data={
                "replay": {
                    "seq": seq,
                    "capture_seq": seq,
                    "recording_id": str(recording),
                    "offset_ms": seq * 100,
                    "started_at": "2026-10-15T12:00:00Z",
                    "direction": "out",
                    "data_base64": base64.b64encode(b"root\r\n").decode(),
                    "truncated": False,
                    "final": final,
                }
            },
        )

    store(
        ctx,
        [
            chunk(1, True),
            document(
                ctx,
                type="session.closed",
                session_id=str(session),
                data={"replay_recordings": [str(recording)]},
            ),
        ],
    )
    path = f"/api/v1/sessions/{session}/replay"
    pending = client.get(path).json()
    assert pending["chunks"] == [] and pending["complete"] is False
    store(ctx, [chunk(0)])
    page = client.get(path, params={"limit": 1}).json()
    assert page["complete"] is True and page["chunks"][0]["seq"] == 0
    last = client.get(path, params={"after": page["next_cursor"]}).json()
    assert last["chunks"][0]["seq"] == 1 and last["next_cursor"] is None
    assert (
        client.get(
            f"/api/v1/sessions/{uuid4()}/replay", params={"after": page["tail_cursor"]}
        ).status_code
        == 422
    )
    client.cookies.clear()
    assert client.get(path).status_code == 401
