"""Read-only открытие под доверенным root до первого чтения исходных bytes."""

import os
import stat
import sys
from pathlib import Path

from agent.telemetry.low_journal.models import JournalReadError
from agent.telemetry.low_journal.source import JournalSource

from .models import CowrieJournalError, CowrieJournalSource


class CowrieFile:
    """POSIX удерживает каталоги через dir_fd; Win32 проверяет путь открытого HANDLE."""

    def __init__(self, source: CowrieJournalSource) -> None:
        self.fd: int | None = None
        path = source.root.joinpath(*source.relative_path.split("/"))
        try:
            self.fd = self._windows(path) if os.name == "nt" else self._posix(path)
            info = os.fstat(self.fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise CowrieJournalError("UNSAFE_PATH")
            self.identity = (str(info.st_dev), str(info.st_ino))
            self.size = info.st_size
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _posix(path: Path) -> int:
        if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
            raise CowrieJournalError("UNSAFE_PATH")
        flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_DIRECTORY", 0)
        parent = os.open(path.anchor, flags)
        try:
            for part in path.parent.parts[1:]:
                child = os.open(part, flags, dir_fd=parent)
                os.close(parent)
                parent = child
            return os.open(
                path.name, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0), dir_fd=parent
            )
        finally:
            os.close(parent)

    @staticmethod
    def _windows(path: Path) -> int:
        if sys.platform != "win32":
            raise CowrieJournalError("UNSAFE_PATH")
        import ctypes
        import msvcrt
        from ctypes import wintypes

        for ancestor in (*reversed(path.parent.parents), path.parent, path):
            info = ancestor.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise CowrieJournalError("UNSAFE_PATH")
        try:
            fd = JournalSource._open_windows(path)
        except JournalReadError:
            raise CowrieJournalError("UNSAFE_PATH") from None
        try:
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            final = kernel.GetFinalPathNameByHandleW
            final.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
            final.restype = wintypes.DWORD
            buffer = ctypes.create_unicode_buffer(32768)
            count = final(msvcrt.get_osfhandle(fd), buffer, len(buffer), 0)
            if not count or count >= len(buffer):
                raise CowrieJournalError("UNSAFE_PATH")
            actual = buffer.value.removeprefix("\\\\?\\")
            if os.path.normcase(actual) != os.path.normcase(os.path.abspath(path)):
                raise CowrieJournalError("UNSAFE_PATH")
            return fd
        except BaseException:
            os.close(fd)
            raise

    def read(self, count: int) -> bytes:
        assert self.fd is not None
        return os.read(self.fd, min(count, 4096))

    def check(self) -> None:
        """Рост допустим; усечение открытого snapshot запрещено."""
        assert self.fd is not None
        if os.fstat(self.fd).st_size < self.size:
            raise CowrieJournalError("SOURCE_TRUNCATED")

    def close(self) -> None:
        fd, self.fd = self.fd, None
        if fd is not None:
            os.close(fd)
