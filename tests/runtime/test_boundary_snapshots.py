"""Закрепление байтов журналов закрываемого писателя (§8.3). Только Linux.

Проверяются обычные копии (не ссылки), неизменность исходников, перечитывание копии
новым дескриптором, пределы §8.5 и сверка опубликованного набора с перечнем.
"""

import dataclasses
import os
import stat
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest
from boundary_support import collector_gid, publication_base, require_linux

from agent.runtime.boundary.documents import SnapshotEntry, WriterBinding
from agent.runtime.boundary.errors import BoundaryError, BoundaryErrorCode
from agent.runtime.boundary.journals import (
    JournalFile,
    JournalHeader,
    JournalMeasurement,
    JournalSlot,
    journal_name,
)
from agent.runtime.boundary.publication import (
    MODE_PUBLISHED_DIRECTORY,
    MODE_PUBLISHED_FILE,
    LinuxWriteOnceRename,
    ProtectedTree,
)
from agent.runtime.boundary.snapshots import SnapshotLimits, SnapshotStore

SAMPLES = Path(__file__).resolve().parents[2] / "specs/samples/low-process-boundary"
INSTANCE = "a2df557e-785f-48f3-ba6e-348b76730f41"
DEPLOYMENT = "e0d692f0-fb4c-4a37-920e-0c8683471639"
AGENT = "00000000-0000-4000-8000-000000000001"
BOUNDARY = "d6e623c9-425a-4ce4-9b31-24a8ad509283"
FIRST_JOURNAL = "61c4c12f-bcd3-45eb-8498-d04f84d6cc75"
COLLECTOR_GID = collector_gid()

require_linux("снимков")

pytestmark = pytest.mark.skipif(
    sys.platform != "linux",
    reason="права POSIX и публикация без замены проверяются Linux-шагом",
)


@pytest.fixture
def base() -> Iterator[Path]:
    """Безопасная база для snapshot-root: без записи группе и остальным (§8.2)."""
    yield from publication_base("snapshots")


def binding() -> WriterBinding:
    return WriterBinding(AGENT, DEPLOYMENT, INSTANCE, 1)


def store(base: Path) -> SnapshotStore:
    return SnapshotStore(ProtectedTree(base / "snapshots", COLLECTOR_GID, LinuxWriteOnceRename()))


def slot_with(base: Path, source: str) -> tuple[Path, Sequence[JournalFile]]:
    """Готовит каталог слота из фикстуры и собирает набор закрываемого писателя."""
    directory = base / "journals" / DEPLOYMENT
    directory.mkdir(mode=0o750, parents=True, exist_ok=True)
    target = directory / journal_name(INSTANCE, 1)
    target.write_bytes((SAMPLES / source).read_bytes())
    chain = JournalSlot(directory, binding()).collect_writer_chain(FIRST_JOURNAL, 1)
    return directory, chain


def published(base: Path, name: str) -> Path:
    return base / "snapshots" / "low-snapshots" / AGENT / DEPLOYMENT / INSTANCE / BOUNDARY / name


def limits(maximum: int = 8 * 1024 * 1024, quota: int = 256 * 1024 * 1024) -> SnapshotLimits:
    return SnapshotLimits(maximum, quota)


def synthetic(count: int, size: int) -> list[JournalFile]:
    """Набор без файлов на диске: пределы проверяются до копирования (§8.5)."""
    return [
        JournalFile(
            path=Path(journal_name(INSTANCE, number)),
            header=JournalHeader(
                journal_id=f"61c4c12f-bcd3-45eb-8498-d04f84d6cc{number:02d}",
                journal_seq=number,
                previous_journal_id=None,
                instance_id=INSTANCE,
                deployment_id=DEPLOYMENT,
                profile_revision=1,
                first_seq=1,
                header_bytes=336,
                header_sha256="0" * 64,
            ),
            measurement=JournalMeasurement(size, "0" * 64, size, False),
        )
        for number in range(1, count + 1)
    ]


def test_copies_are_plain_files_and_sources_stay_untouched(base: Path) -> None:
    """Копия — новый обычный файл; исходник остаётся на месте и не меняется."""
    directory, chain = slot_with(base, "old-crash.jsonl")
    source = chain[0].path
    before = source.lstat()
    entries = store(base).stage(binding(), BOUNDARY, chain, limits())
    assert len(entries) == 1
    entry = entries[0]
    assert entry.journal_id == FIRST_JOURNAL
    assert entry.snapshot_ref == journal_name(INSTANCE, 1)
    assert (entry.size_bytes, entry.complete_lf_end, entry.footer_present) == (885, 885, False)
    assert entry.sha256 == chain[0].measurement.sha256
    copy = published(base, entry.snapshot_ref)
    info = copy.lstat()
    assert stat.S_ISREG(info.st_mode) and info.st_nlink == 1
    assert info.st_ino != before.st_ino
    assert stat.S_IMODE(info.st_mode) == MODE_PUBLISHED_FILE
    assert info.st_gid == COLLECTOR_GID
    assert copy.read_bytes() == source.read_bytes()
    after = source.lstat()
    assert (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)
    assert stat.S_IMODE(copy.parent.lstat().st_mode) == MODE_PUBLISHED_DIRECTORY
    assert sorted(path.name for path in directory.iterdir()) == [journal_name(INSTANCE, 1)]


def test_incomplete_tail_is_preserved_in_the_copy(base: Path) -> None:
    """Все байты, включая неполный хвост, попадают в копию и в хеш (§8.3)."""
    _, chain = slot_with(base, "old-with-tail.jsonl")
    entries = store(base).stage(binding(), BOUNDARY, chain, limits())
    entry = entries[0]
    assert (entry.size_bytes, entry.complete_lf_end, entry.tail_bytes) == (910, 885, 25)
    assert entry.sha256 == "ee98c6080d62239d899514cfee4316306cfeb116b89d3db00d194d271612c422"
    assert published(base, entry.snapshot_ref).stat().st_size == 910


def test_verification_detects_a_changed_copy(base: Path) -> None:
    """Перед первым использованием проверяются размер и SHA-256 копии (§8.3)."""
    _, chain = slot_with(base, "old-crash.jsonl")
    snapshots = store(base)
    entries = snapshots.stage(binding(), BOUNDARY, chain, limits())
    snapshots.verify_published(binding(), BOUNDARY, entries)
    copy = published(base, entries[0].snapshot_ref)
    copy.parent.chmod(0o750)
    copy.chmod(0o640)
    copy.write_bytes(copy.read_bytes().replace(b'"seq":1', b'"seq":9', 1))
    with pytest.raises(BoundaryError) as error:
        snapshots.verify_published(binding(), BOUNDARY, entries)
    assert error.value.code is BoundaryErrorCode.BOUNDARY_SNAPSHOT_UNAVAILABLE


def test_verification_detects_a_different_set(base: Path) -> None:
    """Состав каталога снимков обязан совпадать с перечнем registry."""
    _, chain = slot_with(base, "old-crash.jsonl")
    snapshots = store(base)
    entries = snapshots.stage(binding(), BOUNDARY, chain, limits())
    extra = SnapshotEntry(
        journal_id="20c648ef-de66-4bf1-94ef-deebdc14a298",
        journal_seq=2,
        snapshot_ref=journal_name(INSTANCE, 2),
        size_bytes=1,
        sha256="0" * 64,
        complete_lf_end=0,
        footer_present=False,
    )
    with pytest.raises(BoundaryError) as error:
        snapshots.verify_published(binding(), BOUNDARY, (*entries, extra))
    assert error.value.code is BoundaryErrorCode.BOUNDARY_SNAPSHOT_UNAVAILABLE


def test_published_transition_directory_is_never_replaced(base: Path) -> None:
    """Повторная подготовка того же перехода — конфликт публикации, а не замена."""
    _, chain = slot_with(base, "old-crash.jsonl")
    snapshots = store(base)
    snapshots.stage(binding(), BOUNDARY, chain, limits())
    with pytest.raises(BoundaryError) as error:
        snapshots.stage(binding(), BOUNDARY, chain, limits())
    assert error.value.code is BoundaryErrorCode.BOUNDARY_PUBLICATION_CONFLICT


def test_limits_are_checked_before_copying(base: Path) -> None:
    """Больше 64 журналов или размер больше предела писателя — LIMIT_EXCEEDED (§8.5)."""
    snapshots = store(base)
    with pytest.raises(BoundaryError) as error:
        snapshots.stage(binding(), BOUNDARY, synthetic(65, 100), limits())
    assert error.value.code is BoundaryErrorCode.LIMIT_EXCEEDED
    with pytest.raises(BoundaryError) as error:
        snapshots.stage(binding(), BOUNDARY, synthetic(1, 1024), limits(maximum=512))
    assert error.value.code is BoundaryErrorCode.LIMIT_EXCEEDED
    with pytest.raises(BoundaryError) as error:
        snapshots.stage(binding(), BOUNDARY, synthetic(4, 300), limits(maximum=512, quota=1000))
    assert error.value.code is BoundaryErrorCode.LIMIT_EXCEEDED
    # Ни одна из неудач не оставила каталога перехода и подготовки.
    assert not published(base, "any").parent.exists()


def test_too_long_tail_blocks_the_transition_after_copying(base: Path) -> None:
    """Хвост больше 65535 байт проверяется по копиям и блокирует переход (§8.5)."""
    directory = base / "journals" / DEPLOYMENT
    directory.mkdir(mode=0o750, parents=True)
    target = directory / journal_name(INSTANCE, 1)
    header = (SAMPLES / "old-crash.jsonl").read_bytes().split(b"\n", 1)[0]
    target.write_bytes(header + b"\n" + b"x" * 70000)
    chain = JournalSlot(directory, binding()).collect_writer_chain(FIRST_JOURNAL, 1)
    with pytest.raises(BoundaryError) as error:
        store(base).stage(binding(), BOUNDARY, chain, limits())
    assert error.value.code is BoundaryErrorCode.LIMIT_EXCEEDED
    # Подготовка снята: нового RW не было, исходник на месте.
    parent = published(base, "any").parent.parent
    assert sorted(path.name for path in parent.iterdir()) == []
    assert target.stat().st_size == len(header) + 1 + 70000


class MutatingStore(SnapshotStore):
    """Подмена для воспроизведения изменения исходника во время копирования."""

    def __init__(self, tree: ProtectedTree, source: Path) -> None:
        super().__init__(tree)
        self._source = source
        self._mutated = False

    def _write_all(self, descriptor: int, data: bytes) -> None:  # type: ignore[override]
        """Дописывает исходник после первого блока: ограждение оказалось неполным."""
        if not self._mutated:
            self._mutated = True
            with self._source.open("ab") as stream:
                stream.write(b"{}\n")
        os.write(descriptor, data)


def test_source_changed_during_copying_stops_the_transition(base: Path) -> None:
    """Изменившийся исходник означает неполное ограждение: SOURCE_CHANGED (§8.3)."""
    _, chain = slot_with(base, "old-crash.jsonl")
    tree = ProtectedTree(base / "snapshots", COLLECTOR_GID, LinuxWriteOnceRename())
    snapshots = MutatingStore(tree, chain[0].path)
    with pytest.raises(BoundaryError) as error:
        snapshots.stage(binding(), BOUNDARY, chain, limits())
    assert error.value.code is BoundaryErrorCode.SOURCE_CHANGED
    assert not published(base, "any").parent.exists()


def test_operator_quota_of_the_snapshot_root_stops_copying(base: Path) -> None:
    """Квота оператора на snapshot_root проверяется до копирования (§8.3 «Место»)."""
    _, chain = slot_with(base, "old-crash.jsonl")
    tight = SnapshotStore(
        ProtectedTree(base / "snapshots", COLLECTOR_GID, LinuxWriteOnceRename()),
        quota_bytes=16,
    )
    with pytest.raises(BoundaryError) as error:
        tight.check_capacity(binding(), chain)
    assert error.value.code is BoundaryErrorCode.BOUNDARY_SNAPSHOT_UNAVAILABLE
    with pytest.raises(BoundaryError):
        tight.stage(binding(), BOUNDARY, chain, limits())
    assert not (base / "snapshots" / "low-snapshots").exists()
    # Без квоты тот же набор закрепляется, и занятое место становится видимым.
    roomy = store(base)
    roomy.check_capacity(binding(), chain)
    entries = roomy.stage(binding(), BOUNDARY, chain, limits())
    assert roomy.subtree_usage() == entries[0].size_bytes


def test_published_set_is_adopted_only_when_it_matches_the_chain(base: Path) -> None:
    """Перенятый каталог сверяется с байтами цепочки, а не принимается на веру (§8.3)."""
    _, chain = slot_with(base, "old-crash.jsonl")
    snapshots = store(base)
    entries = snapshots.stage(binding(), BOUNDARY, chain, limits())
    assert snapshots.adopt_published(binding(), BOUNDARY, chain) == entries
    # Цепочка с другими байтами тому же каталогу не соответствует.
    other = base / "journals" / DEPLOYMENT / journal_name(INSTANCE, 1)
    other.write_bytes((SAMPLES / "old-with-tail.jsonl").read_bytes())
    changed = JournalSlot(other.parent, binding()).collect_writer_chain(FIRST_JOURNAL, 1)
    with pytest.raises(BoundaryError) as error:
        snapshots.adopt_published(binding(), BOUNDARY, changed)
    assert error.value.code is BoundaryErrorCode.BOUNDARY_SNAPSHOT_UNAVAILABLE


def test_copy_that_differs_from_the_measurement_is_source_changed(base: Path) -> None:
    """Копия сверяется с измерением исходника до копирования (§3 шаг 3)."""
    _, chain = slot_with(base, "old-crash.jsonl")
    # Подменяем измерение так, будто журнал был короче: копия обязана не совпасть.
    shortened = dataclasses.replace(
        chain[0],
        measurement=dataclasses.replace(chain[0].measurement, size_bytes=100),
    )
    with pytest.raises(BoundaryError) as error:
        store(base).stage(binding(), BOUNDARY, (shortened,), limits())
    assert error.value.code is BoundaryErrorCode.SOURCE_CHANGED
    assert not published(base, "any").parent.exists()
