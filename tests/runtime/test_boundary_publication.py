"""Публикация write-once и права корней границы процесса (§8.2). Только Linux.

Гарантии §8.2 определены для Linux: атомарное имя без замены через
renameat2(RENAME_NOREPLACE), владелец R, группа чтения G и отсутствие битов записи
у опубликованного файла. На другой ОС producer выключен, поэтому тест пропускается;
шаг CI с HF_REQUIRE_LINUX_BOUNDARY=1 требует его фактического выполнения.

Корни создаются не в /tmp: этот каталог доступен на запись остальным, и по §8.2
такой родитель корня сам по себе запрещён. База берётся из HF_BOUNDARY_TEST_ROOT
или из домашнего каталога пользователя.
"""

import os
import stat
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from boundary_support import collector_gid, publication_base, require_linux

from agent.runtime.boundary.errors import BoundaryError, BoundaryErrorCode
from agent.runtime.boundary.publication import (
    MODE_DIRECTORY,
    MODE_PUBLISHED_DIRECTORY,
    MODE_PUBLISHED_FILE,
    MODE_STAGING_DIRECTORY,
    LinuxWriteOnceRename,
    ProtectedTree,
    PublicationRoots,
)

COLLECTOR_GID = collector_gid()

require_linux("публикации")

pytestmark = pytest.mark.skipif(
    sys.platform != "linux",
    reason="renameat2 и права POSIX проверяются выделенным Linux-шагом",
)


@pytest.fixture
def base() -> Iterator[Path]:
    """Безопасная база для корней: владелец текущий, без записи группе и остальным."""
    yield from publication_base("publication")


def tree(base: Path, *, group: int | None = COLLECTOR_GID) -> ProtectedTree:
    """Защищённое дерево публикации с настоящим renameat2."""
    return ProtectedTree(base / "control", group, LinuxWriteOnceRename())


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def test_renameat2_without_replacement_is_available() -> None:
    """Без RENAME_NOREPLACE producer выключен: доступность проверяется явно."""
    rename = LinuxWriteOnceRename()
    assert rename.available, rename.reason


def test_document_is_published_once_with_read_only_access(base: Path) -> None:
    """Опубликованный документ: владелец R, группа G, режим 0440, одна жёсткая ссылка."""
    protected = tree(base)
    directory = protected.open_directory(("low-boundaries", "agent"), create=True)
    try:
        assert protected.write_once(directory, "anchor.json", b'{"schema":"x"}') is True
    finally:
        os.close(directory)
    published = base / "control" / "low-boundaries" / "agent" / "anchor.json"
    info = published.lstat()
    assert published.read_bytes() == b'{"schema":"x"}'
    assert mode_of(published) == MODE_PUBLISHED_FILE
    assert info.st_nlink == 1
    assert info.st_uid == os.geteuid() and info.st_gid == COLLECTOR_GID
    assert mode_of(base / "control") == MODE_DIRECTORY
    assert mode_of(base / "control" / "low-boundaries" / "agent") == MODE_DIRECTORY
    # Временные имена не остаются в каталоге публикации.
    assert [item.name for item in (base / "control" / "low-boundaries" / "agent").iterdir()] == [
        "anchor.json"
    ]


def test_repeated_publication_of_the_same_bytes_is_idempotent(base: Path) -> None:
    """Повтор с теми же байтами успешен и не создаёт нового файла (§8.4)."""
    protected = tree(base)
    parts = ("low-boundaries", "agent")
    data = b'{"schema":"honeyforge-low-writer-anchor/1"}'
    directory = protected.open_directory(parts, create=True)
    try:
        assert protected.write_once(directory, "anchor.json", data) is True
        published = base / "control" / "low-boundaries" / "agent" / "anchor.json"
        before = published.lstat()
        assert protected.write_once(directory, "anchor.json", data) is False
        after = published.lstat()
        assert (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns)
    finally:
        os.close(directory)


def test_other_bytes_under_the_same_name_are_a_conflict(base: Path) -> None:
    """Другие байты под тем же именем — конфликт без перезаписи (§8.2)."""
    protected = tree(base)
    directory = protected.open_directory(("low-boundaries",), create=True)
    try:
        protected.write_once(directory, "1-first.json", b'{"a":1}')
        with pytest.raises(BoundaryError) as error:
            protected.write_once(directory, "1-first.json", b'{"a":2}')
        assert error.value.code is BoundaryErrorCode.BOUNDARY_PUBLICATION_CONFLICT
        assert protected.read_published(directory, "1-first.json") == b'{"a":1}'
        # Незавершённая попытка не оставляет временных файлов.
        assert sorted(os.listdir(directory)) == ["1-first.json"]
    finally:
        os.close(directory)


def test_published_file_with_write_bits_or_extra_link_is_refused(base: Path) -> None:
    """Проверка потребителя: обычный файл, без записи, ровно одна жёсткая ссылка."""
    protected = tree(base)
    directory = protected.open_directory(("low-boundaries",), create=True)
    try:
        protected.write_once(directory, "1-first.json", b'{"a":1}')
        published = base / "control" / "low-boundaries" / "1-first.json"
        published.chmod(0o640)
        with pytest.raises(BoundaryError) as error:
            protected.read_published(directory, "1-first.json")
        assert error.value.code is BoundaryErrorCode.PRODUCER_DISABLED
        published.chmod(MODE_PUBLISHED_FILE)
        os.link(published, base / "control" / "low-boundaries" / "2-second.json")
        with pytest.raises(BoundaryError):
            protected.read_published(directory, "1-first.json")
    finally:
        os.close(directory)


def test_published_names_cannot_start_with_a_dot(base: Path) -> None:
    """Имена с точкой в начале collector не читает, поэтому ими не публикуют."""
    protected = tree(base)
    directory = protected.open_directory(("low-boundaries",), create=True)
    try:
        for name in (".anchor.json", ".staging-1", "..", "a/b", "a\x00b"):
            with pytest.raises(BoundaryError) as error:
                protected.write_once(directory, name, b"{}")
            assert error.value.code is BoundaryErrorCode.PRODUCER_DISABLED
    finally:
        os.close(directory)


def test_symlink_in_the_chain_disables_the_producer(base: Path) -> None:
    """Цепочка openat не переходит по ссылкам: подмена каталога отвергается."""
    protected = tree(base)
    directory = protected.open_directory(("low-boundaries",), create=True)
    os.close(directory)
    elsewhere = base / "elsewhere"
    elsewhere.mkdir(mode=0o750)
    link = base / "control" / "low-boundaries" / "linked"
    link.symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(BoundaryError) as error:
        protected.open_directory(("low-boundaries", "linked"), create=False)
    assert error.value.code is BoundaryErrorCode.PRODUCER_DISABLED


def test_group_writable_parent_disables_the_producer(base: Path) -> None:
    """Родитель корня, доступный на запись группе или остальным, запрещён (§8.2)."""
    nested = base / "writable"
    nested.mkdir()
    # Режим задаётся отдельно: umask процесса снял бы бит записи группы у mkdir.
    nested.chmod(0o770)
    protected = ProtectedTree(nested / "control", COLLECTOR_GID, LinuxWriteOnceRename())
    with pytest.raises(BoundaryError) as error:
        protected.require_available()
    assert error.value.code is BoundaryErrorCode.PRODUCER_DISABLED


def test_missing_directory_is_not_created_without_permission(base: Path) -> None:
    """Чтение не создаёт каталогов: отсутствующий путь — выключенный producer."""
    protected = tree(base)
    with pytest.raises(BoundaryError):
        protected.open_directory(("low-boundaries", "absent"), create=False)


def test_staging_directory_is_private_and_removable(base: Path) -> None:
    """Подготовка приватна (0700), удаляется вместе с содержимым и только по префиксу."""
    protected = tree(base)
    directory = protected.open_directory(("low-snapshots",), create=True)
    try:
        staging = protected.create_staging(directory, ".staging-boundary")
        try:
            path = base / "control" / "low-snapshots" / ".staging-boundary"
            assert mode_of(path) == MODE_STAGING_DIRECTORY
            descriptor = protected.staging_file(staging, "copy.jsonl")
            os.write(descriptor, b"{}\n")
            protected.finalize_file(descriptor)
            os.close(descriptor)
            assert mode_of(path / "copy.jsonl") == MODE_PUBLISHED_FILE
        finally:
            os.close(staging)
        with pytest.raises(BoundaryError):
            protected.remove_staging(directory, "published")
        protected.remove_staging(directory, ".staging-boundary")
        assert sorted(os.listdir(directory)) == []
    finally:
        os.close(directory)


def test_published_snapshot_directory_is_read_only_and_write_once(base: Path) -> None:
    """Каталог снимков получает 0550 и не заменяется повторной публикацией (§8.3)."""
    protected = tree(base)
    directory = protected.open_directory(("low-snapshots",), create=True)
    try:
        staging = protected.create_staging(directory, ".staging-one")
        os.close(staging)
        assert protected.publish_directory(directory, ".staging-one", "boundary-one") is True
        published = base / "control" / "low-snapshots" / "boundary-one"
        assert mode_of(published) == MODE_PUBLISHED_DIRECTORY
        assert protected.directory_exists(directory, "boundary-one") is True
        staging = protected.create_staging(directory, ".staging-two")
        os.close(staging)
        assert protected.publish_directory(directory, ".staging-two", "boundary-one") is False
        protected.remove_staging(directory, ".staging-two")
    finally:
        os.close(directory)


def test_roots_must_be_absolute_and_outside_the_journal_root(base: Path) -> None:
    """Корни задаёт оператор; они не вложены в journal_root и не содержат его (§8.2)."""
    with pytest.raises(BoundaryError):
        PublicationRoots(Path("control"), base / "snapshots")
    with pytest.raises(BoundaryError):
        PublicationRoots(base / "control", base / ".." / "snapshots")
    roots = PublicationRoots(base / "control", base / "snapshots", COLLECTOR_GID)
    roots.check_outside(base / "journals")
    with pytest.raises(BoundaryError):
        roots.check_outside(base)
    with pytest.raises(BoundaryError):
        roots.check_outside(base / "control" / "nested")
    assert roots.overlaps_any(base / "snapshots" / "low-snapshots")
    assert not roots.overlaps_any(base / "journals")


def test_rename_without_replacement_reports_an_existing_name(base: Path) -> None:
    """Прямая проверка примитива: существующая цель даёт FileExistsError."""
    rename = LinuxWriteOnceRename()
    (base / "one").write_bytes(b"1")
    (base / "two").write_bytes(b"2")
    descriptor = os.open(base, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(FileExistsError):
            rename.rename(descriptor, "one", "two")
        rename.rename(descriptor, "one", "three")
    finally:
        os.close(descriptor)
    assert (base / "three").read_bytes() == b"1"
    assert (base / "two").read_bytes() == b"2"


def test_staging_is_removed_even_after_it_got_the_published_mode(base: Path) -> None:
    """Подготовка удаляется и после назначения режима 0550 (§8.4).

    Режим публикуемого каталога назначается непосредственно перед переименованием.
    Если переименование отказало, уборка обязана сработать и под обычным
    пользователем: иначе исходная причина отказа подменялась бы ошибкой прав, а
    каталог подготовки оставался бы неудаляемым и блокировал повтор копирования.
    """
    protected = tree(base)
    directory = protected.open_directory(("low-snapshots",), create=True)
    try:
        staging = protected.create_staging(directory, ".staging-boundary")
        try:
            descriptor = protected.staging_file(staging, "low-copy.jsonl")
            os.write(descriptor, b"{}\n")
            protected.finalize_file(descriptor)
            os.close(descriptor)
            # Ровно то, что делает publish_directory до renameat2.
            os.fchmod(staging, MODE_PUBLISHED_DIRECTORY)
        finally:
            os.close(staging)
        protected.remove_staging(directory, ".staging-boundary")
        assert sorted(os.listdir(directory)) == []
    finally:
        os.close(directory)


def test_published_names_are_listed_without_hidden_entries(base: Path) -> None:
    """Перечисление показывает только опубликованные имена: по ним видна история."""
    protected = tree(base)
    directory = protected.open_directory(("low-boundaries",), create=True)
    try:
        assert protected.list_published(directory) == ()
        protected.write_once(directory, "anchor.json", b'{"a":1}')
        staging = protected.create_staging(directory, ".staging-x")
        os.close(staging)
        assert protected.list_published(directory) == ("anchor.json",)
        protected.remove_staging(directory, ".staging-x")
    finally:
        os.close(directory)


def test_overlap_follows_symbolic_links(base: Path) -> None:
    """Ссылка из каталога журнала внутрь корней не проходит сверку (§8.2)."""
    control = base / "control"
    control.mkdir(mode=0o750)
    journals = base / "journals"
    journals.symlink_to(control, target_is_directory=True)
    roots = PublicationRoots(control, base / "snapshots", COLLECTOR_GID)
    with pytest.raises(BoundaryError) as error:
        roots.check_outside(journals)
    assert error.value.code is BoundaryErrorCode.PRODUCER_DISABLED
    assert roots.overlaps_any(journals / "slot")


def test_rename_through_the_raw_syscall_number_behaves_identically(base: Path) -> None:
    """Ветвь номера системного вызова ведёт себя как символ glibc (§8.2).

    В production она выбирается только на glibc старше 2.28, поэтому в прогонах
    оставалась недостижимой: §9.4 отмечал маршалинг шести аргументов как
    непроверенный. Здесь ветвь выбирается принудительно, а поведение сверяется
    с тем же примитивом через символ библиотеки.
    """
    forced = LinuxWriteOnceRename().without_library_symbol()
    if not forced.available:
        pytest.skip(forced.reason or "номер системного вызова неизвестен")
    (base / "one").write_bytes(b"1")
    (base / "two").write_bytes(b"2")
    descriptor = os.open(base, os.O_RDONLY | os.O_DIRECTORY)
    try:
        # Существующая цель не заменяется: тот же FileExistsError, что и у символа.
        with pytest.raises(FileExistsError):
            forced.rename(descriptor, "one", "two")
        forced.rename(descriptor, "one", "three")
        # Отсутствующий источник даёт обычную ошибку ядра, а не успех.
        with pytest.raises(OSError) as error:
            forced.rename(descriptor, "absent", "four")
        assert not isinstance(error.value, FileExistsError)
    finally:
        os.close(descriptor)
    assert (base / "three").read_bytes() == b"1"
    assert (base / "two").read_bytes() == b"2"
    assert not (base / "one").exists() and not (base / "four").exists()


def test_publication_works_through_the_raw_syscall_number(base: Path) -> None:
    """Публикация write-once целиком проходит на ветви номера системного вызова."""
    forced = LinuxWriteOnceRename().without_library_symbol()
    if not forced.available:
        pytest.skip(forced.reason or "номер системного вызова неизвестен")
    protected = ProtectedTree(base / "control", COLLECTOR_GID, forced)
    directory = protected.open_directory(("low-boundaries",), create=True)
    try:
        assert protected.write_once(directory, "anchor.json", b'{"a":1}') is True
        assert protected.write_once(directory, "anchor.json", b'{"a":1}') is False
        with pytest.raises(BoundaryError) as error:
            protected.write_once(directory, "anchor.json", b'{"a":2}')
        assert error.value.code is BoundaryErrorCode.BOUNDARY_PUBLICATION_CONFLICT
        assert protected.read_published(directory, "anchor.json") == b'{"a":1}'
    finally:
        os.close(directory)


def test_forced_branch_reports_an_unknown_architecture_instead_of_guessing() -> None:
    """Без известного номера ветвь объявляет себя недоступной, а не угадывает его."""
    forced = LinuxWriteOnceRename().without_library_symbol()
    forced._number = None  # noqa: SLF001 - проверяется именно внутренний выбор ветви
    forced._syscall = None  # noqa: SLF001
    assert not forced.available
    with pytest.raises(BoundaryError) as error:
        forced.rename(0, "one", "two")
    assert error.value.code is BoundaryErrorCode.PRODUCER_DISABLED
