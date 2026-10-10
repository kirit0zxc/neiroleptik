"""Закрытый файл agent token: без ссылок, перезаписи и раскрытия ввода в ошибках."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from types import TracebackType


class TokenFileError(Exception):
    """Код ошибки не содержит путь и секретное содержимое файла."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"Файл токена: {code}")


def validate_agent_token(token: object) -> str:
    """Bearer согласован с authenticate центра: префикс, длина и ASCII без пробелов."""
    if (
        not isinstance(token, str)
        or not token.startswith("hf_agent_")
        or not 9 < len(token) <= 128
        or any(ord(character) <= 32 or ord(character) >= 127 for character in token)
    ):
        raise TokenFileError("INVALID_AGENT_TOKEN")
    return token


class TokenFile:
    """POSIX использует dir_fd и O_NOFOLLOW; Windows дополнительно проверяет reparse."""

    def __init__(self, path: str | Path) -> None:
        try:
            raw = os.fspath(path)
            if not raw or "\x00" in raw:
                raise ValueError
            requested = Path(raw)
            if ".." in requested.parts or (
                os.name == "nt" and requested.drive and not requested.is_absolute()
            ):
                raise ValueError
            self._path = Path(os.path.abspath(raw))
            if os.name == "nt":
                # UNC не является локальным диском, а ADS не является отдельным файлом.
                if self._path.drive.startswith("\\\\") or any(
                    ":" in part for part in self._path.parts[1:]
                ):
                    raise ValueError
        except (TypeError, ValueError, OSError):
            raise TokenFileError("UNSAFE_TOKEN_PATH") from None

    @staticmethod
    def _check(info: os.stat_result, *, directory: bool) -> None:
        reparse = getattr(info, "st_file_attributes", 0) & getattr(
            stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
        )
        expected = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
        if stat.S_ISLNK(info.st_mode) or reparse or not expected:
            raise TokenFileError("UNSAFE_TOKEN_PATH")
        if not directory:
            if info.st_nlink != 1:
                raise TokenFileError("UNSAFE_TOKEN_PATH")
            if os.name == "posix" and (
                info.st_uid != getattr(os, "geteuid", lambda: -1)()
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise TokenFileError("UNSAFE_TOKEN_PERMISSIONS")

    def _parent(self) -> int | None:
        """Открытый дескриптор удерживает проверенный POSIX каталог при смене пути."""
        if os.name != "posix":
            for ancestor in reversed((self._path.parent, *self._path.parent.parents)):
                self._check(ancestor.lstat(), directory=True)
            return None
        directory_flag = getattr(os, "O_DIRECTORY", 0)
        nofollow_flag = getattr(os, "O_NOFOLLOW", 0)
        if not directory_flag or not nofollow_flag:
            raise TokenFileError("UNSAFE_TOKEN_PATH")
        flags = os.O_RDONLY | directory_flag | nofollow_flag
        descriptor = os.open(self._path.anchor, flags)
        try:
            for part in self._path.parent.parts[1:]:
                child = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            info = os.fstat(descriptor)
            if (
                info.st_uid not in (0, getattr(os, "geteuid", lambda: -1)())
                or stat.S_IMODE(info.st_mode) & 0o022
            ):
                raise TokenFileError("UNSAFE_TOKEN_PERMISSIONS")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _target(self, parent: int | None) -> str | Path:
        return self._path if parent is None else self._path.name

    def _check_opened(self, descriptor: int, parent: int | None) -> os.stat_result:
        opened = os.fstat(descriptor)
        self._check(opened, directory=False)
        linked = os.stat(self._target(parent), dir_fd=parent, follow_symlinks=False)
        self._check(linked, directory=False)
        if (opened.st_dev, opened.st_ino) != (linked.st_dev, linked.st_ino):
            raise TokenFileError("UNSAFE_TOKEN_PATH")
        return opened

    def read(self) -> str:
        """Читаем не более 129 байтов; единственный завершающий LF допускается."""
        parent = None
        descriptor = None
        try:
            parent = self._parent()
            self._check(
                os.stat(self._target(parent), dir_fd=parent, follow_symlinks=False), directory=False
            )
            descriptor = os.open(
                self._target(parent),
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0)
                | getattr(os, "O_BINARY", 0),
                dir_fd=parent,
            )
            self._check_opened(descriptor, parent)
            raw = os.read(descriptor, 130)
            self._check_opened(descriptor, parent)
            if len(raw) > 129:
                raise TokenFileError("INVALID_AGENT_TOKEN")
            token = raw.decode("ascii")
            if token.endswith("\n"):
                token = token[:-1]
            return validate_agent_token(token)
        except (OSError, ValueError, UnicodeError):
            raise TokenFileError("TOKEN_FILE_READ_FAILED") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if parent is not None:
                os.close(parent)

    def reserve(self) -> TokenReservation:
        """Существующий файл отвергается до расходования одноразового enrollment token."""
        return TokenReservation(self)


class TokenReservation:
    """Файл создаётся эксклюзивно до регистрации; только подтверждённая запись остаётся."""

    def __init__(self, token_file: TokenFile) -> None:
        self._file = token_file
        self._parent: int | None = None
        self._descriptor: int | None = None
        self._identity: tuple[int, int] | None = None
        self._written = False

    def __enter__(self) -> TokenReservation:
        if self._descriptor is not None or self._written:
            raise TokenFileError("TOKEN_FILE_RESERVATION_USED")
        try:
            self._parent = self._file._parent()
            self._descriptor = os.open(
                self._file._target(self._parent),
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_BINARY", 0),
                0o600,
                dir_fd=self._parent,
            )
            info = os.fstat(self._descriptor)
            self._identity = (info.st_dev, info.st_ino)
            if os.name == "posix":
                fchmod = getattr(os, "fchmod", None)
                if fchmod is None:
                    raise TokenFileError("UNSAFE_TOKEN_PERMISSIONS")
                fchmod(self._descriptor, 0o600)
            info = self._file._check_opened(self._descriptor, self._parent)
            self._identity = (info.st_dev, info.st_ino)
            os.fsync(self._descriptor)
            if self._parent is not None:
                os.fsync(self._parent)
            return self
        except FileExistsError:
            self.__exit__(None, None, None)
            raise TokenFileError("TOKEN_FILE_EXISTS") from None
        except (OSError, ValueError, TokenFileError):
            self.__exit__(None, None, None)
            raise TokenFileError("TOKEN_FILE_CREATE_FAILED") from None

    def write(self, token: str) -> None:
        """Запись завершается fsync файла и каталога; повторная запись запрещена."""
        if self._descriptor is None or self._written:
            raise TokenFileError("TOKEN_FILE_RESERVATION_USED")
        raw = (validate_agent_token(token) + "\n").encode("ascii")
        try:
            self._file._check_opened(self._descriptor, self._parent)
            while raw:
                count = os.write(self._descriptor, raw)
                if count <= 0:
                    raise OSError
                raw = raw[count:]
            os.fsync(self._descriptor)
            self._file._check_opened(self._descriptor, self._parent)
            if self._parent is not None:
                os.fsync(self._parent)
            self._written = True
        except (OSError, ValueError):
            raise TokenFileError("TOKEN_FILE_WRITE_FAILED") from None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            # Windows не удаляет открытый файл: сначала освобождаем дескриптор.
            if self._descriptor is not None:
                os.close(self._descriptor)
                self._descriptor = None
            if not self._written and self._identity is not None:
                try:
                    linked = os.stat(
                        self._file._target(self._parent),
                        dir_fd=self._parent,
                        follow_symlinks=False,
                    )
                    if (linked.st_dev, linked.st_ino) == self._identity:
                        os.unlink(self._file._target(self._parent), dir_fd=self._parent)
                        if self._parent is not None:
                            os.fsync(self._parent)
                except OSError:
                    pass
        finally:
            if self._descriptor is not None:
                os.close(self._descriptor)
                self._descriptor = None
            if self._parent is not None:
                os.close(self._parent)
                self._parent = None
