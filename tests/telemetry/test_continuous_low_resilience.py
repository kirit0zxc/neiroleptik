"""C03–C11/C16–C17: worker с production mapping, SQLite и настоящим HTTP."""

import json
import socket
from contextlib import closing
from dataclasses import replace

import pytest
from continuous_low_support import (
    Receiver,
    append,
    expected_events,
    journal,
    snapshot,
    worker_for,
)
from test_low_journal_reader_contract import OTHER, binding, event, footer, header, line
from test_low_production_mapper import AGENT, request
from test_sender import Clock

from agent.runtime.executor.telemetry import LowTelemetryWorker
from agent.telemetry.low_journal.collector import LowJournalCollector
from agent.telemetry.low_journal.durable import RawJournalRepository
from agent.telemetry.sender import TransportError


def pending(state):
    return {row[1]: row[2].encode() for row in state["pending"]}


def assert_evidence(path, source):
    state = snapshot(path)
    original = source.read_bytes().splitlines(keepends=True)
    assert [row[-1] for row in state["low_raw_records"]] == original
    assert state["low_raw_generations"][0][2] == sum(map(len, original))
    assert len(state["low_conversion_ledger"]) == len(original)
    assert not state["low_boundary_bindings"] and not state["low_boundary_writers"]
    return state


def test_c03_503_collects_during_backoff_and_delivers_exact_union(tmp_path):
    source = journal(tmp_path)
    db = tmp_path / "spool.db"
    receiver = Receiver(tmp_path / "receiver.db")
    receiver.mode = "503"
    clock = Clock()
    with receiver.serve() as endpoint, closing(worker_for(db, tmp_path, endpoint, clock)) as worker:
        assert worker.step((binding(),))[1] == "HTTP_RETRY"
        before = assert_evidence(db, source)
        assert pending(before) == expected_events(source)
        for seq in (2, 3):
            append(source, event(seq=seq))
            assert worker.step((binding(),))[1] == "waiting"
            assert pending(assert_evidence(db, source)) == expected_events(source)
            assert len(receiver.rows("requests")) == 1
        receiver.mode = "accepted"
        clock.value = 1
        assert worker.step((binding(),))[1] == "delivered:1"
        assert worker.step((binding(),))[1] == "delivered:2"
        final = assert_evidence(db, source)
        assert not final["pending"] and not final["quarantine"]
        assert receiver.events() == expected_events(source)
        requests = receiver.rows("requests")
        assert requests[0] == requests[1]
        assert worker.step((binding(),))[1] == "idle"


def test_c04_refused_reserved_socket_then_receiver_on_same_endpoint(tmp_path):
    source = journal(tmp_path)
    db = tmp_path / "spool.db"
    clock = Clock()
    receiver = Receiver(tmp_path / "receiver.db")
    # bind без listen гарантирует отказ и удерживает порт до передачи HTTPServer.
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        endpoint = f"http://127.0.0.1:{reserved.getsockname()[1]}/agent/v1/events"
        with closing(worker_for(db, tmp_path, endpoint, clock)) as worker:
            assert worker.step((binding(),))[1] == "TRANSPORT_ERROR"
            assert pending(assert_evidence(db, source)) == expected_events(source)
            with receiver.serve(reserved) as recovered:
                assert recovered == endpoint
                assert worker.step((binding(),))[1] == "waiting"
                assert receiver.rows("requests") == []
                clock.value = 1
                assert worker.step((binding(),))[1] == "delivered:1"
                assert receiver.events() == expected_events(source)
                assert not snapshot(db)["pending"]


@pytest.mark.parametrize("mode", ["foreign", "invalid", "incomplete"])
def test_c06_bad_ack_is_atomic_then_valid_ack_accounts_for_all(tmp_path, mode):
    source = journal(tmp_path, rows=[header(), event(), event(seq=2), footer(last_seq=2)])
    db = tmp_path / "spool.db"
    receiver = Receiver(tmp_path / "receiver.db")
    receiver.mode = mode
    clock = Clock()
    with receiver.serve() as endpoint, closing(worker_for(db, tmp_path, endpoint, clock)) as worker:
        assert worker.step((binding(),))[1] == "INVALID_ACK"
        before = assert_evidence(db, source)
        assert pending(before) == expected_events(source)
        assert not before["quarantine"]
        assert worker.step((binding(),))[1] == "waiting"
        assert snapshot(db) == before
        receiver.mode = "accepted"
        clock.value = 1
        assert worker.step((binding(),))[1] == "delivered:2"
        after = assert_evidence(db, source)
        for table in ("low_raw_records", "low_raw_generations", "low_conversion_ledger"):
            assert before[table] == after[table]
        assert not after["pending"] and not after["quarantine"]
        assert receiver.events() == expected_events(source)


def test_c07_mixed_ack_preserves_raw_ledger_and_does_not_resend_permanent(tmp_path):
    source = journal(
        tmp_path, rows=[header(), event(), event(seq=2), event(seq=3), footer(last_seq=3)]
    )
    expected = expected_events(source)
    accepted, permanent, retryable = expected
    db = tmp_path / "spool.db"
    receiver = Receiver(tmp_path / "receiver.db")
    receiver.mode = "mixed"
    clock = Clock()
    with receiver.serve() as endpoint, closing(worker_for(db, tmp_path, endpoint, clock)) as worker:
        assert worker.step((binding(),))[1] == "delivered:1"
        before = assert_evidence(db, source)
        assert pending(before) == {retryable: expected[retryable]}
        assert [(row[1], row[2].encode(), row[4]) for row in before["quarantine"]] == [
            (permanent, expected[permanent], "INVALID_EVENT")
        ]
        assert receiver.events() == {accepted: expected[accepted]}
        receiver.mode = "accepted"
        clock.value = 1
        assert worker.step((binding(),))[1] == "delivered:1"
        sent = json.loads(receiver.rows("requests")[-1][0])["events"]
        assert [row["event_id"] for row in sent] == [retryable]
        after = assert_evidence(db, source)
        assert not after["pending"]
        assert after["quarantine"] == before["quarantine"]
        assert after["low_conversion_ledger"] == before["low_conversion_ledger"]
        assert receiver.events() == {key: raw for key, raw in expected.items() if key != permanent}


@pytest.mark.parametrize("status", ["401", "403"])
def test_c08_auth_pause_collects_until_restart(tmp_path, status):
    source = journal(tmp_path)
    db = tmp_path / "spool.db"
    receiver = Receiver(tmp_path / "receiver.db")
    receiver.mode = status
    with receiver.serve() as endpoint:
        with closing(worker_for(db, tmp_path, endpoint)) as worker:
            assert worker.step((binding(),))[1] == "AUTH_REQUIRED"
            for seq in (2, 3, 4):
                append(source, event(seq=seq))
                assert worker.step((binding(),))[1] == "AUTH_REQUIRED"
                assert pending(assert_evidence(db, source)) == expected_events(source)
                assert len(receiver.rows("requests")) == 1
            assert receiver.events() == {}
        receiver.mode = "accepted"
        with closing(worker_for(db, tmp_path, endpoint)) as worker:
            assert worker.step((binding(),))[1] == "delivered:4"
            assert worker.step((binding(),))[1] == "idle"
        assert not snapshot(db)["pending"]
        assert receiver.events() == expected_events(source)


@pytest.mark.parametrize("failure", ["corrupt", "late_null"])
def test_c09_c10_round_robin_keeps_independent_progress_and_saved_delivery(tmp_path, failure):
    first = journal(tmp_path)
    second_binding = replace(binding(), deployment_id=OTHER)
    second = journal(tmp_path, second_binding, [header(deployment_id=OTHER), event(), footer()])
    db = tmp_path / "spool.db"
    receiver = Receiver(tmp_path / "receiver.db")
    receiver.mode = "503"
    with receiver.serve() as endpoint:
        with closing(worker_for(db, tmp_path, endpoint)) as worker:
            assert worker.step((binding(),))[1] == "HTTP_RETRY"
        expected_first = expected_events(first)
        if failure == "corrupt":
            append(first, b"CORRUPT_SECRET\n", event(seq=2))
            codes = {"INVALID_RECORD"}
        else:
            # Остановка между collector commit и conversion: raw уже доверен и сохранён.
            append(first, event(seq=2))
            with closing(RawJournalRepository(db, AGENT)) as raw:
                assert LowJournalCollector(raw, binding()).step([first]).saved_records == 1
            expected_first = expected_events(first)
            first.with_name(f"low-{binding().instance_id}-late.jsonl").write_bytes(
                line(header(journal_id=OTHER, journal_seq=2, first_seq=2))
            )
            codes = {"MISSING_PREDECESSOR", "OPEN_PREDECESSOR"}
        saved = snapshot(db)
        receiver.mode = "accepted"
        with closing(worker_for(db, tmp_path, endpoint)) as worker:
            collected, sent = worker.step((binding(), second_binding))
            assert collected in codes and sent == f"delivered:{len(expected_first)}"
            assert snapshot(db)["low_raw_generations"][0] == saved["low_raw_generations"][0]
            assert worker.step((binding(), second_binding))[1] == "delivered:1"
        after = snapshot(db)
        assert not after["low_boundary_bindings"] and not after["low_boundary_writers"]
        assert receiver.events() == expected_first | expected_events(second, second_binding)
        assert not after["pending"]


def test_c11_partial_line_restart_completion_footer_and_replay(tmp_path):
    encoded = line(event())
    source = journal(tmp_path, rows=[header(), encoded[:30]])
    db = tmp_path / "spool.db"
    receiver = Receiver(tmp_path / "receiver.db")
    with receiver.serve() as endpoint:
        with closing(worker_for(db, tmp_path, endpoint)) as worker:
            assert worker.step((binding(),))[1] == "idle"
        before = snapshot(db)
        assert before["low_raw_generations"][0][2] == len(line(header()))
        assert len(before["low_raw_records"]) == len(before["low_conversion_ledger"]) == 1
        with closing(worker_for(db, tmp_path, endpoint)) as worker:
            assert worker.step((binding(),))[1] == "idle"
            assert snapshot(db) == before
            append(source, encoded[30:], footer())
            assert worker.step((binding(),))[1] == "delivered:1"
            after = assert_evidence(db, source)
            assert worker.step((binding(),))[1] == "idle"
            assert snapshot(db) == after
        assert receiver.events() == expected_events(source)
        assert len(receiver.rows("requests")) == 1


def test_c16_safe_worker_diagnostics_for_payload_ack_token_and_transport(tmp_path, capsys):
    payload = b"GET /PAYLOAD_SECRET?token=QUERY_SECRET HTTP/1.1\r\n\r\n"
    source = journal(tmp_path, rows=[header(), request(payload)])
    receiver = Receiver(tmp_path / "receiver.db")
    receiver.mode = "invalid"
    db = tmp_path / "spool.db"
    with receiver.serve() as endpoint, closing(worker_for(db, tmp_path, endpoint)) as worker:
        status = worker.step((binding(),))
        assert status[1] == "INVALID_ACK"

    class BrokenTransport:
        def post(self, payload):
            raise TransportError("TRANSPORT_SECRET Authorization private/path")

    with closing(LowTelemetryWorker(db, tmp_path, AGENT, BrokenTransport())) as worker:
        broken_status = worker.step((binding(),))
        assert broken_status[1] == "TRANSPORT_ERROR"
    public = repr((status, broken_status)) + str(capsys.readouterr())
    for secret in (
        "PAYLOAD_SECRET",
        "QUERY_SECRET",
        "ACK_SECRET",
        "TRANSPORT_SECRET",
        "Authorization",
        str(source),
        "hf_agent_test-only-token",
        "payload_b64",
    ):
        assert secret not in public
    assert pending(snapshot(db)) == expected_events(source)


def test_c17_raw_budget_does_not_remove_saved_pending_or_advance_checkpoint(tmp_path):
    source = journal(tmp_path)
    receiver = Receiver(tmp_path / "receiver.db")
    receiver.mode = "503"
    clock = Clock()
    db = tmp_path / "spool.db"
    with receiver.serve() as endpoint, closing(worker_for(db, tmp_path, endpoint, clock)) as worker:
        assert worker.step((binding(),))[1] == "HTTP_RETRY"
        before = snapshot(db)
        worker._raw.max_raw_bytes = len(source.read_bytes())
        original = expected_events(source)
        append(source, event(seq=2))
        receiver.mode = "accepted"
        clock.value = 1
        assert worker.step((binding(),)) == ("STORAGE_LIMIT", "delivered:1")
        after = snapshot(db)
        for table in (
            "low_raw_records",
            "low_raw_generations",
            "low_conversion_ledger",
            "low_storage_usage",
        ):
            assert before[table] == after[table]
        assert not after["pending"] and not after["quarantine"]
        assert receiver.events() == original
        assert len(source.read_bytes()) > after["low_raw_generations"][0][2]
