"""Фактические раздельные пользователи R, C и L для публикации (§8.2). Linux и root.

Прежние Linux-проверки публикации шли под одним пользователем, поэтому биты режима
не ограничивали сам проверяющий процесс: §9.4 честно отмечал, что раздельные
R, C и L не проверены. Здесь роли настоящие, а решения принимает ядро.

Root нужен только родителю: он готовит владельцев каталогов и необратимо меняет
идентичность дочерних процессов. Это не проверка контейнерного runtime, bind mount
или SELinux. Проверяются фактические операции (open, unlink, rename, create),
а не `os.access` и не биты из `stat`.
"""

import os
import shutil
import stat
import sys
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

import pytest

from agent.runtime.boundary.publication import (
    MODE_DIRECTORY,
    MODE_PUBLISHED_FILE,
    LinuxWriteOnceRename,
    ProtectedTree,
)

# Синтетические идентичности из диапазона nobody: системных записей им не нужно.
RUNTIME_UID = 65401
RUNTIME_GID = 65401
COLLECTOR_UID = 65402
COLLECTOR_GID = 65403
TRAP_UID = 65404
TRAP_GID = 65404
PARTS = ("low-boundaries", "agent")
DOCUMENT = b'{"schema":"honeyforge-low-writer-anchor/1"}'
_REQUIRED = os.environ.get("HF_REQUIRE_LINUX_USERS") == "1"


def _unavailable() -> str | None:
    """Причина пропуска: роль ядра здесь подменить нечем."""
    if sys.platform != "linux":
        return "раздельные пользователи проверяются только на Linux"
    if os.geteuid() != 0:
        return "подготовка владельцев требует root у родительского процесса"
    return None


if _REQUIRED and _unavailable() is not None:
    raise AssertionError(
        f"Проверке раздельных пользователей необходим Linux и root: {_unavailable()}"
    )

pytestmark = pytest.mark.skipif(_unavailable() is not None, reason=_unavailable() or "")


@pytest.fixture
def base() -> Path:
    """База вне /tmp: родитель корня не может быть доступен на запись остальным (§8.2)."""
    root = Path("/opt") / f"hf-boundary-users-{uuid4()}"
    root.mkdir(mode=0o755)
    os.chown(root, 0, 0)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def as_user(uid: int, gid: int, groups: list[int], action: Callable[[], str]) -> str:
    """Выполняет проверку в дочернем процессе под заданной идентичностью ядра.

    Смена идентичности необратима и выполняется до действия, поэтому решение
    принимает ядро, а не проверка битов в тесте. Результат передаётся текстом:
    так ошибка внутри роли попадает в отчёт, а не теряется кодом выхода.
    """
    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:  # pragma: no cover - исполняется в дочернем процессе
        code = 0
        try:
            os.close(read_fd)
            os.setgroups(groups)
            os.setgid(gid)
            os.setuid(uid)
            assert os.getuid() == os.geteuid() == uid
            message = action()
        except BaseException as error:  # noqa: BLE001 - причина уходит родителю текстом
            message, code = f"{type(error).__name__}: {error}", 1
        try:
            os.write(write_fd, message.encode("utf-8", "replace")[:4000])
        finally:
            os._exit(code)
    os.close(write_fd)
    collected = b""
    while True:
        chunk = os.read(read_fd, 4096)
        if not chunk:
            break
        collected += chunk
    os.close(read_fd)
    _, status = os.waitpid(child, 0)
    message = collected.decode("utf-8", "replace")
    if os.waitstatus_to_exitcode(status) != 0:
        raise AssertionError(f"роль uid={uid} завершилась с ошибкой: {message}")
    return message


def tree(base: Path) -> ProtectedTree:
    """Дерево публикации с группой чтения сборщика и настоящим renameat2."""
    return ProtectedTree(base / "control", COLLECTOR_GID, LinuxWriteOnceRename())


def publish(base: Path) -> str:
    """Действие роли R: создаёт каталоги привязки и публикует документ."""
    protected = tree(base)
    directory = protected.open_directory(PARTS, create=True)
    try:
        protected.write_once(directory, "anchor.json", DOCUMENT)
    finally:
        os.close(directory)
    return "опубликовано"


def prepare(base: Path) -> Path:
    """Готовит корень за R и публикует документ его руками."""
    control = base / "control"
    control.mkdir(mode=MODE_DIRECTORY)
    os.chown(control, RUNTIME_UID, COLLECTOR_GID)
    assert as_user(RUNTIME_UID, RUNTIME_GID, [COLLECTOR_GID], lambda: publish(base)) == (
        "опубликовано"
    )
    return control / PARTS[0] / PARTS[1] / "anchor.json"


def test_runtime_publishes_and_collector_only_reads(base: Path) -> None:
    """R публикует, C читает и не может изменить, L не получает доступа (§8.2)."""
    published = prepare(base)
    info = published.lstat()
    assert stat.S_IMODE(info.st_mode) == MODE_PUBLISHED_FILE
    assert (info.st_uid, info.st_gid) == (RUNTIME_UID, COLLECTOR_GID)
    assert info.st_nlink == 1

    def collector() -> str:
        """Роль C: чтение разрешено, любое изменение запрещено ядром."""
        results = [f"прочитано:{len(published.read_bytes())}"]
        for mode in ("ab", "wb"):
            try:
                with published.open(mode):
                    results.append(f"ОШИБКА: открыл на запись {mode}")
            except PermissionError:
                results.append(f"запись {mode} запрещена")
        for action, name in (
            (lambda: os.unlink(published), "unlink"),
            (lambda: os.rename(published, published.parent / "stolen.json"), "rename"),
            (lambda: (published.parent / "own.json").write_bytes(b"{}"), "create"),
            (lambda: os.chmod(published, 0o640), "chmod"),
        ):
            try:
                action()
                results.append(f"ОШИБКА: выполнил {name}")
            except PermissionError:
                results.append(f"{name} запрещён")
        return " | ".join(results)

    report = as_user(COLLECTOR_UID, COLLECTOR_GID, [COLLECTOR_GID], collector)
    assert "ОШИБКА" not in report, report
    assert f"прочитано:{len(DOCUMENT)}" in report
    for name in ("запись ab запрещена", "unlink запрещён", "rename запрещён", "create запрещён"):
        assert name in report, report

    def trap() -> str:
        """Роль L: пользователь контейнеров ловушек не имеет пути к корням."""
        results = []
        for action, name in (
            (lambda: published.read_bytes(), "чтение документа"),
            (lambda: os.listdir(published.parent), "перечисление каталога"),
            (lambda: (published.parent / "planted.json").write_bytes(b"{}"), "подлог файла"),
        ):
            try:
                action()
                results.append(f"ОШИБКА: выполнил {name}")
            except PermissionError:
                results.append(f"{name} запрещено")
        return " | ".join(results)

    assert "ОШИБКА" not in as_user(TRAP_UID, TRAP_GID, [], trap)


def test_collector_cannot_replace_the_published_document(base: Path) -> None:
    """Сборщик не может подменить документ даже созданием своего файла рядом (§8.2)."""
    published = prepare(base)
    original = published.read_bytes()

    def collector() -> str:
        """Роль C: подготовить подмену и переименовать её поверх документа."""
        candidate = published.parent / "replacement.json"
        try:
            candidate.write_bytes(b'{"schema":"spoof"}')
        except PermissionError:
            return "подготовка подмены запрещена"
        return "ОШИБКА: создал файл в каталоге публикации"

    assert as_user(COLLECTOR_UID, COLLECTOR_GID, [COLLECTOR_GID], collector) == (
        "подготовка подмены запрещена"
    )
    assert published.read_bytes() == original
    assert sorted(item.name for item in published.parent.iterdir()) == ["anchor.json"]


def test_runtime_cannot_rewrite_its_own_published_document(base: Path) -> None:
    """Даже R не открывает опубликованный документ на запись: имя write-once (§8.3)."""
    published = prepare(base)

    def runtime() -> str:
        """Роль R: владелец без бита записи получает отказ ядра."""
        try:
            with published.open("ab"):
                return "ОШИБКА: владелец открыл документ на запись"
        except PermissionError:
            return "запись владельцем запрещена"

    assert as_user(RUNTIME_UID, RUNTIME_GID, [COLLECTOR_GID], runtime) == (
        "запись владельцем запрещена"
    )
    # Повторная публикация тех же байтов остаётся успешной и идемпотентной.
    assert as_user(RUNTIME_UID, RUNTIME_GID, [COLLECTOR_GID], lambda: publish(base)) == (
        "опубликовано"
    )
