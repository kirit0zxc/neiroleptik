"""Открытие обычного файла без следования ссылке в конечном компоненте пути."""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

from .models import JournalReadError


class JournalSource:
    """Владеет read-only дескриптором; предки пути считаются доверенными."""

    def __init__(self, path: Path) -> None:
        self._fd: int | None = None
        try:
            descriptor = self._open_windows(path) if os.name == "nt" else self._open_posix(path)
            self._fd = descriptor
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise JournalReadError("UNSAFE_PATH", None)
            self.size = info.st_size
        except (OSError, JournalReadError):
            self.close()
            raise

    @staticmethod
    def _open_posix(path: Path) -> int:
        if not hasattr(os, "O_NOFOLLOW"):
            raise JournalReadError("UNSAFE_PATH", None)
        try:
            # NONBLOCK исключает зависание на FIFO до проверки открытого дескриптора.
            return os.open(
                path,
                os.O_RDONLY
                | os.O_NOFOLLOW
                | getattr(os, "O_NONBLOCK", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise JournalReadError("UNSAFE_PATH", None) from None
            raise

    @staticmethod
    def _open_windows(path: Path) -> int:
        """Win32 открывает сам reparse point; атрибуты проверяются до чтения bytes."""
        import ctypes
        import msvcrt
        from ctypes import wintypes

        class AttributeTag(ctypes.Structure):
            _fields_ = [("attributes", wintypes.DWORD), ("tag", wintypes.DWORD)]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        create = kernel.CreateFileW
        create.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        create.restype = wintypes.HANDLE
        attributes = kernel.GetFileInformationByHandleEx
        attributes.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        attributes.restype = wintypes.BOOL
        file_type = kernel.GetFileType
        file_type.argtypes = [wintypes.HANDLE]
        file_type.restype = wintypes.DWORD
        close = kernel.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        close.restype = wintypes.BOOL
        # Только GENERIC_READ; совместный доступ не мешает писателю и смене имени файла.
        handle = create(os.fspath(path), 0x80000000, 7, None, 3, 0x00200000 | 0x02000000, None)
        if handle == ctypes.c_void_p(-1).value:
            raise OSError(ctypes.get_last_error(), "Не удалось открыть источник")
        try:
            if file_type(handle) != 1:  # FILE_TYPE_DISK
                raise JournalReadError("UNSAFE_PATH", None)
            tag = AttributeTag()
            if not attributes(handle, 9, ctypes.byref(tag), ctypes.sizeof(tag)):
                raise JournalReadError("UNSAFE_PATH", None)
            if tag.attributes & (0x400 | 0x10):  # REPARSE_POINT или DIRECTORY
                raise JournalReadError("UNSAFE_PATH", None)
            descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY | os.O_NOINHERIT)
        except (OSError, JournalReadError):
            close(handle)
            raise
        # Владение HANDLE передано CRT; закрывать его теперь следует только через os.close.
        return descriptor

    def check_size(self, offset: int) -> None:
        """Рост допустим, но уменьшение исходного снимка запрещает успешный итог."""
        assert self._fd is not None
        if os.fstat(self._fd).st_size < self.size:
            raise JournalReadError("SOURCE_CHANGED", offset)

    def read(self, count: int) -> bytes:
        assert self._fd is not None
        return os.read(self._fd, count)

    def close(self) -> None:
        descriptor, self._fd = self._fd, None
        if descriptor is not None:
            os.close(descriptor)
