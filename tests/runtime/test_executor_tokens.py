"""Файловые гарантии Bearer проверяются без регистрации и настоящих секретов."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.runtime.executor.tokens import TokenFile, TokenFileError

TOKEN = "hf_agent_synthetic-token-for-tests"


def test_reserved_file_is_exclusive_and_persisted(tmp_path: Path) -> None:
    target = tmp_path / "agent.token"
    destination = TokenFile(target)
    with destination.reserve() as reserved:
        assert target.exists() and target.read_bytes() == b""
        with pytest.raises(TokenFileError, match="TOKEN_FILE_EXISTS"):
            destination.reserve().__enter__()
        reserved.write(TOKEN)
        with pytest.raises(TokenFileError, match="TOKEN_FILE_RESERVATION_USED"):
            reserved.write(TOKEN)
    assert destination.read() == TOKEN
    assert target.read_bytes() == (TOKEN + "\n").encode()
    if os.name == "posix":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert target.stat().st_uid == os.geteuid()


def test_existing_file_is_not_overwritten(tmp_path: Path) -> None:
    target = tmp_path / "agent.token"
    target.write_text("existing")
    with pytest.raises(TokenFileError, match="TOKEN_FILE_EXISTS"):
        with TokenFile(target).reserve():
            pytest.fail("Существующий файл не должен допустить регистрацию")
    assert target.read_text() == "existing"


def test_uncommitted_reservation_is_removed(tmp_path: Path) -> None:
    target = tmp_path / "agent.token"
    with pytest.raises(RuntimeError):
        with TokenFile(target).reserve():
            raise RuntimeError("Синтетическая ошибка центра")
    assert not target.exists()


@pytest.mark.parametrize(
    "token",
    ["", "other-prefix", "hf_agent_bad\nsecret", "hf_agent_\u00e9", "hf_agent_" + "x" * 120],
)
def test_bad_token_is_never_persisted_or_exposed(tmp_path: Path, token: str) -> None:
    target = tmp_path / "agent.token"
    with pytest.raises(TokenFileError) as failure:
        with TokenFile(target).reserve() as destination:
            destination.write(token)
    assert not target.exists()
    assert token not in str(failure.value) if token else True


@pytest.mark.parametrize(
    "raw", [b"", b"hf_agent_a\n\n", b"hf_agent_a\r\n", b"x" * 130, b"hf_agent_\xff"]
)
def test_bad_file_content_is_safe(tmp_path: Path, raw: bytes) -> None:
    target = tmp_path / "agent.token"
    target.write_bytes(raw)
    target.chmod(0o600)
    with pytest.raises(TokenFileError) as failure:
        TokenFile(target).read()
    assert str(target) not in str(failure.value)
    assert target.read_bytes() == raw


def test_read_accepts_token_without_trailing_newline(tmp_path: Path) -> None:
    target = tmp_path / "agent.token"
    target.write_text(TOKEN)
    target.chmod(0o600)
    assert TokenFile(target).read() == TOKEN


def test_token_path_rejects_directory_and_hardlinks(tmp_path: Path) -> None:
    with pytest.raises(TokenFileError, match="UNSAFE_TOKEN_PATH"):
        TokenFile(tmp_path).read()
    target = tmp_path / "agent.token"
    target.write_text(TOKEN)
    target.chmod(0o600)
    os.link(target, tmp_path / "link.token")
    with pytest.raises(TokenFileError, match="UNSAFE_TOKEN_PATH"):
        TokenFile(target).read()


@pytest.mark.skipif(os.name != "posix", reason="POSIX режим, владелец и безопасные symlink")
def test_posix_permissions_owner_and_symlink_are_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "agent.token"
    target.write_text(TOKEN)
    target.chmod(0o640)
    with pytest.raises(TokenFileError, match="UNSAFE_TOKEN_PERMISSIONS"):
        TokenFile(target).read()
    target.chmod(0o600)
    monkeypatch.setattr(os, "geteuid", lambda: target.stat().st_uid + 1)
    with pytest.raises(TokenFileError, match="UNSAFE_TOKEN_PERMISSIONS"):
        TokenFile._check(target.stat(), directory=False)
    monkeypatch.undo()
    alias = tmp_path / "alias.token"
    alias.symlink_to(target)
    with pytest.raises(TokenFileError, match="UNSAFE_TOKEN_PATH"):
        TokenFile(alias).read()
    with pytest.raises(TokenFileError, match="TOKEN_FILE_EXISTS"):
        TokenFile(alias).reserve().__enter__()
    linked_parent = tmp_path / "alias-dir"
    linked_parent.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(TokenFileError):
        TokenFile(linked_parent / "new.token").reserve().__enter__()
    assert not (tmp_path / "new.token").exists()


def test_fsync_failure_never_leaves_usable_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "agent.token"
    with TokenFile(target).reserve() as reservation:
        monkeypatch.setattr(
            os, "fsync", lambda descriptor: (_ for _ in ()).throw(OSError("PRIVATE"))
        )
        with pytest.raises(TokenFileError, match="TOKEN_FILE_WRITE_FAILED") as failure:
            reservation.write(TOKEN)
        assert "PRIVATE" not in str(failure.value)
    assert not target.exists()


def test_fsync_is_called_before_token_is_committed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    original = os.fsync

    def observed(descriptor: int) -> None:
        calls.append(descriptor)
        original(descriptor)

    monkeypatch.setattr(os, "fsync", observed)
    with TokenFile(tmp_path / "agent.token").reserve() as reservation:
        before = len(calls)
        reservation.write(TOKEN)
    assert before >= 1 and len(calls) >= 2 * before


def test_partial_writes_are_completed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = os.write
    monkeypatch.setattr(os, "write", lambda descriptor, raw: original(descriptor, raw[:3]))
    target = tmp_path / "agent.token"
    with TokenFile(target).reserve() as destination:
        destination.write(TOKEN)
    assert TokenFile(target).read() == TOKEN


def test_zero_byte_write_is_safe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "agent.token"
    with TokenFile(target).reserve() as destination:
        monkeypatch.setattr(os, "write", lambda descriptor, raw: 0)
        with pytest.raises(TokenFileError, match="TOKEN_FILE_WRITE_FAILED"):
            destination.write(TOKEN)
    assert not target.exists()


def test_windows_reparse_point_is_rejected_without_opening() -> None:
    info = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_file_attributes=0x400)
    with pytest.raises(TokenFileError, match="UNSAFE_TOKEN_PATH"):
        TokenFile._check(info, directory=False)


def test_missing_parent_is_safe_and_not_created(tmp_path: Path) -> None:
    target = tmp_path / "missing" / "agent.token"
    with pytest.raises(TokenFileError, match="TOKEN_FILE_CREATE_FAILED") as failure:
        TokenFile(target).reserve().__enter__()
    assert str(target) not in str(failure.value) and not target.parent.exists()


def test_parent_traversal_is_rejected_before_path_normalization(tmp_path: Path) -> None:
    with pytest.raises(TokenFileError, match="UNSAFE_TOKEN_PATH"):
        TokenFile(tmp_path / "alias" / ".." / "agent.token")


def test_windows_alternate_stream_and_drive_relative_paths_are_rejected() -> None:
    if os.name == "nt":
        for path in ("C:relative.token", "agent.token:stream", "\\\\server\\share\\agent.token"):
            with pytest.raises(TokenFileError, match="UNSAFE_TOKEN_PATH"):
                TokenFile(path)


@pytest.mark.skipif(os.name != "posix", reason="POSIX FIFO и symlink предков")
def test_fifo_and_symlink_in_parent_are_rejected_before_read(tmp_path: Path) -> None:
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(TokenFileError, match="UNSAFE_TOKEN_PATH"):
        TokenFile(fifo).read()
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    with TokenFile(private / "agent.token").reserve() as destination:
        destination.write(TOKEN)
    alias = tmp_path / "alias"
    alias.symlink_to(private, target_is_directory=True)
    with pytest.raises(TokenFileError):
        TokenFile(alias / "agent.token").read()
