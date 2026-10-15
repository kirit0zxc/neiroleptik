"""Закрепление байтов журналов закрываемого writer до выдачи нового RW (§8.3).

Снимок закрепляет конкретные старые байты, даже если новый writer способен изменить
общий исходный каталог. Копии обычные: не hardlink, не symlink, не bind mount и не
reflink, поэтому новый Low не может их изменить. Исходники остаются на месте: модуль
их не удаляет, не перемещает и не дописывает.

Хеш и размер проверяются по перечитанной копии новым дескриптором, а исходник
сверяется по размеру и mtime до и после чтения: изменившийся исходник означает, что
ограждение неполно, и переход становится неизвестным.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from collections.abc import Sequence
from dataclasses import dataclass

from agent.runtime.boundary.documents import (
    MAX_JOURNALS,
    MAX_TAIL_BYTES,
    MAX_TOTAL_TAIL_BYTES,
    SnapshotEntry,
    WriterBinding,
)
from agent.runtime.boundary.errors import BoundaryError, BoundaryErrorCode
from agent.runtime.boundary.journals import (
    JournalFile,
    JournalMeasurement,
    JournalScan,
    journal_name,
    read_exactly,
)
from agent.runtime.boundary.publication import ProtectedTree

SNAPSHOT_PREFIX = "low-snapshots"
_CHUNK_BYTES = 256 * 1024
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)


@dataclass(frozen=True)
class SnapshotLimits:
    """Пределы, с которыми запускался закрываемый writer; хранятся в его intent (§8.5)."""

    journal_max_bytes: int
    journal_quota_bytes: int


def _unavailable(detail: str) -> BoundaryError:
    return BoundaryError(BoundaryErrorCode.BOUNDARY_SNAPSHOT_UNAVAILABLE, detail)


def _exceeded(detail: str) -> BoundaryError:
    return BoundaryError(BoundaryErrorCode.LIMIT_EXCEEDED, detail)


class SnapshotStore:
    """Защищённый snapshot-root одного runtime: единственный писатель — producer."""

    def __init__(self, tree: ProtectedTree, quota_bytes: int | None = None) -> None:
        self._tree = tree
        # Квота оператора на snapshot_root; None — ограничением остаётся свободное место.
        self._quota_bytes = quota_bytes

    @staticmethod
    def directory_parts(binding: WriterBinding) -> tuple[str, ...]:
        """Раскладка snapshot-root из §8.2 без идентификатора перехода."""
        return (
            SNAPSHOT_PREFIX,
            binding.agent_id,
            binding.deployment_id,
            binding.instance_id,
        )

    def stage(
        self,
        binding: WriterBinding,
        boundary_id: str,
        journals: Sequence[JournalFile],
        limits: SnapshotLimits,
    ) -> tuple[SnapshotEntry, ...]:
        """Копирует весь набор журналов writer и публикует каталог перехода.

        Пределы числа журналов и размеров проверяются до копирования, длина хвоста —
        по готовым копиям. Любой отказ снимает незавершённую подготовку: исходники
        неизменны, нового RW ещё не было.
        """
        self._check_before_copy(journals, limits)
        self.check_capacity(binding, journals)
        parent = self._tree.open_directory(self.directory_parts(binding), create=True)
        staging = f".staging-{boundary_id}"
        try:
            if self._tree.directory_exists(parent, boundary_id):
                raise BoundaryError(
                    BoundaryErrorCode.BOUNDARY_PUBLICATION_CONFLICT,
                    "каталог снимков этого перехода уже опубликован",
                )
            staging_fd = self._tree.create_staging(parent, staging)
            try:
                entries = tuple(self._copy(binding, journal, staging_fd) for journal in journals)
                self._check_after_copy(entries)
                os.fsync(staging_fd)
            finally:
                os.close(staging_fd)
            if not self._tree.publish_directory(parent, staging, boundary_id):
                raise BoundaryError(
                    BoundaryErrorCode.BOUNDARY_PUBLICATION_CONFLICT,
                    "каталог снимков этого перехода уже опубликован",
                )
            return entries
        except BaseException:
            self._tree.remove_staging(parent, staging)
            raise
        finally:
            os.close(parent)

    def check_capacity(self, binding: WriterBinding, journals: Sequence[JournalFile]) -> None:
        """Проверяет место под копии в snapshot_root с учётом подготовки (§8.3 «Место»).

        Проверяется и фактическое свободное место файловой системы, и отдельная квота
        оператора, если она задана. При нехватке снимки не создаются, новый writer не
        запускается и доказательства нет. Вызывается до ограждения по текущим размерам
        и повторно внутри `stage` по фактическим.
        """
        required = sum(journal.measurement.size_bytes for journal in journals)
        if required <= 0:
            return
        root = self._tree.root
        try:
            free = shutil.disk_usage(root if root.exists() else root.parent).free
        except OSError as error:
            raise _unavailable("свободное место snapshot_root недоступно") from error
        if free < required:
            raise _unavailable("в snapshot_root не хватает места под копии журналов")
        quota = self._quota_bytes
        if quota is not None and self.subtree_usage() + required > quota:
            raise _unavailable("квота snapshot_root не позволяет закрепить копии")

    def subtree_usage(self) -> int:
        """Занятое место снимков вместе с незавершённой подготовкой под корнем дерева."""
        total = 0
        root = self._tree.root / SNAPSHOT_PREFIX
        for current, _directories, files in os.walk(root, followlinks=False):
            for name in files:
                try:
                    total += os.stat(os.path.join(current, name), follow_symlinks=False).st_size
                except OSError:
                    # Файл исчез между перечислением и stat: учёт места приблизительный.
                    continue
        return total

    def adopt_published(
        self,
        binding: WriterBinding,
        boundary_id: str,
        journals: Sequence[JournalFile],
    ) -> tuple[SnapshotEntry, ...]:
        """Перенимает уже опубликованный каталог снимков, сверив его с цепочкой.

        Нужно, когда процесс прервался между атомарной публикацией каталога и записью
        перечня в registry: копии неизменны и write-once, поэтому перечень
        восстанавливается по ним, а не копированием заново. Каждая копия сверяется с
        измерением своего журнала; расхождение означает, что каталог относится к другим
        байтам, и переход останавливается.
        """
        parent = self._tree.open_directory(self.directory_parts(binding), create=False)
        try:
            directory = os.open(
                boundary_id, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW, dir_fd=parent
            )
        except FileNotFoundError:
            raise _unavailable("каталог снимков перехода отсутствует") from None
        except OSError as error:
            raise _unavailable("каталог снимков перехода недоступен") from error
        finally:
            os.close(parent)
        try:
            present = set(os.listdir(directory))
            entries = tuple(
                SnapshotEntry(
                    journal_id=journal.header.journal_id,
                    journal_seq=journal.header.journal_seq,
                    snapshot_ref=journal_name(binding.instance_id, journal.header.journal_seq),
                    size_bytes=journal.measurement.size_bytes,
                    sha256=journal.measurement.sha256,
                    complete_lf_end=journal.measurement.complete_lf_end,
                    footer_present=journal.measurement.footer_present,
                )
                for journal in journals
            )
            if present != {entry.snapshot_ref for entry in entries}:
                raise _unavailable("состав опубликованного каталога снимков не совпадает")
            self._check_after_copy(entries)
            for entry in entries:
                self._verify_copy(directory, entry)
            return entries
        finally:
            os.close(directory)

    def verify_published(
        self, binding: WriterBinding, boundary_id: str, entries: Sequence[SnapshotEntry]
    ) -> None:
        """Сверяет опубликованный набор с registry перед повторным использованием (§8.3).

        Проверяются состав каталога, размер и SHA-256 каждой копии. Несовпадение —
        ошибка целостности без возврата к изменяемому оригиналу.
        """
        parent = self._tree.open_directory(self.directory_parts(binding), create=False)
        try:
            directory = os.open(
                boundary_id, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW, dir_fd=parent
            )
        except FileNotFoundError:
            raise _unavailable("каталог снимков перехода отсутствует") from None
        except OSError as error:
            raise _unavailable("каталог снимков перехода недоступен") from error
        finally:
            os.close(parent)
        try:
            present = set(os.listdir(directory))
            expected = {entry.snapshot_ref for entry in entries}
            if present != expected:
                raise _unavailable("состав каталога снимков не совпадает с registry")
            for entry in entries:
                self._verify_copy(directory, entry)
        finally:
            os.close(directory)

    def _check_before_copy(self, journals: Sequence[JournalFile], limits: SnapshotLimits) -> None:
        """Пределы §8.5 проверяются после ограждения и до копирования."""
        if not journals:
            raise _unavailable("набор журналов закрываемого writer пуст")
        if len(journals) > MAX_JOURNALS:
            raise _exceeded("больше 64 журналов на переход")
        total = 0
        for journal in journals:
            size = journal.measurement.size_bytes
            if size > limits.journal_max_bytes:
                raise _exceeded("журнал больше journal_max_bytes закрываемого writer")
            total += size
        if total > limits.journal_quota_bytes:
            raise _exceeded("сумма журналов больше journal_quota_bytes закрываемого writer")

    @staticmethod
    def _check_after_copy(entries: Sequence[SnapshotEntry]) -> None:
        """Длина неполного хвоста проверяется по копиям до запуска нового writer."""
        total = 0
        for entry in entries:
            if entry.tail_bytes > MAX_TAIL_BYTES:
                raise _exceeded("неполный хвост журнала больше 65535 байт")
            total += entry.tail_bytes
        if total > MAX_TOTAL_TAIL_BYTES:
            raise _exceeded("суммарный неполный хвост больше предела")

    def _copy(self, binding: WriterBinding, journal: JournalFile, staging_fd: int) -> SnapshotEntry:
        """Копирует один журнал в подготовку и перечитывает копию новым дескриптором."""
        name = journal_name(binding.instance_id, journal.header.journal_seq)
        if journal.path.name != name:
            raise _unavailable("имя исходного журнала не совпадает с его номером")
        source = self._open_source(journal)
        try:
            before = os.fstat(source)
            size = before.st_size
            scan = JournalScan()
            destination = self._tree.staging_file(staging_fd, name)
            try:
                while scan.read_bytes < size:
                    chunk = os.read(source, min(_CHUNK_BYTES, size - scan.read_bytes))
                    if not chunk:
                        raise BoundaryError(
                            BoundaryErrorCode.SOURCE_CHANGED,
                            "журнал короче объявленного размера",
                        )
                    self._write_all(destination, chunk)
                    scan.update(chunk)
                after = os.fstat(source)
                if after.st_size != size or after.st_mtime_ns != before.st_mtime_ns:
                    raise BoundaryError(
                        BoundaryErrorCode.SOURCE_CHANGED,
                        "журнал изменился во время копирования",
                    )
                self._tree.finalize_file(destination)
            finally:
                os.close(destination)
        finally:
            os.close(source)
        measurement = scan.result(journal.header.journal_id)
        self._require_same_bytes(journal, measurement)
        entry = SnapshotEntry(
            journal_id=journal.header.journal_id,
            journal_seq=journal.header.journal_seq,
            snapshot_ref=name,
            size_bytes=measurement.size_bytes,
            sha256=measurement.sha256,
            complete_lf_end=measurement.complete_lf_end,
            footer_present=measurement.footer_present,
        )
        self._verify_copy(staging_fd, entry, header_sha256=journal.header.header_sha256)
        return entry

    @staticmethod
    def _require_same_bytes(journal: JournalFile, measurement: JournalMeasurement) -> None:
        """Копия обязана совпасть с измерением исходника, сделанным при перечислении.

        Снимок закрепляет **старые** байты (§3 шаг 3). Между перечислением цепочки и
        копированием закрываемый writer уже огорожен, поэтому любое расхождение
        означает, что ограждение неполно, и переход становится неизвестным. Повторный
        fstat внутри копирования покрывает только изменение во время чтения.
        """
        expected = journal.measurement
        if (
            measurement.size_bytes != expected.size_bytes
            or measurement.sha256 != expected.sha256
            or measurement.complete_lf_end != expected.complete_lf_end
            or measurement.footer_present != expected.footer_present
        ):
            raise BoundaryError(
                BoundaryErrorCode.SOURCE_CHANGED,
                "копия журнала не совпадает с его измерением до копирования",
            )

    def _verify_copy(
        self, directory_fd: int, entry: SnapshotEntry, header_sha256: str | None = None
    ) -> None:
        """Перечитывает копию новым дескриптором и сверяет размер, хеш и заголовок."""
        try:
            descriptor = os.open(entry.snapshot_ref, os.O_RDONLY | _O_NOFOLLOW, dir_fd=directory_fd)
        except FileNotFoundError:
            raise _unavailable("копия журнала отсутствует") from None
        except OSError as error:
            raise _unavailable("копия журнала недоступна") from error
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise _unavailable("копия журнала не обычный файл с одной ссылкой")
            if info.st_size != entry.size_bytes:
                raise _unavailable("размер копии журнала не совпадает")
            scan = read_exactly(descriptor, info.st_size)
            measurement = scan.result(entry.journal_id)
            if (
                measurement.sha256 != entry.sha256
                or measurement.complete_lf_end != entry.complete_lf_end
                or measurement.footer_present != entry.footer_present
            ):
                raise _unavailable("копия журнала не совпадает с перечнем")
            if header_sha256 is not None:
                os.lseek(descriptor, 0, os.SEEK_SET)
                first = self._read_header_hash(descriptor)
                if first != header_sha256:
                    raise _unavailable("заголовок копии не совпадает с исходником")
        finally:
            os.close(descriptor)

    @staticmethod
    def _read_header_hash(descriptor: int) -> str:
        """Хеш первой строки копии вместе с LF: тот же способ, что у привязки writer."""
        raw = b""
        while b"\n" not in raw and len(raw) < 4096:
            chunk = os.read(descriptor, 4096 - len(raw))
            if not chunk:
                break
            raw += chunk
        if b"\n" not in raw:
            raise _unavailable("копия журнала без полной строки заголовка")
        return hashlib.sha256(raw[: raw.index(b"\n") + 1]).hexdigest()

    @staticmethod
    def _open_source(journal: JournalFile) -> int:
        """Исходник открывается только для чтения и без перехода по ссылке."""
        try:
            descriptor = os.open(journal.path, os.O_RDONLY | _O_NOFOLLOW)
        except OSError as error:
            raise _unavailable("исходный журнал недоступен") from error
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise _unavailable("исходный журнал не обычный файл")
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    @staticmethod
    def _write_all(descriptor: int, data: bytes) -> None:
        """Небуферизованная запись может принять часть данных: дописывает остаток.

        Нехватка места в snapshot_root приходит сюда как OSError и переводится в
        BOUNDARY_SNAPSHOT_UNAVAILABLE: сырой OSError не отличался бы от ошибки
        конфигурации и не сообщал бы, что данные остались на месте (§8.3).
        """
        view = memoryview(data)
        while view:
            try:
                written = os.write(descriptor, view)
            except OSError as error:
                raise _unavailable("копию журнала не удалось записать") from error
            if not written:
                raise _unavailable("запись копии журнала не продвигается")
            view = view[written:]
