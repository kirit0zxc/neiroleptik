"""Локальная публикация write-once для anchor, proof и снимков (§8.2 предложения).

Канал MVP — только файловая публикация на runtime-хосте: anchor и proof не передаются
через Event, command ACK, telemetry batch или API центра. Гарантии определены для Linux;
на другой ОС и на файловой системе без RENAME_NOREPLACE producer выключен и не выдаёт
документов, поэтому collector остаётся в прежнем ожидании.

Имя публикуется атомарно и **без замены**: повтор с теми же байтами успешен, другие
байты под тем же именем — конфликт без перезаписи. Обходные схемы через обычный rename
или link здесь намеренно отсутствуют: они допускали бы подмену уже выданного документа.
"""

from __future__ import annotations

import copy
import ctypes
import ctypes.util
import os
import platform
import stat
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from agent.runtime.boundary.documents import DOCUMENT_MAX_BYTES
from agent.runtime.boundary.errors import BoundaryError, BoundaryErrorCode

# Флаг renameat2: целевое имя не заменяется, при его существовании вызов даёт EEXIST.
RENAME_NOREPLACE = 1
# Номера системного вызова renameat2 по архитектурам Linux; применяются только тогда,
# когда glibc не экспортирует саму функцию (до 2.28).
_SYSCALL_NUMBERS = {
    "x86_64": 316,
    "i386": 353,
    "i686": 353,
    "aarch64": 276,
    "armv7l": 382,
    "armv8l": 382,
    "riscv64": 276,
    "ppc64le": 357,
    "s390x": 347,
    "loongarch64": 276,
}
# Режимы из таблицы §8.2.
MODE_DIRECTORY = 0o750
MODE_PUBLISHED_DIRECTORY = 0o550
MODE_PUBLISHED_FILE = 0o440
MODE_STAGING_DIRECTORY = 0o700
MODE_STAGING_FILE = 0o600
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


def _disabled(detail: str) -> BoundaryError:
    """Producer выключен: документы не выдаются, прежнее ожидание collector сохраняется."""
    return BoundaryError(BoundaryErrorCode.PRODUCER_DISABLED, detail)


class LinuxWriteOnceRename:
    """Доступ к renameat2(RENAME_NOREPLACE); без него producer выключен (§8.2).

    Предпочитается символ glibc; при его отсутствии применяется номер системного вызова
    известной архитектуры. Если ни то, ни другое недоступно, класс сообщает об этом,
    и обходные схемы не используются.

    Обе ветви разрешаются при создании, даже когда символ есть: иначе ветвь номера
    системного вызова оставалась бы недостижимой на обычной glibc и непроверенной
    до первой машины со старой библиотекой.
    """

    def __init__(self) -> None:
        self._renameat2: object | None = None
        self._syscall: object | None = None
        self._number = _SYSCALL_NUMBERS.get(platform.machine())
        self.reason = ""
        if sys.platform != "linux":
            self.reason = "публикация поддерживается только на Linux"
            return
        try:
            library = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
        except OSError:
            self.reason = "libc недоступна для renameat2"
            return
        if hasattr(library, "renameat2"):
            function = library.renameat2
            function.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            function.restype = ctypes.c_int
            self._renameat2 = function
        if self._number is not None and hasattr(library, "syscall"):
            function = library.syscall
            function.restype = ctypes.c_long
            self._syscall = function
        if not self.available:
            self.reason = "renameat2 недоступен в этой системе"

    def without_library_symbol(self) -> LinuxWriteOnceRename:
        """Тот же доступ, но только через номер системного вызова.

        Нужен проверке ветви, которая в production выбирается лишь на glibc старше
        2.28: без принудительного выбора её маршалинг остался бы непроверенным.
        Производственные пути этот метод не используют.
        """
        forced = copy.copy(self)
        forced._renameat2 = None
        if forced._syscall is None or forced._number is None:
            forced.reason = "номер системного вызова renameat2 для этой архитектуры неизвестен"
        return forced

    @property
    def available(self) -> bool:
        """Доступность проверяется до любых изменений корней публикации."""
        return self._renameat2 is not None or self._syscall is not None

    def rename(self, directory_fd: int, old: str, new: str) -> None:
        """Атомарно переименовывает внутри одного каталога без замены цели.

        Существующее целевое имя приводит к FileExistsError; вызывающая сторона решает,
        повтор это или конфликт.
        """
        if not self.available:
            raise _disabled(self.reason or "renameat2 недоступен")
        source = old.encode("utf-8")
        target = new.encode("utf-8")
        ctypes.set_errno(0)
        if self._renameat2 is not None:
            function = self._renameat2
            assert callable(function)
            result = int(function(directory_fd, source, directory_fd, target, RENAME_NOREPLACE))
        else:
            function = self._syscall
            assert callable(function) and self._number is not None
            result = int(
                function(
                    self._number,
                    ctypes.c_long(directory_fd),
                    ctypes.c_char_p(source),
                    ctypes.c_long(directory_fd),
                    ctypes.c_char_p(target),
                    ctypes.c_long(RENAME_NOREPLACE),
                )
            )
        if result != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), new)


@dataclass(frozen=True)
class PublicationRoots:
    """Абсолютные корни из настроек оператора; профиль и документы их не задают (§8.2)."""

    control_root: Path
    snapshot_root: Path
    # Группа G чтения collector; None — группа не меняется (локальная отладка на одном UID).
    group_gid: int | None = None
    # Отдельная квота оператора на snapshot_root с учётом подготовки (§8.3 «Место»).
    # None — ограничением считается только фактическое свободное место файловой системы.
    snapshot_quota_bytes: int | None = None

    def __post_init__(self) -> None:
        for name in ("control_root", "snapshot_root"):
            value = Path(getattr(self, name))
            if not value.is_absolute() or ".." in value.parts:
                raise _disabled(f"{name} должен быть абсолютным путём без «..»")
            object.__setattr__(self, name, value)
        if self.group_gid is not None and (
            type(self.group_gid) is not int or not 0 <= self.group_gid <= 0xFFFFFFFF
        ):
            raise _disabled("group_gid должен быть неотрицательным целым")
        if self.snapshot_quota_bytes is not None and (
            type(self.snapshot_quota_bytes) is not int or self.snapshot_quota_bytes < 1
        ):
            raise _disabled("snapshot_quota_bytes должен быть положительным целым")

    def check_outside(self, journal_root: Path) -> None:
        """Корни не вложены в journal_root и не содержат его (§8.2)."""
        journal = Path(journal_root)
        for root in (self.control_root, self.snapshot_root):
            if self.overlaps(root, journal):
                raise _disabled("корни публикации пересекаются с journal_root")

    def overlaps_any(self, source: Path) -> bool:
        """Источник bind mount пересекается с корнями: такой контейнер не запускается."""
        return any(
            self.overlaps(root, Path(source)) for root in (self.control_root, self.snapshot_root)
        )

    @staticmethod
    def overlaps(first: Path, second: Path) -> bool:
        """Пересечением считается совпадение или вложенность в любую сторону.

        Пути приводятся к фактическим: иначе символическая ссылка из каталога журнала
        внутрь корней прошла бы лексическую сверку и ловушка получила бы путь к своим
        же доказательствам. Несуществующие части пути просто нормализуются.
        """
        resolved_first = Path(os.path.realpath(first))
        resolved_second = Path(os.path.realpath(second))
        return (
            resolved_first == resolved_second
            or resolved_first in resolved_second.parents
            or resolved_second in resolved_first.parents
        )


class ProtectedTree:
    """Защищённое дерево публикации: единственный писатель — runtime producer.

    Класс отвечает за безопасные пути и права, но не за содержание документов.
    От компрометации пользователя R или root он не защищает: они входят в доверенную
    базу, поэтому потребитель перед первым использованием проверяет размер и SHA-256.
    """

    def __init__(self, root: Path, group_gid: int | None, rename: LinuxWriteOnceRename) -> None:
        self._root = Path(root)
        self._group_gid = group_gid
        self._rename = rename

    @property
    def root(self) -> Path:
        """Корень дерева из настроек оператора; нужен для диагностических путей."""
        return self._root

    def require_available(self) -> None:
        """Проверяет ОС, renameat2 и безопасность предков корня до любых изменений."""
        if sys.platform != "linux":
            raise _disabled("публикация поддерживается только на Linux")
        if not self._rename.available:
            raise _disabled(self._rename.reason or "renameat2 недоступен")
        self._check_ancestors()

    def _check_ancestors(self) -> None:
        """Родители корня до «/» принадлежат root или R и не доступны на запись другим."""
        geteuid = getattr(os, "geteuid", None)
        owner = 0 if geteuid is None else geteuid()
        for directory in self._root.parents:
            try:
                info = directory.lstat()
            except OSError as error:
                raise _disabled("родитель корня публикации недоступен") from error
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise _disabled("родитель корня публикации не каталог")
            if info.st_uid not in (0, owner):
                raise _disabled("родитель корня публикации принадлежит другому владельцу")
            if stat.S_IMODE(info.st_mode) & (stat.S_IWGRP | stat.S_IWOTH):
                raise _disabled("родитель корня публикации доступен на запись другим")

    def open_directory(self, parts: Sequence[str], *, create: bool) -> int:
        """Открывает каталог цепочкой openat от корня, не переходя по ссылкам.

        Отсутствующие промежуточные каталоги создаются с режимом 0750 и группой G,
        когда это разрешено вызывающей стороной. Возвращённый дескриптор закрывает
        вызывающая сторона.
        """
        self.require_available()
        flags = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC
        try:
            current = os.open(self._root, flags)
        except FileNotFoundError:
            if not create:
                raise _disabled("корень публикации отсутствует") from None
            self._create_root()
            current = os.open(self._root, flags)
        except OSError as error:
            raise _disabled("корень публикации недоступен") from error
        try:
            self._check_directory(current, published=False)
            for name in parts:
                self._check_name(name)
                try:
                    nested = os.open(name, flags, dir_fd=current)
                except FileNotFoundError:
                    if not create:
                        raise _disabled("каталог публикации отсутствует") from None
                    os.mkdir(name, MODE_DIRECTORY, dir_fd=current)
                    nested = os.open(name, flags, dir_fd=current)
                    self._apply_group(nested, MODE_DIRECTORY)
                    os.fsync(current)
                except OSError as error:
                    raise _disabled("каталог публикации недоступен") from error
                os.close(current)
                current = nested
                self._check_directory(current, published=False)
        except BaseException:
            os.close(current)
            raise
        return current

    def _create_root(self) -> None:
        """Создаёт корень публикации вместе с предками по настройке оператора."""
        try:
            self._root.mkdir(mode=MODE_DIRECTORY, parents=True, exist_ok=True)
        except OSError as error:
            raise _disabled("корень публикации нельзя создать") from error
        descriptor = os.open(self._root, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW)
        try:
            self._apply_group(descriptor, MODE_DIRECTORY)
        finally:
            os.close(descriptor)

    def write_once(self, directory_fd: int, name: str, data: bytes) -> bool:
        """Публикует байты под окончательным именем; True — имя создано этим вызовом.

        Повтор с теми же байтами считается успешным (False), другие байты под тем же
        именем дают BOUNDARY_PUBLICATION_CONFLICT без перезаписи. Файловая система,
        которая не поддерживает RENAME_NOREPLACE, выключает producer на этом вызове.
        """
        self._check_published_name(name)
        if not isinstance(data, (bytes, bytearray)) or not 0 < len(data) <= DOCUMENT_MAX_BYTES:
            raise BoundaryError(BoundaryErrorCode.LIMIT_EXCEEDED, "размер документа недопустим")
        existing = self.read_published(directory_fd, name)
        if existing is not None:
            if existing == bytes(data):
                return False
            raise BoundaryError(
                BoundaryErrorCode.BOUNDARY_PUBLICATION_CONFLICT,
                "имя уже опубликовано с другими байтами",
            )
        temporary = f".tmp-{uuid4()}"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC,
            MODE_STAGING_FILE,
            dir_fd=directory_fd,
        )
        try:
            self._write_all(descriptor, bytes(data))
            os.fsync(descriptor)
            self._apply_group(descriptor, MODE_PUBLISHED_FILE)
        except BaseException:
            os.close(descriptor)
            self._discard(directory_fd, temporary)
            raise
        os.close(descriptor)
        try:
            self._rename.rename(directory_fd, temporary, name)
        except FileExistsError:
            self._discard(directory_fd, temporary)
            published = self.read_published(directory_fd, name)
            if published == bytes(data):
                return False
            raise BoundaryError(
                BoundaryErrorCode.BOUNDARY_PUBLICATION_CONFLICT,
                "имя уже опубликовано с другими байтами",
            ) from None
        except OSError as error:
            self._discard(directory_fd, temporary)
            raise _disabled("публикация без замены не выполнена") from error
        os.fsync(directory_fd)
        return True

    def directory_exists(self, directory_fd: int, name: str) -> bool:
        """Сообщает, опубликован ли уже каталог с этим именем, не меняя его."""
        self._check_published_name(name)
        try:
            nested = os.open(name, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW, dir_fd=directory_fd)
        except FileNotFoundError:
            return False
        except NotADirectoryError:
            return True
        except OSError as error:
            raise _disabled("опубликованный каталог недоступен") from error
        os.close(nested)
        return True

    def publish_directory(self, directory_fd: int, staging: str, name: str) -> bool:
        """Переводит готовый `.staging-*` в окончательное имя атомарно и без замены."""
        self._check_published_name(name)
        nested = os.open(staging, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW, dir_fd=directory_fd)
        try:
            self._apply_group(nested, MODE_PUBLISHED_DIRECTORY)
            os.fsync(nested)
        finally:
            os.close(nested)
        try:
            self._rename.rename(directory_fd, staging, name)
        except FileExistsError:
            return False
        except OSError as error:
            raise _disabled("публикация каталога без замены не выполнена") from error
        os.fsync(directory_fd)
        return True

    def read_published(self, directory_fd: int, name: str) -> bytes | None:
        """Читает опубликованный файл целиком из одного дескриптора; None — файла нет.

        Проверяются обычный файл, владелец R, отсутствие битов записи, ровно одна
        жёсткая ссылка и предел размера: иначе файл не считается опубликованным.
        """
        self._check_name(name)
        try:
            descriptor = os.open(name, os.O_RDONLY | _O_NOFOLLOW | _O_CLOEXEC, dir_fd=directory_fd)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise _disabled("опубликованный файл недоступен") from error
        try:
            info = os.fstat(descriptor)
            geteuid = getattr(os, "geteuid", None)
            owner = info.st_uid if geteuid is None else geteuid()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != owner:
                raise _disabled("опубликованный файл не прошёл проверку владельца")
            if sys.platform == "linux" and stat.S_IMODE(info.st_mode) & 0o222:
                raise _disabled("опубликованный файл доступен на запись")
            if info.st_size > DOCUMENT_MAX_BYTES:
                raise BoundaryError(
                    BoundaryErrorCode.LIMIT_EXCEEDED, "опубликованный файл больше предела"
                )
            data = b""
            while True:
                chunk = os.read(descriptor, DOCUMENT_MAX_BYTES + 1 - len(data))
                if not chunk:
                    break
                data += chunk
            return data
        finally:
            os.close(descriptor)

    def create_staging(self, directory_fd: int, name: str) -> int:
        """Создаёт приватный каталог подготовки 0700; существующий удаляется заново."""
        self._check_name(name)
        try:
            os.mkdir(name, MODE_STAGING_DIRECTORY, dir_fd=directory_fd)
        except FileExistsError:
            self.remove_staging(directory_fd, name)
            os.mkdir(name, MODE_STAGING_DIRECTORY, dir_fd=directory_fd)
        except OSError as error:
            raise _disabled("каталог подготовки нельзя создать") from error
        return os.open(name, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW, dir_fd=directory_fd)

    def remove_staging(self, directory_fd: int, name: str) -> None:
        """Удаляет незавершённую подготовку: исходники неизменны, нового RW не было (§8.4).

        Режим каталога возвращается к приватному 0700 перед удалением содержимого:
        подготовка получает 0550 непосредственно перед переименованием, и без этого
        шага уборка после отказа переименования падала бы по правам, подменяя
        исходную причину отказа и оставляя неудаляемый каталог.
        """
        self._check_name(name)
        if not name.startswith((".staging-", ".tmp-")):
            raise _disabled("удаление допустимо только для каталога подготовки")
        try:
            nested = os.open(name, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW, dir_fd=directory_fd)
        except FileNotFoundError:
            return
        except OSError as error:
            raise _disabled("каталог подготовки недоступен") from error
        try:
            fchmod = getattr(os, "fchmod", None)
            if fchmod is not None:
                try:
                    fchmod(nested, MODE_STAGING_DIRECTORY)
                except OSError as error:
                    raise _disabled("каталог подготовки нельзя вернуть в приватный режим") from (
                        error
                    )
            for entry in os.listdir(nested):
                os.unlink(entry, dir_fd=nested)
        finally:
            os.close(nested)
        os.rmdir(name, dir_fd=directory_fd)
        os.fsync(directory_fd)

    def list_published(self, directory_fd: int) -> tuple[str, ...]:
        """Имена опубликованных объектов каталога; имена с точкой не считаются.

        Нужно, чтобы обнаружить уже выданные документы привязки при утраченной
        registry: историю по каталогу восстанавливать запрещено (§8.4), но её наличие
        обязано остановить выдачу новых документов.
        """
        try:
            names = os.listdir(directory_fd)
        except OSError as error:
            raise _disabled("каталог публикации недоступен для перечисления") from error
        return tuple(sorted(name for name in names if not name.startswith(".")))

    def staging_file(self, staging_fd: int, name: str) -> int:
        """Создаёт файл копии 0600 в каталоге подготовки без перехода по ссылке."""
        self._check_name(name)
        return os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC,
            MODE_STAGING_FILE,
            dir_fd=staging_fd,
        )

    def finalize_file(self, descriptor: int) -> None:
        """Фиксирует копию: fsync, группа G и режим только для чтения (§8.3)."""
        os.fsync(descriptor)
        self._apply_group(descriptor, MODE_PUBLISHED_FILE)

    def _apply_group(self, descriptor: int, mode: int) -> None:
        """Назначает группу чтения collector и окончательный режим объекта."""
        fchown = getattr(os, "fchown", None)
        if self._group_gid is not None and fchown is not None:
            try:
                fchown(descriptor, -1, self._group_gid)
            except OSError as error:
                raise _disabled("группа чтения collector не назначена") from error
        fchmod = getattr(os, "fchmod", None)
        if fchmod is not None:
            try:
                fchmod(descriptor, mode)
            except OSError as error:
                raise _disabled("режим объекта публикации не назначен") from error

    def _check_directory(self, descriptor: int, *, published: bool) -> None:
        """Каталог не ссылка, принадлежит R и не доступен на запись группе и остальным."""
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode):
            raise _disabled("объект публикации не каталог")
        geteuid = getattr(os, "geteuid", None)
        owner = info.st_uid if geteuid is None else geteuid()
        if info.st_uid != owner:
            raise _disabled("каталог публикации принадлежит другому владельцу")
        if sys.platform == "linux":
            mode = stat.S_IMODE(info.st_mode)
            if mode & (stat.S_IWGRP | stat.S_IWOTH):
                raise _disabled("каталог публикации доступен на запись другим")
            if published and mode & stat.S_IWUSR:
                raise _disabled("опубликованный каталог доступен на запись")

    @classmethod
    def _check_published_name(cls, name: str) -> None:
        """Окончательное имя не начинается с точки: такие имена collector не читает (§8.2)."""
        cls._check_name(name)
        if name.startswith("."):
            raise _disabled("опубликованное имя не может начинаться с точки")

    @staticmethod
    def _check_name(name: str) -> None:
        """Одно имя внутри каталога: без разделителей, «..» и управляющих символов."""
        if (
            not isinstance(name, str)
            or not 1 <= len(name) <= 255
            or name in (".", "..")
            or "/" in name
            or "\\" in name
            or "\x00" in name
            or any(ord(character) < 32 for character in name)
        ):
            raise _disabled("недопустимое имя в каталоге публикации")

    @staticmethod
    def _write_all(descriptor: int, data: bytes) -> None:
        """Небуферизованная запись может принять часть данных: дописывает остаток."""
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if not written:
                raise _disabled("запись документа не продвигается")
            view = view[written:]

    @staticmethod
    def _discard(directory_fd: int, name: str) -> None:
        """Снимает незавершённый временный файл; его отсутствие не ошибка."""
        try:
            os.unlink(name, dir_fd=directory_fd)
        except OSError:
            return
