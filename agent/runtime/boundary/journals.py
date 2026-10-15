"""Чтение каталога слота Low для producer границы процесса (§8.1, §8.3 предложения).

Модуль наблюдает файлы журнала и ничего в них не меняет: он не дописывает footer,
не удаляет и не переименовывает исходники. Все проверки выполняются по фактическим
байтам, а не по имени файла: имя лишь выбирает кандидатов каталога слота.

Формат журнала не меняется (honeyforge-low-journal/1): writer_id в заголовок не
пишется, связь writer и журнала хранит runtime registry.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from agent.runtime.boundary.documents import MAX_SAFE_INTEGER, WriterBinding
from agent.runtime.boundary.errors import BoundaryError, BoundaryErrorCode

JOURNAL_SCHEMA = "honeyforge-low-journal/1"
# Заголовок читается в пределах одной страницы: больше у honeyforge-low-journal/1 не бывает.
MAX_HEADER_BYTES = 4096
# Размер блока чтения при подсчёте хеша копии.
_CHUNK_BYTES = 256 * 1024
_UUID_TEXT = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_JOURNAL_NAME = re.compile(rf"low-({_UUID_TEXT})-(\d{{6,}})\.jsonl")


def _unavailable(detail: str) -> BoundaryError:
    """Набор журналов закрываемого writer не доказан: переход останавливается (§8.3)."""
    return BoundaryError(BoundaryErrorCode.BOUNDARY_SNAPSHOT_UNAVAILABLE, detail)


def read_exactly(descriptor: int, size: int) -> JournalScan:
    """Читает ровно size байт дескриптора и возвращает завершённый подсчёт.

    Короткое чтение означает, что файл изменился под нами: ограждение неполно.
    """
    scan = JournalScan()
    while scan.read_bytes < size:
        chunk = os.read(descriptor, min(_CHUNK_BYTES, size - scan.read_bytes))
        if not chunk:
            raise BoundaryError(
                BoundaryErrorCode.SOURCE_CHANGED, "файл короче объявленного размера"
            )
        scan.update(chunk)
    return scan


def journal_name(instance_id: str, journal_seq: int) -> str:
    """Каноническое имя поколения, которое создаёт сам Low: номер с нулями до шести знаков."""
    if type(journal_seq) is not int or not 1 <= journal_seq <= MAX_SAFE_INTEGER:
        raise _unavailable("недопустимый номер поколения")
    return f"low-{str(UUID(instance_id))}-{journal_seq:06d}.jsonl"


@dataclass(frozen=True)
class JournalHeader:
    """Первая строка поколения вместе с её LF: по ней считается header_sha256 (§8.1)."""

    journal_id: str
    journal_seq: int
    previous_journal_id: str | None
    instance_id: str
    deployment_id: str
    profile_revision: int
    first_seq: int
    header_bytes: int
    header_sha256: str


@dataclass(frozen=True)
class JournalMeasurement:
    """Фактические байты файла: размер, граница полных строк, хвост и наличие footer."""

    size_bytes: int
    sha256: str
    complete_lf_end: int
    footer_present: bool

    @property
    def tail_bytes(self) -> int:
        """Неполный хвост в конце файла; сам по себе он не означает ошибку Low."""
        return self.size_bytes - self.complete_lf_end


@dataclass(frozen=True)
class JournalFile:
    """Наблюдённое поколение каталога слота: путь, заголовок и измеренные байты."""

    path: Path
    header: JournalHeader
    measurement: JournalMeasurement


class JournalScan:
    """Потоковый подсчёт по байтам журнала: хеш, граница полных строк и последняя строка.

    Класс используется и при наблюдении исходника, и при проверке готовой копии,
    поэтому правило границы полных строк в двух местах не расходится.
    """

    def __init__(self) -> None:
        self._digest = hashlib.sha256()
        self._read = 0
        self._complete_lf_end = 0
        # pending — байты после последнего LF, last_line — последняя полная строка.
        self._pending = b""
        self._last_line = b""

    @property
    def read_bytes(self) -> int:
        """Сколько байт уже прошло через подсчёт."""
        return self._read

    def update(self, chunk: bytes) -> None:
        """Добавляет очередной блок; поиск LF идёт по концу блока, без разбора строк."""
        self._digest.update(chunk)
        position = chunk.rfind(b"\n")
        if position < 0:
            self._pending += chunk
        else:
            previous = chunk.rfind(b"\n", 0, position)
            self._last_line = (
                self._pending + chunk[:position] if previous < 0 else chunk[previous + 1 : position]
            )
            self._pending = chunk[position + 1 :]
            self._complete_lf_end = self._read + position + 1
        self._read += len(chunk)

    def result(self, journal_id: str | None) -> JournalMeasurement:
        """Итог подсчёта; наличие footer проверяется по последней полной строке."""
        return JournalMeasurement(
            size_bytes=self._read,
            sha256=self._digest.hexdigest(),
            complete_lf_end=self._complete_lf_end,
            footer_present=is_footer_line(self._last_line, journal_id),
        )


def is_footer_line(line: bytes, journal_id: str | None) -> bool:
    """Структурная проверка последней полной строки; false не значит пустой журнал.

    Концовка принимается только для своего поколения: строка footer с чужим
    journal_id закрытием этого файла не является.
    """
    try:
        value = json.loads(line.decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        return False
    if not isinstance(value, dict) or value.get("record") != "footer":
        return False
    return journal_id is None or value.get("journal_id") == journal_id


class JournalReader:
    """Читает заголовок и байты поколения, не переходя по ссылкам и не меняя файл."""

    def read_header(self, path: Path) -> JournalHeader:
        """Читает и проверяет полную первую строку; отсутствие LF — отказ перехода.

        Файл обязан быть обычным: symlink, FIFO и каталог не принимаются даже тогда,
        когда их имя совпадает с именем поколения Low.
        """
        descriptor = self._open(path)
        try:
            raw = b""
            while b"\n" not in raw and len(raw) < MAX_HEADER_BYTES:
                chunk = os.read(descriptor, MAX_HEADER_BYTES - len(raw))
                if not chunk:
                    break
                raw += chunk
        except OSError as error:
            raise _unavailable("журнал недоступен для чтения") from error
        finally:
            os.close(descriptor)
        if b"\n" not in raw:
            raise _unavailable("журнал без полной строки заголовка")
        line = raw[: raw.index(b"\n") + 1]
        return self._parse_header(line)

    def measure(self, path: Path, journal_id: str | None = None) -> JournalMeasurement:
        """Считает размер, SHA-256, границу полных строк и наличие footer по байтам файла.

        Размер и mtime фиксируются fstat до чтения и сверяются после: изменившийся
        исходник означает неполное ограждение, поэтому переход становится неизвестным
        (§8.3). Хеш считается по всем байтам, включая неполный хвост.
        """
        descriptor = self._open(path)
        try:
            before = os.fstat(descriptor)
            scan = read_exactly(descriptor, before.st_size)
            after = os.fstat(descriptor)
            if after.st_size != before.st_size or after.st_mtime_ns != before.st_mtime_ns:
                raise BoundaryError(
                    BoundaryErrorCode.SOURCE_CHANGED, "журнал изменился во время чтения"
                )
            return scan.result(journal_id)
        except OSError as error:
            raise _unavailable("журнал недоступен для чтения") from error
        finally:
            os.close(descriptor)

    @staticmethod
    def _open(path: Path) -> int:
        """Открывает обычный файл без перехода по ссылке и reparse point."""
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as error:
            raise _unavailable("журнал недоступен для чтения") from error
        try:
            info = os.fstat(descriptor)
            reparse = getattr(info, "st_file_attributes", 0) & getattr(
                stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0
            )
            if not stat.S_ISREG(info.st_mode) or reparse:
                raise _unavailable("журнал не является обычным файлом")
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    @staticmethod
    def _parse_header(line: bytes) -> JournalHeader:
        """Строгий разбор заголовка; хеш считается по строке вместе с LF (§8.1)."""
        try:
            value = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise _unavailable("заголовок журнала не разбирается") from error
        if not isinstance(value, dict) or value.get("record") != "header":
            raise _unavailable("первая строка журнала не заголовок")
        if value.get("schema") != JOURNAL_SCHEMA:
            raise _unavailable("неизвестная схема журнала")
        try:
            journal_id = str(UUID(str(value["journal_id"])))
            instance_id = str(UUID(str(value["instance_id"])))
            deployment_id = str(UUID(str(value["deployment_id"])))
            previous = value["previous_journal_id"]
            previous_id = None if previous is None else str(UUID(str(previous)))
            journal_seq = value["journal_seq"]
            first_seq = value["first_seq"]
            revision = value["profile_revision"]
        except (KeyError, ValueError, TypeError, AttributeError) as error:
            raise _unavailable("заголовок журнала неполон") from error
        for number in (journal_seq, first_seq, revision):
            if type(number) is not int or not 1 <= number <= MAX_SAFE_INTEGER:
                raise _unavailable("числовое поле заголовка недопустимо")
        return JournalHeader(
            journal_id=journal_id,
            journal_seq=journal_seq,
            previous_journal_id=previous_id,
            instance_id=instance_id,
            deployment_id=deployment_id,
            profile_revision=revision,
            first_seq=first_seq,
            header_bytes=len(line),
            header_sha256=hashlib.sha256(line).hexdigest(),
        )


class JournalSlot:
    """Каталог слота одного назначения: поколения экземпляра и занятое место квоты.

    Квота Low считается по каталогу целиком, как это делает сам журнал ловушки,
    поэтому в занятое место входят поколения всех экземпляров слота.
    """

    def __init__(
        self,
        directory: Path,
        binding: WriterBinding,
        reader: JournalReader | None = None,
    ) -> None:
        self.directory = Path(directory)
        self._binding = binding
        self._reader = reader if reader is not None else JournalReader()

    def entries(self) -> dict[int, Path]:
        """Поколения этого экземпляра по номеру; неожидаемое имя блокирует переход.

        Имя считается известным, только если совпадает с каноническим именем Low для
        своего номера: иначе два файла могли бы претендовать на одно поколение.
        """
        found: dict[int, Path] = {}
        prefix = f"low-{self._binding.instance_id}-".lower()
        # Отсутствие самого каталога отличается от исчезновения файла в нём: пустой
        # слот допустим, а пропажа поколения во время наблюдения — повод отказать.
        items = self._scandir()
        if items is None:
            return {}
        for item in items:
            match = _JOURNAL_NAME.fullmatch(item.name)
            if match is None or match.group(1) != self._binding.instance_id:
                if item.name.lower().startswith(prefix):
                    raise _unavailable("в каталоге слота неизвестное имя экземпляра")
                continue
            try:
                regular = item.is_file(follow_symlinks=False)
            except OSError as error:
                raise _unavailable("поколение журнала исчезло во время наблюдения") from error
            if not regular:
                raise _unavailable("поколение журнала не обычный файл")
            number = int(match.group(2))
            if item.name != journal_name(self._binding.instance_id, number):
                raise _unavailable("в каталоге слота неканоническое имя поколения")
            found[number] = Path(item.path)
        return found

    def directory_usage(self) -> int:
        """Суммарный размер всех поколений каталога: так же считает квоту сам Low."""
        total = 0
        items = self._scandir()
        if items is None:
            return 0
        for item in items:
            if _JOURNAL_NAME.fullmatch(item.name) is None:
                continue
            try:
                if not item.is_file(follow_symlinks=False):
                    continue
                total += os.stat(item.path, follow_symlinks=False).st_size
            except FileNotFoundError:
                # Сборщик удалил обработанный файл между чтением каталога и stat.
                continue
            except OSError as error:
                raise _unavailable("поколение журнала недоступно") from error
        return total

    def _scandir(self) -> list[os.DirEntry[str]] | None:
        """Перечисляет каталог слота целиком; None — каталога ещё нет.

        Отсутствие каталога распознаётся только на его открытии: ошибка во время
        перечисления означает, что каталог меняется под наблюдением, и это отказ,
        а не пустой слот.
        """
        try:
            iterator = os.scandir(self.directory)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise _unavailable("каталог слота недоступен") from error
        try:
            with iterator as items:
                return list(items)
        except OSError as error:
            raise _unavailable("каталог слота изменился во время наблюдения") from error

    def collect_writer_chain(
        self, first_journal_id: str, first_journal_seq: int
    ) -> tuple[JournalFile, ...]:
        """Все поколения закрываемого writer от его первого журнала до максимального номера.

        Цепочка проверяется без пропусков, повторов и развилок: номер в имени совпадает
        с номером заголовка, previous_journal_id указывает на предыдущее поколение этого
        writer, а привязка каждого заголовка совпадает с привязкой перехода. Поколения
        с номером меньше первого относятся к прежним writer и в набор не входят.
        """
        entries = self.entries()
        numbers = sorted(number for number in entries if number >= first_journal_seq)
        if not numbers or numbers[0] != first_journal_seq:
            raise _unavailable("первый журнал закрываемого writer отсутствует")
        if numbers != list(range(first_journal_seq, numbers[-1] + 1)):
            raise _unavailable("в цепочке журналов writer пропущен номер")
        collected: list[JournalFile] = []
        expected_previous: str | None = None
        seen: set[str] = set()
        for number in numbers:
            path = entries[number]
            header = self._reader.read_header(path)
            self._require_binding(header, number)
            if header.journal_id in seen:
                raise _unavailable("журналы writer повторяют journal_id")
            seen.add(header.journal_id)
            if number == first_journal_seq:
                if header.journal_id != str(UUID(first_journal_id)):
                    raise BoundaryError(
                        BoundaryErrorCode.BOUNDARY_BINDING_MISMATCH,
                        "первый журнал writer не совпадает с выделением",
                    )
                if header.previous_journal_id is not None:
                    raise _unavailable("первый журнал writer ссылается на предшественника")
            elif header.previous_journal_id != expected_previous:
                raise _unavailable("цепочка previous_journal_id разорвана")
            expected_previous = header.journal_id
            collected.append(
                JournalFile(
                    path=path,
                    header=header,
                    measurement=self._reader.measure(path, header.journal_id),
                )
            )
        return tuple(collected)

    def _require_binding(self, header: JournalHeader, number: int) -> None:
        """Заголовок чужой привязки или другого номера не относится к этому переходу."""
        if header.journal_seq != number:
            raise _unavailable("номер в имени не совпадает с заголовком")
        if (
            header.instance_id != self._binding.instance_id
            or header.deployment_id != self._binding.deployment_id
            or header.profile_revision != self._binding.profile_revision
        ):
            raise BoundaryError(
                BoundaryErrorCode.BOUNDARY_BINDING_MISMATCH,
                "заголовок журнала относится к другой привязке",
            )
