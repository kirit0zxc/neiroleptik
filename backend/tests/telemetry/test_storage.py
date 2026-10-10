"""Транзакционные сценарии S01–S32 на настоящих PostgreSQL constraints."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, Event
from time import monotonic
from uuid import UUID

import pytest
from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from honeyforge.telemetry.publisher import OutboxPublisher
from honeyforge.telemetry.reader import PublicationReader
from honeyforge.telemetry.storage import EventStorage
from honeyforge.telemetry.types import BIGINT_MAX, EventFilters, PreparedEvent

FIXTURES = Path(__file__).parent / "fixtures"


class GoldenEvents:
    """Проверенные агентом байты; замена UUID сохраняет канонический формат."""

    def make(self, number=1, index=0, agent=1, variant="base"):
        value = (FIXTURES / f"{variant}.json").read_bytes()
        value = value.replace(
            b"00000000-0000-4000-8000-000000000001", str(UUID(int=agent)).encode()
        ).replace(b"cbedf64d-96bc-5f0d-9be6-b514f8c4a543", str(UUID(int=number)).encode())
        return PreparedEvent(
            index,
            UUID(int=agent),
            UUID(int=number),
            UUID("00000000-0000-4000-8000-000000000002"),
            UUID("00000000-0000-4000-8000-000000000003"),
            1,
            value,
        )


class DatabaseEvidence:
    """Каждая проверка наблюдает committed данные отдельным соединением."""

    def __init__(self, engine, tables):
        self.engine, self.t = engine, tables

    def snapshot(self):
        with self.engine.connect() as connection:
            return tuple(
                tuple(
                    connection.execute(
                        select(table).order_by(list(table.primary_key.columns)[0])
                    ).all()
                )
                for table in (self.t.events, self.t.outbox, self.t.publications, self.t.state)
            )

    def counts(self, expected):
        with self.engine.connect() as connection:
            actual = tuple(
                connection.scalar(select(func.count()).select_from(table))
                for table in (self.t.events, self.t.outbox, self.t.publications)
            )
        assert actual == expected

    def invariants(self):
        events, outbox, publications, state = self.snapshot()
        assert len(events) == len(outbox)
        assert {r.event_pk for r in outbox} == {r.id for r in events}
        assert len({r.event_pk for r in publications}) == len(publications)
        assert {r.event_pk for r in publications} == {
            r.event_pk for r in outbox if r.processed_at is not None
        }
        assert all(r.stream_seq > 0 for r in publications)
        assert state[0].last_seq == max((r.stream_seq for r in publications), default=0)

    def wait_for_lock(self):
        deadline = monotonic() + 10
        with self.engine.connect() as connection:
            while monotonic() < deadline:
                if connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
                        "AND pid<>pg_backend_pid() AND wait_event_type='Lock'"
                    )
                ):
                    return
                connection.rollback()
        raise AssertionError("Вторая транзакция не достигла ожидания PostgreSQL lock")


class PausedStorage(EventStorage):
    """Управляемая граница до commit, без барьера после блокирующего INSERT."""

    def __init__(self, engine, tables, stage="before_commit", fail=False):
        super().__init__(engine, tables)
        self.stage, self.fail = stage, fail
        self.entered, self.release = Event(), Event()

    def _checkpoint(self, stage, session):
        if stage == self.stage:
            self.entered.set()
            assert self.release.wait(10), "Истёк timeout управляемой транзакции"
            if self.fail:
                raise RuntimeError("Управляемый отказ записи")


class PausedPublisher(OutboxPublisher):
    """Остановка под настоящим lock стабильного счётчика."""

    def __init__(self, engine, tables, stage="before_commit", fail=False):
        super().__init__(engine, tables)
        self.stage, self.fail = stage, fail
        self.entered, self.release = Event(), Event()

    def _checkpoint(self, stage, session):
        if stage == self.stage:
            self.entered.set()
            assert self.release.wait(10)
            if self.fail:
                raise RuntimeError("Управляемый отказ публикации")


@pytest.fixture
def core(engine, tables):
    return (
        GoldenEvents(),
        EventStorage(engine, tables),
        OutboxPublisher(engine, tables),
        PublicationReader(engine, tables),
        DatabaseEvidence(engine, tables),
    )


class TestStorage:
    def test_insert_event_and_outbox(self, core):
        f, store, _, reader, db = core
        event = f.make()
        assert store.store([event])[0].status == "accepted"
        db.counts((1, 1, 0))
        rows = db.snapshot()
        assert rows[0][0].canonical_bytes == event.canonical_bytes
        assert rows[0][0].received_at.tzinfo is not None
        assert rows[1][0].processed_at is None
        assert reader.watermark() == 0
        db.invariants()

    def test_identical_retry_preserves_original(self, core):
        f, store, _, _, db = core
        store.store([f.make()])
        original = db.snapshot()
        assert EventStorage(store.engine, store.tables).store([f.make()])[0].status == "accepted"
        assert db.snapshot() == original

    def test_conflict_does_not_overwrite(self, core):
        f, store, _, _, db = core
        store.store([f.make()])
        original = db.snapshot()
        assert store.store([f.make(variant="password_a")])[0].status == "EVENT_ID_CONFLICT"
        assert db.snapshot() == original

    def test_mixed_batch_commits_good_items(self, core):
        f, store, _, _, db = core
        store.store([f.make()])
        original = db.snapshot()[0][0]
        result = store.store([f.make(2), f.make(index=1, variant="password_a"), f.make(3, 2)])
        assert [(r.index, r.status) for r in result] == [
            (0, "accepted"),
            (1, "EVENT_ID_CONFLICT"),
            (2, "accepted"),
        ]
        db.counts((3, 3, 0))
        assert db.snapshot()[0][0] == original
        db.invariants()

    def test_same_event_id_for_two_agents(self, core):
        f, store, publisher, reader, db = core
        store.store([f.make(), f.make(index=1, agent=2)])
        assert publisher.publish_once() == 2
        assert {r.agent_id for r in reader.stream(0, 2)} == {UUID(int=1), UUID(int=2)}
        db.counts((2, 2, 2))
        db.invariants()

    def test_duplicate_keys_in_internal_input(self, core):
        f, store, _, _, db = core
        with pytest.raises(ValueError):
            store.store([f.make(), f.make(index=1)])
        db.counts((0, 0, 0))

    @pytest.mark.parametrize("stage", ["event_inserted", "before_commit"])
    def test_rollback_and_commit_failure_have_no_success(self, core, stage):
        f, store, _, _, db = core
        failing = PausedStorage(store.engine, store.tables, stage, True)
        failing.release.set()
        with pytest.raises(RuntimeError):
            failing.store([f.make()])
        db.counts((0, 0, 0))
        db.invariants()

    def test_actual_commit_failure_has_no_success(self, core):
        f, store, _, _, db = core

        def fail_commit(session):
            raise RuntimeError("Отказ на границе commit")

        sqlalchemy_event.listen(Session, "before_commit", fail_commit)
        try:
            with pytest.raises(RuntimeError):
                store.store([f.make(), f.make(2, 1)])
        finally:
            sqlalchemy_event.remove(Session, "before_commit", fail_commit)
        db.counts((0, 0, 0))
        db.invariants()

    def test_unexpected_integrity_error_rolls_back_batch(self, core):
        f, store, _, _, db = core

        class BrokenOutbox(EventStorage):
            def _checkpoint(self, stage, session):
                if (
                    stage == "event_inserted"
                    and session.scalar(select(func.count()).select_from(self.tables.events)) == 2
                ):
                    session.execute(insert(self.tables.outbox).values(event_pk=-1))

        with pytest.raises(IntegrityError):
            BrokenOutbox(store.engine, store.tables).store([f.make(), f.make(2, 1)])
        db.counts((0, 0, 0))
        db.invariants()

    def test_lost_result_after_commit(self, core):
        f, store, _, _, db = core
        store.store([f.make()])
        before = db.snapshot()
        assert EventStorage(store.engine, store.tables).store([f.make()])[0].status == "accepted"
        assert db.snapshot() == before
        db.counts((1, 1, 0))

    def test_digest_is_not_equality(self, core):
        f, store, _, _, db = core
        colliding = EventStorage(store.engine, store.tables, digest=lambda _: b"same")
        colliding.store([f.make()])
        before = db.snapshot()
        assert colliding.store([f.make(variant="password_a")])[0].status == "EVENT_ID_CONFLICT"
        assert db.snapshot() == before

    def test_empty_input_is_noop(self, core):
        _, store, _, _, db = core
        before = db.snapshot()
        assert store.store([]) == []
        assert db.snapshot() == before

    def test_projection_matches_original(self, core):
        f, store, _, _, db = core
        with pytest.raises(ValueError):
            replace(f.make(), agent_id=UUID(int=99))
        db.counts((0, 0, 0))

    @pytest.mark.parametrize(
        "variant,rollback", [("base", False), ("password_a", False), ("base", True)]
    )
    def test_competing_insert_waits_for_commit_or_rollback(self, core, variant, rollback):
        f, store, _, _, db = core
        first = PausedStorage(store.engine, store.tables, fail=rollback)
        with ThreadPoolExecutor(2) as pool:
            winner = pool.submit(first.store, [f.make()])
            try:
                assert first.entered.wait(10)
                loser = pool.submit(store.store, [f.make(variant=variant)])
                db.wait_for_lock()
                db.counts((0, 0, 0))
            finally:
                first.release.set()
            if rollback:
                with pytest.raises(RuntimeError):
                    winner.result(10)
            else:
                assert winner.result(10)[0].status == "accepted"
            result = loser.result(10)
        assert result[0].status == ("EVENT_ID_CONFLICT" if variant != "base" else "accepted")
        db.counts((1, 1, 0))
        assert db.snapshot()[0][0].canonical_bytes == f.make().canonical_bytes
        db.invariants()

    def test_reversed_overlapping_batches(self, core):
        f, store, _, _, db = core
        barrier = Barrier(2, timeout=10)

        def write(values):
            barrier.wait()
            return store.store(values)

        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(write, [f.make(1), f.make(2, 1)])
            second = pool.submit(write, [f.make(2), f.make(1, 1)])
            for future, ids in [(first, [1, 2]), (second, [2, 1])]:
                result = future.result(10)
                assert [r.event_id.int for r in result] == ids
                assert all(r.status == "accepted" for r in result)
        db.counts((2, 2, 0))
        db.invariants()

    def test_uncommitted_event_is_invisible(self, core):
        f, store, publisher, reader, db = core
        paused = PausedStorage(store.engine, store.tables)
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(paused.store, [f.make()])
            try:
                assert paused.entered.wait(10)
                assert publisher.publish_once() == 0
                assert reader.history(0) == reader.stream(0, 0) == []
                db.counts((0, 0, 0))
            finally:
                paused.release.set()
            assert future.result(10)[0].status == "accepted"
        assert publisher.publish_once() == 1
        assert reader.watermark() == 1
        db.invariants()

    def test_constraints_enforced_in_database(self, core):
        f, store, _, _, db = core
        store.store([f.make()])
        original = db.snapshot()[0][0]._asdict()
        original.pop("id")
        with pytest.raises(IntegrityError), store.engine.begin() as connection:
            connection.execute(insert(store.tables.events).values(**original))
        with pytest.raises(IntegrityError), store.engine.begin() as connection:
            connection.execute(insert(store.tables.outbox).values(event_pk=999))
        db.counts((1, 1, 0))
        db.invariants()


class TestPublisher:
    def test_publish_atomically(self, core):
        f, store, publisher, reader, db = core
        store.store([f.make(), f.make(2, 1)])
        assert publisher.publish_once() == 2
        assert [r.stream_seq for r in reader.stream(0, 2)] == [1, 2]
        db.counts((2, 2, 2))
        db.invariants()

    def test_publish_empty_and_repeat(self, core):
        f, store, publisher, reader, db = core
        assert publisher.publish_once() == reader.watermark() == 0
        store.store([f.make()])
        publisher.publish_once()
        before = db.snapshot()
        assert publisher.publish_once() == 0
        assert db.snapshot() == before

    def test_publisher_rollback_before_commit(self, core):
        f, store, publisher, reader, db = core
        store.store([f.make()])
        failing = PausedPublisher(store.engine, store.tables, "publication_inserted", True)
        failing.release.set()
        before = db.snapshot()
        with pytest.raises(RuntimeError):
            failing.publish_once()
        assert db.snapshot() == before
        assert reader.watermark() == 0
        assert publisher.publish_once() == 1
        db.invariants()

    def test_publisher_restart_after_commit(self, core):
        f, store, publisher, _, db = core
        store.store([f.make()])
        publisher.publish_once()
        before = db.snapshot()
        assert OutboxPublisher(store.engine, store.tables).publish_once() == 0
        assert db.snapshot() == before
        db.invariants()

    def test_two_publishers_serialize(self, core):
        f, store, publisher, reader, db = core
        store.store([f.make(), f.make(2, 1)])
        paused = PausedPublisher(store.engine, store.tables)
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(paused.publish_once, 1)
            try:
                assert paused.entered.wait(10)
                second = pool.submit(publisher.publish_once, 1)
                db.wait_for_lock()
                assert reader.watermark() == 0
                db.counts((2, 2, 0))
            finally:
                paused.release.set()
            assert first.result(10) == second.result(10) == 1
        assert [r.stream_seq for r in reader.stream(0, 2)] == [1, 2]
        db.invariants()

    def test_late_ingest_commit_after_watermark(self, core):
        f, store, publisher, reader, db = core
        paused = PausedStorage(store.engine, store.tables)
        with ThreadPoolExecutor(1) as pool:
            late = pool.submit(paused.store, [f.make()])
            try:
                assert paused.entered.wait(10)
                store.store([f.make(2)])
                assert publisher.publish_once() == 1
                watermark = reader.watermark()
                assert [r.event_id.int for r in reader.history(watermark)] == [2]
            finally:
                paused.release.set()
            late.result(10)
        assert publisher.publish_once() == 1
        assert reader.stream(watermark, reader.watermark())[0].event_id.int == 1
        assert [r.event_id.int for r in reader.history(watermark)] == [2]
        db.invariants()

    def test_uncommitted_publication_not_in_watermark(self, core):
        f, store, publisher, reader, db = core
        store.store([f.make()])
        paused = PausedPublisher(store.engine, store.tables)
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(paused.publish_once)
            try:
                assert paused.entered.wait(10)
                assert reader.watermark() == 0
                assert reader.stream(0, 1) == reader.history(1) == []
                db.counts((1, 1, 0))
                assert db.snapshot()[1][0].processed_at is None
            finally:
                paused.release.set()
            assert future.result(10) == 1
        assert reader.watermark() == 1
        db.invariants()

    def test_rollover_limit_and_overflow(self, core):
        f, store, publisher, reader, db = core
        store.store([f.make(), f.make(2, 1), f.make(3, 2)])
        assert publisher.publish_once(1) == 1
        assert publisher.publish_once(1) == 1
        with store.engine.begin() as connection:
            connection.execute(update(store.tables.state).values(last_seq=BIGINT_MAX - 1))
        store.store([f.make(4)])
        before_overflow = db.snapshot()
        with pytest.raises(OverflowError):
            publisher.publish_once(2)
        assert db.snapshot() == before_overflow
        assert publisher.publish_once(1) == 1
        assert reader.watermark() == BIGINT_MAX
        before = db.snapshot()
        with pytest.raises(OverflowError):
            publisher.publish_once(1)
        assert db.snapshot() == before
        db.invariants()

    def test_unique_publication_and_restrictive_event_delete(self, core):
        f, store, publisher, _, db = core
        store.store([f.make()])
        publisher.publish_once()
        before = db.snapshot()
        pk = before[0][0].id
        for statement in [
            insert(store.tables.publications).values(
                stream_seq=2, event_pk=pk, published_at=datetime.now(UTC)
            ),
            insert(store.tables.outbox).values(event_pk=pk),
            delete(store.tables.events).where(store.tables.events.c.id == pk),
        ]:
            with pytest.raises(IntegrityError), store.engine.begin() as connection:
                connection.execute(statement)
            assert db.snapshot() == before
        db.invariants()

    def test_fk_prevents_orphan_publication(self, core):
        _, store, _, _, db = core
        with pytest.raises(IntegrityError), store.engine.begin() as connection:
            connection.execute(
                insert(store.tables.publications).values(
                    stream_seq=1, event_pk=999, published_at=datetime.now(UTC)
                )
            )
        db.counts((0, 0, 0))
        db.invariants()


class TestReaderAndOriginal:
    def test_snapshot_keyset_excludes_late_publications(self, core):
        f, store, publisher, reader, db = core
        store.store([f.make(1), f.make(2, 1), f.make(3, 2)])
        publisher.publish_once()
        watermark = reader.watermark()
        first = reader.history(watermark, 1)
        store.store([f.make(4)])
        with store.engine.begin() as connection:
            connection.execute(
                update(store.tables.events)
                .where(store.tables.events.c.event_id == UUID(int=4))
                .values(received_at=first[0].received_at - timedelta(days=1))
            )
        publisher.publish_once()
        second = reader.history(watermark, 2, first[-1].history_key)
        assert {r.event_id.int for r in first + second} == {1, 2, 3}
        assert len(first + second) == 3
        assert reader.history(watermark, 2, second[-1].history_key) == []
        assert {r.event_id.int for r in reader.history(reader.watermark())} == {1, 2, 3, 4}
        db.invariants()

    def test_equal_received_at_identity_order(self, core):
        f, store, publisher, reader, db = core
        store.store([f.make(1), f.make(1, 1, agent=2), f.make(2, 2)])
        now = datetime.now(UTC)
        with store.engine.begin() as connection:
            connection.execute(update(store.tables.events).values(received_at=now))
        publisher.publish_once()
        rows = reader.history(3)
        assert [(r.event_id.int, r.agent_id.int) for r in rows] == [(2, 1), (1, 2), (1, 1)]
        assert reader.history(3, before=rows[1].history_key) == rows[2:]
        db.invariants()

    def test_filters_and_read_only(self, core):
        f, store, publisher, reader, db = core
        event = f.make()
        store.store([event])
        publisher.publish_once()
        before = db.snapshot()
        exact = EventFilters(
            event.deployment_id,
            event.occurred_at,
            event.occurred_at + timedelta(seconds=1),
            "192.0.2.23",
            "http.request",
        )
        assert len(reader.history(1, filters=exact)) == 1
        for filters in [
            replace(exact, to_time=event.occurred_at, from_time=None),
            replace(exact, from_time=event.occurred_at + timedelta(microseconds=1)),
            replace(exact, deployment_id=UUID(int=99)),
            replace(exact, source_ip="192.0.2.99"),
            replace(exact, event_type="session.closed"),
        ]:
            assert reader.history(1, filters=filters) == []
        assert db.snapshot() == before

    def test_mutable_caller_cannot_change_bytes(self, core):
        f, store, publisher, reader, db = core
        event = f.make()
        buffer = bytearray(event.canonical_bytes)
        event = replace(event, canonical_bytes=buffer)
        store.store([event])
        buffer.clear()
        original = event.original()
        original["data"].clear()
        publisher.publish_once()
        returned = reader.history(1)[0]
        returned.original()["data"].clear()
        assert returned.canonical_bytes == f.make().canonical_bytes
        assert db.snapshot()[0][0].canonical_bytes == f.make().canonical_bytes

    @pytest.mark.parametrize(
        "variant",
        [
            "unicode",
            "integer",
            "float",
            "negative_zero",
            "positive_zero",
            "array_ab",
            "array_ba",
            "password_a",
            "password_b",
        ],
    )
    def test_golden_bytes_preserved(self, core, variant):
        f, store, publisher, reader, db = core
        event = f.make(variant=variant)
        store.store([event])
        publisher.publish_once()
        assert reader.history(1)[0].canonical_bytes == event.canonical_bytes
        counterpart = {
            "integer": "float",
            "float": "integer",
            "negative_zero": "positive_zero",
            "positive_zero": "negative_zero",
            "array_ab": "array_ba",
            "array_ba": "array_ab",
            "password_a": "password_b",
            "password_b": "password_a",
            "unicode": "base",
        }[variant]
        before = db.snapshot()
        assert store.store([f.make(variant=counterpart)])[0].status == "EVENT_ID_CONFLICT"
        assert db.snapshot() == before
        db.invariants()
