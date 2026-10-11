"""Дополнительная файловая матрица: снимок, bounded read, ошибки и no-follow."""

from __future__ import annotations

import os
import subprocess
import traceback
import tracemalloc
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest
from test_low_journal_reader_contract import (
    DEPLOYMENT,
    INSTANCE,
    binding,
    event,
    footer,
    header,
    line,
    read,
    source,
)

from agent.telemetry.low_journal import JournalBinding, JournalReadError, LowJournalReader
from agent.telemetry.low_journal.source import JournalSource


def test_snapshot_fixed_on_enter_not_open(tmp_path: Path) -> None:
    path = source(tmp_path, header())
    context = LowJournalReader().open(path, binding())
    with path.open("ab") as writer:
        writer.write(line(event())[:-1])
    with context as stream:
        # Дописывание после __enter__ не расширяет этот проход даже при наличии LF.
        with path.open("ab") as writer:
            writer.write(b"\n" + line(footer()))
        assert [record.kind for record in stream] == ["header"]
        assert stream.summary.status == "open"
        assert stream.summary.next_offset == len(line(header()))
    records, summary = read(path)
    assert [record.kind for record in records] == ["header", "event", "footer"]
    assert summary.status == "closed"


def test_closed_snapshot_ignores_later_append(tmp_path: Path) -> None:
    path = source(tmp_path, header(), event(), footer())
    with LowJournalReader().open(path, binding()) as stream:
        with path.open("ab") as writer:
            writer.write(b"not-in-snapshot")
        assert len(list(stream)) == 3
        assert stream.summary.status == "closed"
    with pytest.raises(JournalReadError, match="INVALID_STRUCTURE"):
        read(path)


def test_replaced_name_does_not_switch_open_descriptor(tmp_path: Path) -> None:
    path = source(tmp_path, header(), event(), footer())
    original = path.read_bytes()
    archived = tmp_path / "archived.jsonl"
    with LowJournalReader().open(path, binding()) as stream:
        path.rename(archived)
        source(tmp_path, header(first_seq=8), footer(last_seq=7))
        records = list(stream)
        assert b"".join(record.raw for record in records) == original
        assert stream.summary.next_offset == len(original)
    records, _ = read(path)
    assert records[0].document()["first_seq"] == 8
    assert archived.read_bytes() == original


def test_large_file_uses_bounded_reads_and_memory(tmp_path: Path, monkeypatch: Any) -> None:
    path = tmp_path / "large.jsonl"
    total = 12000
    with path.open("wb") as writer:
        writer.write(line(header()))
        for seq in range(1, total + 1):
            writer.write(line(event(seq=seq)))
        writer.write(line(footer(last_seq=total)))
    size = path.stat().st_size
    original = JournalSource.read
    counts: list[int] = []

    def bounded(self: JournalSource, count: int) -> bytes:
        assert 0 < count <= 65537
        counts.append(count)
        return original(self, count)

    monkeypatch.setattr(JournalSource, "read", bounded)
    tracemalloc.start()
    try:
        with LowJournalReader().open(path, binding()) as stream:
            for _index, record in enumerate(stream, start=1):
                assert record.next_offset <= size
            assert _index == total + 2
            assert stream.summary.next_offset == size
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert sum(counts) == size
    assert peak < 1024 * 1024 < size


@pytest.mark.parametrize("delta,terminated", [(0, True), (1, True), (1, False)])
def test_default_physical_line_boundary(tmp_path: Path, delta: int, terminated: bool) -> None:
    raw = line(event())
    padded = raw[:-1] + b" " * (65536 + delta - len(raw)) + b"\n"
    if not terminated:
        padded = b"x" * 65537
    path = source(tmp_path, header(), padded)
    if delta == 0:
        records, _ = read(path)
        assert len(records[1].raw) == 65536
    else:
        with pytest.raises(JournalReadError) as caught:
            read(path)
        assert (caught.value.code, caught.value.offset) == ("LINE_TOO_LONG", len(line(header())))


@pytest.mark.parametrize("failure", ["truncate", "eof", "read", "stat"])
def test_source_failure_closes_descriptor(tmp_path: Path, monkeypatch: Any, failure: str) -> None:
    # Запись превышает размер порции чтения: ранний EOF нельзя скрыть prefetch-буфером.
    padded = line(event())[:-1] + b" " * 20000 + b"\n"
    path = source(tmp_path, header(), padded, footer())
    with LowJournalReader().open(path, binding()) as stream:
        descriptor = stream._source._fd
        assert next(stream).kind == "header"
        if failure == "truncate":
            path.write_bytes(b"")
        elif failure == "eof":
            monkeypatch.setattr(JournalSource, "read", lambda self, count: b"")
        elif failure == "read":
            monkeypatch.setattr(JournalSource, "read", fail_io)
        else:
            monkeypatch.setattr(JournalSource, "check_size", fail_io)
        with pytest.raises(JournalReadError) as caught:
            next(stream)
        expected = "SOURCE_CHANGED" if failure in {"truncate", "eof"} else "IO_ERROR"
        assert (caught.value.code, caught.value.offset) == (expected, len(line(header())))
        with pytest.raises(RuntimeError):
            _ = stream.summary
        with pytest.raises(OSError):
            os.fstat(descriptor)


def fail_io(*args: Any) -> Any:
    raise PermissionError("fixture-secret-in-system-error")


@pytest.mark.parametrize("failure", ["open", "stat", "missing"])
def test_open_errors_are_safe(tmp_path: Path, monkeypatch: Any, failure: str) -> None:
    path = tmp_path / "missing"
    if failure == "open":
        method = "_open_windows" if os.name == "nt" else "_open_posix"
        monkeypatch.setattr(JournalSource, method, fail_io)
    elif failure == "stat":
        source(tmp_path, header())
        path = tmp_path / "explicit-source.jsonl"
        monkeypatch.setattr(os, "fstat", fail_io)
    with pytest.raises(JournalReadError) as caught:
        read(path)
    assert (caught.value.code, caught.value.offset) == ("IO_ERROR", None)
    assert "fixture-secret" not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize(
    "bad",
    [
        b'{"secret":"fixture-secret-in-malformed-json"\n',
        line(event(event="request", payload_b64="fixture-secret-in-bad-base64")),
    ],
)
def test_parser_diagnostics_hide_source(tmp_path: Path, bad: bytes) -> None:
    with pytest.raises(JournalReadError) as caught:
        read(source(tmp_path, header(), bad))
    assert caught.value.code == "INVALID_RECORD"
    for rendered in (
        str(caught.value),
        repr(caught.value),
        "".join(traceback.format_exception(caught.value)),
    ):
        assert "fixture-secret" not in rendered


@pytest.mark.parametrize("exit_kind", ["break", "consumer", "parser", "exhausted"])
def test_context_closes_and_controls_summary(tmp_path: Path, exit_kind: str) -> None:
    bad = b"not-json\n" if exit_kind == "parser" else line(event())
    path = source(tmp_path, header(), bad, footer())
    context = LowJournalReader().open(path, binding())
    try:
        with context as stream:
            descriptor = stream._source._fd
            with pytest.raises(RuntimeError):
                _ = stream.summary
            if exit_kind == "break":
                for _ in stream:
                    break
            elif exit_kind == "consumer":
                next(stream)
                raise LookupError("ошибка потребителя")
            else:
                list(stream)
    except (LookupError, JournalReadError):
        assert exit_kind in {"consumer", "parser"}
    with pytest.raises(OSError):
        os.fstat(descriptor)
    if exit_kind == "exhausted":
        assert context.summary.status == "closed"
        assert list(context) == []
    else:
        with pytest.raises(RuntimeError):
            _ = context.summary
    with pytest.raises(RuntimeError):
        with context:
            pass


def test_public_results_immutable(tmp_path: Path) -> None:
    records, summary = read(source(tmp_path, header()))
    for value, attribute, replacement in [
        (binding(), "profile_revision", 2),
        (records[0], "raw", b"changed"),
        (summary, "next_offset", 0),
    ]:
        with pytest.raises(FrozenInstanceError):
            setattr(value, attribute, replacement)


def test_directory_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(JournalReadError, match="UNSAFE_PATH"):
        read(tmp_path)


def make_link(link: Path, target: Path) -> None:
    """Windows junction не требует привилегии создания symbolic link."""
    if os.name == "nt":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            check=True,
            capture_output=True,
            timeout=5,
        )
    else:
        link.symlink_to(target, target_is_directory=target.is_dir())


def test_direct_reparse_or_symlink_rejected_before_read(tmp_path: Path, monkeypatch: Any) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "secret").write_bytes(b"fixture-only")
    link = tmp_path / "link"
    make_link(link, target)
    monkeypatch.setattr(JournalSource, "read", lambda *args: pytest.fail("ссылка прочитана"))
    with pytest.raises(JournalReadError, match="UNSAFE_PATH"):
        read(link)
    assert (target / "secret").read_bytes() == b"fixture-only"


def test_final_component_replaced_before_kernel_open(tmp_path: Path, monkeypatch: Any) -> None:
    path = source(tmp_path, header())
    target = tmp_path / "target"
    target.mkdir()
    method = "_open_windows" if os.name == "nt" else "_open_posix"
    original = getattr(JournalSource, method)

    def replace(candidate: Path) -> int:
        candidate.unlink()
        make_link(candidate, target)
        return original(candidate)

    monkeypatch.setattr(JournalSource, method, staticmethod(replace))
    monkeypatch.setattr(JournalSource, "read", lambda *args: pytest.fail("подмена прочитана"))
    with pytest.raises(JournalReadError, match="UNSAFE_PATH"):
        read(path)


if os.name == "nt":

    def test_windows_non_disk_device_rejected() -> None:
        with pytest.raises(JournalReadError, match="UNSAFE_PATH"):
            read(Path("NUL"))

else:

    def test_windows_opener_rejected_on_posix(tmp_path: Path) -> None:
        # Чужая платформенная ветка отказывает до импорта и вызова Win32 API.
        with pytest.raises(JournalReadError, match="UNSAFE_PATH"):
            JournalSource._open_windows(tmp_path / "missing")

    def test_posix_fifo_rejected_without_blocking(tmp_path: Path) -> None:
        path = tmp_path / "pipe"
        os.mkfifo(path)
        with pytest.raises(JournalReadError, match="UNSAFE_PATH"):
            read(path)

    def test_posix_file_symlink_rejected(tmp_path: Path) -> None:
        target = source(tmp_path, header())
        link = tmp_path / "file-link"
        link.symlink_to(target)
        with pytest.raises(JournalReadError, match="UNSAFE_PATH"):
            read(link)


@pytest.mark.parametrize("value", [True, False, 0, -1, 1.5, "65536", None])
def test_invalid_limit_before_open(value: Any) -> None:
    with pytest.raises(ValueError):
        LowJournalReader(max_line_bytes=value)


@pytest.mark.parametrize("field", ["deployment_id", "instance_id", "profile_revision"])
@pytest.mark.parametrize("value", [True, 0, -1, 1.5, None, [], "invalid"])
def test_invalid_binding_before_open(field: str, value: Any) -> None:
    values = dict(deployment_id=DEPLOYMENT, instance_id=INSTANCE, profile_revision=1)
    values[field] = value
    with pytest.raises(ValueError):
        JournalBinding(**values)
