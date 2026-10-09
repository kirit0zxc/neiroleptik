"""Файловый журнал Low: заголовок, концовка, ротация, квота и устойчивость к ошибкам записи."""

from __future__ import annotations

import errno
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from honeypots.low.journal import MIN_FILE_BYTES, JournalSettings, RotatingJournalSink

INSTANCE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OTHER_INSTANCE = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
DEPLOYMENT = "11111111-1111-4111-8111-111111111111"
NAME = re.compile(rf"low-{INSTANCE}-(\d{{6}})\.jsonl")


def settings(directory: Path, **changes: Any) -> JournalSettings:
    values: dict[str, Any] = {
        "directory": directory,
        "instance_id": INSTANCE,
        "deployment_id": DEPLOYMENT,
        "profile_revision": 3,
    }
    values.update(changes)
    return JournalSettings(**values)


def event(seq: int, payload_size: int = 0, **extra: Any) -> dict[str, Any]:
    return {
        "seq": seq,
        "event": "request",
        "timestamp": "2026-10-09T12:00:00Z",
        "connection_id": str(uuid4()),
        "protocol": "http",
        "service": {"protocol": "http", "port": 8800},
        "peer": {"host": "203.0.113.5", "port": 40000 + seq},
        "payload_b64": "A" * payload_size,
        **extra,
    }


def files(directory: Path, instance: str = INSTANCE) -> list[Path]:
    return sorted(directory.glob(f"low-{instance}-*.jsonl"))


def records(path: Path) -> list[dict[str, Any]]:
    raw = path.read_bytes()
    assert raw.endswith(b"\n"), "завершённая запись оканчивается LF"
    lines = raw.decode("ascii").split("\n")[:-1]
    assert all(lines), "пустых строк нет"
    return [json.loads(line) for line in lines]


class FixedClock:
    def __init__(self) -> None:
        self.ticks = 0

    def __call__(self) -> datetime:
        self.ticks += 1
        return datetime(2026, 10, 9, 12, 0, self.ticks, tzinfo=UTC)


# --- Настройки ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "changes",
    [
        {"instance_id": "nope"},
        {"deployment_id": 5},
        {"profile_revision": 0},
        {"profile_revision": True},
        {"max_file_bytes": MIN_FILE_BYTES - 1},
        {"max_file_bytes": True},
        {"max_total_bytes": MIN_FILE_BYTES - 1},
    ],
    ids=[
        "instance",
        "deployment-type",
        "revision-zero",
        "revision-bool",
        "file-small",
        "file-bool",
        "quota-below-file",
    ],
)
def test_invalid_settings_are_rejected(tmp_path: Path, changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        settings(tmp_path, **changes)


def test_ids_are_canonicalised(tmp_path: Path) -> None:
    result = settings(tmp_path, instance_id=INSTANCE.upper())

    assert result.instance_id == INSTANCE


# --- Заголовок, события, концовка -----------------------------------------------------


def test_open_creates_a_file_with_a_complete_header(tmp_path: Path) -> None:
    sink = RotatingJournalSink(settings(tmp_path), clock=FixedClock())

    sink.open()

    [path] = files(tmp_path)
    assert path.name == f"low-{INSTANCE}-000001.jsonl"
    [header] = records(path)
    assert header == {
        "record": "header",
        "schema": "honeyforge-low-journal/1",
        "journal_id": header["journal_id"],
        "journal_seq": 1,
        "previous_journal_id": None,
        "instance_id": INSTANCE,
        "deployment_id": DEPLOYMENT,
        "profile_revision": 3,
        "opened_at": "2026-10-09T12:00:01Z",
        "first_seq": 1,
    }
    assert str(UUID(header["journal_id"])) == header["journal_id"]
    assert sink.current_journal_id == header["journal_id"]


def test_events_are_written_in_order_with_record_marker(tmp_path: Path) -> None:
    sink = RotatingJournalSink(settings(tmp_path))
    sink.open()

    for seq in (1, 2, 3):
        sink.emit(event(seq, payload_size=seq))
    sink.close()

    [path] = files(tmp_path)
    header, *events, footer = records(path)
    assert [e["seq"] for e in events] == [1, 2, 3]
    assert {e["record"] for e in events} == {"event"}
    assert events[0]["service"] == {"protocol": "http", "port": 8800}
    assert footer["record"] == "footer"
    assert (footer["journal_id"], footer["last_seq"], footer["reason"]) == (
        header["journal_id"],
        3,
        "shutdown",
    )


def test_footer_of_an_empty_file_has_last_seq_before_first_seq(tmp_path: Path) -> None:
    sink = RotatingJournalSink(settings(tmp_path))
    sink.open()
    sink.close()

    header, footer = records(files(tmp_path)[0])

    assert header["first_seq"] == 1 and footer["last_seq"] == 0


def test_lines_are_ascii_json_without_embedded_newlines(tmp_path: Path) -> None:
    sink = RotatingJournalSink(settings(tmp_path))
    sink.open()
    sink.emit(event(1, text="строка\nс переводом\r\nи Unicode ёж"))
    sink.close()

    raw = files(tmp_path)[0].read_bytes()

    raw.decode("ascii")
    assert raw.count(b"\n") == 3 and b"\r" not in raw
    assert records(files(tmp_path)[0])[1]["text"] == "строка\nс переводом\r\nи Unicode ёж"


def test_close_is_idempotent_and_emit_after_close_starts_a_new_generation(tmp_path: Path) -> None:
    sink = RotatingJournalSink(settings(tmp_path))
    sink.open()
    sink.emit(event(1))
    sink.close()
    sink.close()
    sink.emit(event(2))
    sink.close()

    first, second = files(tmp_path)
    first_header, _, first_footer = records(first)
    second_header, second_event, _ = records(second)
    assert second_header["previous_journal_id"] == first_header["journal_id"]
    assert (second_header["journal_seq"], second_header["first_seq"]) == (2, 2)
    assert second_event["seq"] == 2 and first_footer["last_seq"] == 1


def test_close_without_open_does_nothing(tmp_path: Path) -> None:
    RotatingJournalSink(settings(tmp_path)).close()

    assert files(tmp_path) == []


@pytest.mark.parametrize("seq", [None, 0, -1, True, 1.5, "1"], ids=repr)
def test_event_without_valid_seq_is_refused(tmp_path: Path, seq: Any) -> None:
    sink = RotatingJournalSink(settings(tmp_path))
    sink.open()
    bad = event(1)
    bad["seq"] = seq

    with pytest.raises(ValueError):
        sink.emit(bad)

    assert len(records_without_check(files(tmp_path)[0])) == 1


def records_without_check(path: Path) -> list[str]:
    return path.read_bytes().decode("ascii").splitlines()


def test_oversized_record_is_refused_without_touching_the_file(tmp_path: Path) -> None:
    sink = rotating(tmp_path)

    with pytest.raises(ValueError):
        sink.emit(event(1, payload_size=MIN_FILE_BYTES // 2))

    assert len(records_without_check(files(tmp_path)[0])) == 1


def test_missing_directory_fails_at_open(tmp_path: Path) -> None:
    sink = RotatingJournalSink(settings(tmp_path / "absent"))

    with pytest.raises(OSError):
        sink.open()


@pytest.mark.skipif(sys.platform == "win32", reason="права POSIX")
def test_files_are_not_world_readable(tmp_path: Path) -> None:
    sink = RotatingJournalSink(settings(tmp_path))
    sink.open()

    assert files(tmp_path)[0].stat().st_mode & 0o007 == 0
    sink.close()


# --- Ротация -----------------------------------------------------------------------


def rotating(tmp_path: Path, **changes: Any) -> RotatingJournalSink:
    sink = RotatingJournalSink(settings(tmp_path, max_file_bytes=MIN_FILE_BYTES, **changes))
    sink.open()
    return sink


def test_rotation_keeps_seq_continuous_and_links_generations(tmp_path: Path) -> None:
    sink = rotating(tmp_path)
    big = 60_000  # около четырёх записей на файл

    for seq in range(1, 13):
        sink.emit(event(seq, payload_size=big))
    sink.close()

    paths = files(tmp_path)
    assert len(paths) >= 3
    parsed = [records(path) for path in paths]
    all_seq = [r["seq"] for chunk in parsed for r in chunk if r["record"] == "event"]
    assert all_seq == list(range(1, 13))
    for index, chunk in enumerate(parsed):
        header, footer = chunk[0], chunk[-1]
        assert header["journal_seq"] == index + 1
        assert NAME.fullmatch(paths[index].name).group(1) == f"{index + 1:06d}"  # type: ignore[union-attr]
        assert footer["journal_id"] == header["journal_id"]
        events = [r for r in chunk if r["record"] == "event"]
        assert events, "файл не пуст"
        assert header["first_seq"] == events[0]["seq"]
        assert footer["last_seq"] == events[-1]["seq"]
        assert footer["reason"] == ("shutdown" if index == len(parsed) - 1 else "rotation")
        expected_previous = None if index == 0 else parsed[index - 1][0]["journal_id"]
        assert header["previous_journal_id"] == expected_previous


def test_no_file_exceeds_its_size_limit(tmp_path: Path) -> None:
    sink = rotating(tmp_path)

    for seq in range(1, 40):
        sink.emit(event(seq, payload_size=30_000))
    sink.close()

    assert max(path.stat().st_size for path in files(tmp_path)) <= MIN_FILE_BYTES


def test_journal_ids_are_unique(tmp_path: Path) -> None:
    sink = rotating(tmp_path)

    for seq in range(1, 20):
        sink.emit(event(seq, payload_size=60_000))
    sink.close()

    ids = [records(path)[0]["journal_id"] for path in files(tmp_path)]
    assert len(ids) == len(set(ids)) > 2


# --- Нумерация и чужие файлы -----------------------------------------------------------


def test_numbering_continues_after_existing_files_and_never_overwrites(tmp_path: Path) -> None:
    existing = tmp_path / f"low-{INSTANCE}-000005.jsonl"
    existing.write_bytes(b"keep me\n")

    sink = RotatingJournalSink(settings(tmp_path))
    sink.open()
    sink.close()

    assert existing.read_bytes() == b"keep me\n"
    assert [p.name for p in files(tmp_path)] == [
        f"low-{INSTANCE}-000005.jsonl",
        f"low-{INSTANCE}-000006.jsonl",
    ]


def test_other_instances_files_do_not_affect_numbering(tmp_path: Path) -> None:
    (tmp_path / f"low-{OTHER_INSTANCE}-000009.jsonl").write_bytes(b"x" * 1000)
    sink = RotatingJournalSink(settings(tmp_path))
    sink.open()
    sink.close()

    assert files(tmp_path)[0].name == f"low-{INSTANCE}-000001.jsonl"
    assert (tmp_path / f"low-{OTHER_INSTANCE}-000009.jsonl").read_bytes() == b"x" * 1000


# --- Квота -------------------------------------------------------------------------


def test_quota_refuses_writes_and_recovers_when_the_collector_frees_space(
    tmp_path: Path,
) -> None:
    sink = RotatingJournalSink(
        settings(tmp_path, max_file_bytes=MIN_FILE_BYTES, max_total_bytes=MIN_FILE_BYTES)
    )
    sink.open()
    seq = 0
    with pytest.raises(OSError) as error:
        while True:
            seq += 1
            sink.emit(event(seq, payload_size=60_000))
    assert error.value.errno == errno.ENOSPC
    written = seq - 1
    assert written >= 1
    stored = [r["seq"] for p in files(tmp_path) for r in records(p) if r["record"] == "event"]
    assert stored == list(range(1, written + 1)), "отказавшее событие не записано"

    # Сборщик обработал и удалил закрытый файл.
    for path in files(tmp_path)[:-1]:
        path.unlink()
    for path in files(tmp_path):
        if not records_without_check(path)[-1].startswith('{"record":"footer"'):
            continue
        path.unlink()
    sink.emit(event(seq, payload_size=10))
    sink.close()

    last_records = [r for p in files(tmp_path) for r in records(p)]
    assert any(r["record"] == "event" and r["seq"] == seq for r in last_records)


def test_quota_counts_all_matching_files_of_the_directory(tmp_path: Path) -> None:
    (tmp_path / f"low-{OTHER_INSTANCE}-000001.jsonl").write_bytes(b"x" * (MIN_FILE_BYTES - 200))
    sink = RotatingJournalSink(
        settings(tmp_path, max_file_bytes=MIN_FILE_BYTES, max_total_bytes=MIN_FILE_BYTES)
    )

    with pytest.raises(OSError) as error:
        sink.open()

    assert error.value.errno == errno.ENOSPC
    assert files(tmp_path) == []


def test_quota_ignores_files_that_are_not_journals(tmp_path: Path) -> None:
    (tmp_path / "unrelated.bin").write_bytes(b"y" * (MIN_FILE_BYTES * 4))
    (tmp_path / f"low-{INSTANCE}-notanumber.jsonl").write_bytes(b"z" * (MIN_FILE_BYTES * 4))
    sink = RotatingJournalSink(
        settings(tmp_path, max_file_bytes=MIN_FILE_BYTES, max_total_bytes=MIN_FILE_BYTES)
    )

    sink.open()
    sink.emit(event(1))
    sink.close()

    real = [path for path in files(tmp_path) if NAME.fullmatch(path.name)]
    assert [path.name for path in real] == [f"low-{INSTANCE}-000001.jsonl"]


def test_quota_accounts_the_file_being_written_exactly(tmp_path: Path) -> None:
    """Регрессия: на Windows запись каталога отстаёт от размера открытого файла."""
    sink = RotatingJournalSink(
        settings(tmp_path, max_file_bytes=MIN_FILE_BYTES, max_total_bytes=MIN_FILE_BYTES)
    )
    sink.open()
    seq = 0
    with pytest.raises(OSError) as error:
        while True:
            seq += 1
            sink.emit(event(seq, payload_size=20_000))
    assert error.value.errno == errno.ENOSPC
    sink.close()

    on_disk = sum(path.stat().st_size for path in files(tmp_path))
    assert on_disk <= MIN_FILE_BYTES, "суммарный размер файлов не превышает квоту"
    assert records(files(tmp_path)[-1])[-1]["record"] == "footer", "концовка поместилась"


# --- Ошибки записи -------------------------------------------------------------------


def test_write_error_abandons_the_file_without_footer_and_next_event_opens_a_new_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sink = RotatingJournalSink(settings(tmp_path, fsync_each_event=True))
    sink.open()
    sink.emit(event(1))
    real_fsync = os.fsync
    calls = {"count": 0}

    def flaky_fsync(descriptor: int) -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            raise OSError(errno.EIO, "сбой диска")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", flaky_fsync)

    with pytest.raises(OSError):
        sink.emit(event(2))
    assert sink.current_journal_id is None
    sink.emit(event(3))
    sink.close()

    first, second = files(tmp_path)
    first_records = records(first)
    assert first_records[-1]["record"] != "footer", "оборванный файл без концовки"
    assert [r["seq"] for r in first_records if r["record"] == "event"] == [1], (
        "неподтверждённая запись 2 откатана, а не оставлена в файле"
    )
    second_header, third, footer = records(second)
    assert second_header["previous_journal_id"] == first_records[0]["journal_id"]
    assert (second_header["first_seq"], third["seq"], footer["last_seq"]) == (3, 3, 3)


def test_fsync_each_event_is_optional(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    synced: list[int] = []
    monkeypatch.setattr(os, "fsync", lambda descriptor: synced.append(descriptor))

    quiet = RotatingJournalSink(settings(tmp_path))
    quiet.open()
    quiet.emit(event(1))
    assert synced == []
    quiet.close()
    assert len(synced) == 1, "fsync выполняется при закрытии"

    strict = RotatingJournalSink(
        settings(tmp_path, instance_id=OTHER_INSTANCE, fsync_each_event=True)
    )
    strict.open()
    synced.clear()
    strict.emit(event(1))
    assert len(synced) == 1
    strict.close()


# --- Регрессии по итогам ревью PR3 ------------------------------------------------------------


def test_quota_refusal_keeps_writing_into_the_same_file_after_space_is_freed(
    tmp_path: Path,
) -> None:
    blocker = tmp_path / f"low-{OTHER_INSTANCE}-000001.jsonl"
    blocker.write_bytes(b"x" * (MIN_FILE_BYTES - 2000))
    sink = RotatingJournalSink(
        settings(tmp_path, max_file_bytes=MIN_FILE_BYTES, max_total_bytes=MIN_FILE_BYTES)
    )
    sink.open()
    journal_id = sink.current_journal_id
    with pytest.raises(OSError) as error:
        sink.emit(event(1, payload_size=3000))
    assert error.value.errno == errno.ENOSPC

    blocker.unlink()  # сборщик обработал чужой файл
    sink.emit(event(2))
    sink.close()

    assert sink.current_journal_id is None
    [path] = files(tmp_path)
    header, written, footer = records(path)
    assert header["journal_id"] == journal_id, "отказ по квоте не открывает новое поколение"
    assert (written["seq"], footer["last_seq"]) == (2, 2)


def test_refused_generation_does_not_consume_its_number(tmp_path: Path) -> None:
    blocker = tmp_path / f"low-{OTHER_INSTANCE}-000001.jsonl"
    blocker.write_bytes(b"x" * MIN_FILE_BYTES)
    sink = RotatingJournalSink(
        settings(tmp_path, max_file_bytes=MIN_FILE_BYTES, max_total_bytes=MIN_FILE_BYTES)
    )
    for _ in range(3):
        with pytest.raises(OSError):
            sink.open()

    blocker.unlink()
    sink.open()
    sink.close()

    [path] = files(tmp_path)
    assert path.name == f"low-{INSTANCE}-000001.jsonl"
    assert records(path)[0]["journal_seq"] == 1


def test_file_deleted_by_the_collector_during_the_scan_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Регрессия: удаление файла между чтением каталога и stat роняло запись."""
    vanished = tmp_path / f"low-{OTHER_INSTANCE}-000001.jsonl"
    vanished.write_bytes(b"x" * (MIN_FILE_BYTES * 2))
    real_stat = os.stat

    def racing_stat(target: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        if Path(target) == vanished:
            raise FileNotFoundError(errno.ENOENT, "удалён сборщиком", str(target))
        return real_stat(target, *args, **kwargs)

    monkeypatch.setattr(os, "stat", racing_stat)
    sink = RotatingJournalSink(
        settings(tmp_path, max_file_bytes=MIN_FILE_BYTES, max_total_bytes=MIN_FILE_BYTES)
    )

    sink.open()
    sink.emit(event(1))
    sink.close()

    assert len(files(tmp_path)) == 1


def test_footer_space_is_reserved_inside_the_quota(tmp_path: Path) -> None:
    """Концовка не упирается в квоту: после отказа все файлы закрываются и квота соблюдена."""
    sink = RotatingJournalSink(
        settings(tmp_path, max_file_bytes=MIN_FILE_BYTES, max_total_bytes=MIN_FILE_BYTES)
    )
    sink.open()
    seq = 0
    with pytest.raises(OSError) as error:
        while True:
            seq += 1
            sink.emit(event(seq, payload_size=1000))
    assert error.value.errno == errno.ENOSPC

    sink.close()

    paths = files(tmp_path)
    assert all(records(path)[-1]["record"] == "footer" for path in paths)
    assert sum(path.stat().st_size for path in paths) <= MIN_FILE_BYTES


@pytest.mark.parametrize("failure", ["footer-write", "footer-fsync"])
def test_failed_footer_is_rolled_back_and_shutdown_reports_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """Регрессия: ошибка fsync оставляла полную концовку и выдавала успешное закрытие."""
    sink = RotatingJournalSink(settings(tmp_path))
    sink.open()
    sink.emit(event(1))
    [path] = files(tmp_path)
    confirmed = path.read_bytes()
    real_write = sink._write_all
    real_fsync = os.fsync

    def failing_write(file: Any, data: bytes) -> None:
        real_write(file, data[:17])
        raise OSError(errno.EIO, "сбой записи концовки")

    def failing_fsync(descriptor: int) -> None:
        raise OSError(errno.EIO, "сбой fsync концовки")

    if failure == "footer-write":
        monkeypatch.setattr(sink, "_write_all", failing_write)
    else:
        monkeypatch.setattr(os, "fsync", failing_fsync)

    with pytest.raises(OSError):
        sink.close()
    assert sink.current_journal_id is None
    assert path.read_bytes() == confirmed
    assert all(record["record"] != "footer" for record in records(path))
    sink.close()  # Повтор закрытия не повторяет неудачную запись.

    monkeypatch.setattr(sink, "_write_all", real_write)
    monkeypatch.setattr(os, "fsync", real_fsync)
    sink.emit(event(2))
    sink.close()
    old, new = files(tmp_path)
    assert records(new)[0]["previous_journal_id"] == records(old)[0]["journal_id"]
    assert records(new)[-1]["record"] == "footer"


def test_rotation_failure_does_not_report_the_next_event_as_saved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sink = RotatingJournalSink(
        settings(tmp_path, max_file_bytes=MIN_FILE_BYTES, max_total_bytes=MIN_FILE_BYTES * 2)
    )
    sink.open()
    # Две записи по 100000 байт помещаются в файл 256 КиБ; третья требует ротации.
    sink.emit(event(1, payload_size=100_000))
    sink.emit(event(2, payload_size=100_000))
    [path] = files(tmp_path)
    confirmed = path.read_bytes()

    def failed_fsync(descriptor: int) -> None:
        raise OSError(errno.EIO, "сбой ротации")

    monkeypatch.setattr(os, "fsync", failed_fsync)
    with pytest.raises(OSError):
        sink.emit(event(3, payload_size=100_000))
    assert files(tmp_path) == [path]
    assert path.read_bytes() == confirmed
    assert sink.current_journal_id is None
