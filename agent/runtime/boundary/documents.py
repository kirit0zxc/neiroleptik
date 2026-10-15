"""Канонические документы границы процесса Low: anchor и proof (§4, §8.1 предложения).

Модуль отвечает только за форму и байты документов. Он не наблюдает Docker, не читает
каталог слота и не публикует файлы. Канонический вид документа создаётся один раз при
commit и далее только повторяется: повторная публикация никогда не собирает поля
заново (§8.4), поэтому разбор и сборка здесь строго обратимы.

Счётчики и размеры передаются как канонические десятичные строки, чтобы потребитель
на JavaScript не терял точность; profile_revision остаётся строгим integer, как
указано в таблице полей. В Python они живут обычными int: строковая форма появляется
только в payload.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from agent.runtime.boundary.errors import BoundaryError, BoundaryErrorCode

ANCHOR_SCHEMA = "honeyforge-low-writer-anchor/1"
BOUNDARY_SCHEMA = "honeyforge-low-process-boundary/1"
# Единственное признанное ограждение писателя: контейнер удалён по полному ID (§4).
FENCE_CONTAINER_REMOVED = "container_removed"
# Предел точного целого в JavaScript: выше документ не выдаётся (§4, §8.5).
MAX_SAFE_INTEGER = 9007199254740991
# Общий предел документа consumer; producer не фиксирует документ больше (§8.5).
DOCUMENT_MAX_BYTES = 1024 * 1024
# Пределы MVP на один переход (§8.5).
MAX_JOURNALS = 64
MAX_TAIL_BYTES = 65535
MAX_TOTAL_TAIL_BYTES = 4 * 1024 * 1024

_HEX64 = re.compile(r"[0-9a-f]{64}")
_DECIMAL = re.compile(r"0|[1-9][0-9]*")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?Z")
# Имя копии в защищённом snapshot-root: producer использует имя исходного файла Low.
# Точка в начале запрещена, такие имена collector не читает (§8.2).
_SNAPSHOT_REF = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,254}")


def _invalid(field: str) -> BoundaryError:
    """Причина содержит только имя поля: значение может быть байтами журнала."""
    return BoundaryError(BoundaryErrorCode.INVALID_DOCUMENT, f"недопустимое поле: {field}")


def utc_timestamp(moment: datetime) -> str:
    """RFC3339 UTC Z; только диагностика, причинность задают счётчики и история (§4)."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise _invalid("timestamp")
    text = moment.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if _TIMESTAMP.fullmatch(text) is None:
        raise _invalid("timestamp")
    return text


def _reject_constant(name: str) -> object:
    """JSON-константы NaN, Infinity и -Infinity в каноническом документе запрещены."""
    raise _invalid("document")


class CanonicalJson:
    """Канонический UTF-8 JSON без BOM и перевода строки: один документ — один вид байт."""

    @staticmethod
    def encode(payload: Mapping[str, object]) -> bytes:
        """sort_keys, ensure_ascii=False, компактные разделители, запрет NaN/Infinity (§4)."""
        try:
            text = json.dumps(
                payload,
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as error:
            raise _invalid("payload") from error
        return text.encode("utf-8")

    @staticmethod
    def decode(data: bytes) -> dict[str, object]:
        """Разбирает только канонические байты: иной отступ или порядок ключей отвергается.

        Проверка выполняется сравнением с повторной канонизацией, поэтому дубли ключей,
        BOM, перевод строки, NaN/Infinity и нестандартная запись чисел исключаются сразу.
        """
        if not isinstance(data, (bytes, bytearray)):
            raise _invalid("document")
        raw = bytes(data)
        if len(raw) > DOCUMENT_MAX_BYTES:
            raise BoundaryError(BoundaryErrorCode.LIMIT_EXCEEDED, "документ больше предела")
        if raw.startswith(b"\xef\xbb\xbf") or b"\n" in raw or b"\r" in raw:
            raise _invalid("document")
        try:
            value = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
        except (UnicodeDecodeError, ValueError) as error:
            raise _invalid("document") from error
        if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
            raise _invalid("document")
        payload = cast(dict[str, object], value)
        if CanonicalJson.encode(payload) != raw:
            raise _invalid("document")
        return payload


def _keys(value: object, expected: set[str], path: str) -> dict[str, object]:
    """Объект без неизвестных и без отсутствующих ключей, включая nullable (§4)."""
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise _invalid(path)
    payload = cast(dict[str, object], value)
    if set(payload) != expected:
        raise _invalid(path)
    return payload


def _uuid(value: object, path: str) -> str:
    """Canonical lowercase UUID; иная запись того же числа документом не принимается."""
    if not isinstance(value, str):
        raise _invalid(path)
    try:
        canonical = str(UUID(value))
    except (ValueError, AttributeError):
        raise _invalid(path) from None
    if canonical != value:
        raise _invalid(path)
    return canonical


def _hex64(value: object, path: str) -> str:
    if not isinstance(value, str) or _HEX64.fullmatch(value) is None:
        raise _invalid(path)
    return value


def _const(value: object, expected: str, path: str) -> str:
    if value != expected:
        raise _invalid(path)
    return expected


def _boolean(value: object, path: str) -> bool:
    if type(value) is not bool:
        raise _invalid(path)
    return value


def _revision(value: object, path: str) -> int:
    """Строгий integer 1..MAX_SAFE_INTEGER: исторически применённая revision (§4)."""
    if type(value) is not int or not 1 <= value <= MAX_SAFE_INTEGER:
        raise _invalid(path)
    return value


def _timestamp(value: object, path: str) -> str:
    if not isinstance(value, str) or _TIMESTAMP.fullmatch(value) is None:
        raise _invalid(path)
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise _invalid(path) from None
    return value


def counter(value: int, path: str, *, minimum: int = 1) -> str:
    """Проверяет счётчик и возвращает каноническую десятичную строку для payload."""
    if type(value) is not int or not minimum <= value <= MAX_SAFE_INTEGER:
        raise _invalid(path)
    return str(value)


def _counter_value(value: object, path: str, *, minimum: int = 1) -> int:
    """Читает десятичную строку без ведущих нулей обратно в int."""
    if not isinstance(value, str) or _DECIMAL.fullmatch(value) is None:
        raise _invalid(path)
    number = int(value)
    if not minimum <= number <= MAX_SAFE_INTEGER:
        raise _invalid(path)
    return number


@dataclass(frozen=True)
class WriterBinding:
    """Доверенная привязка перехода; у обоих writer одного instance она одна (§4)."""

    agent_id: str
    deployment_id: str
    instance_id: str
    profile_revision: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "agent_id", _uuid(self.agent_id, "agent_id"))
        object.__setattr__(self, "deployment_id", _uuid(self.deployment_id, "deployment_id"))
        object.__setattr__(self, "instance_id", _uuid(self.instance_id, "instance_id"))
        _revision(self.profile_revision, "profile_revision")

    def payload(self) -> dict[str, object]:
        """Поля привязки документа; profile_revision остаётся integer, не строкой."""
        return {
            "agent_id": self.agent_id,
            "deployment_id": self.deployment_id,
            "instance_id": self.instance_id,
            "profile_revision": self.profile_revision,
        }

    @classmethod
    def read(cls, payload: Mapping[str, object]) -> WriterBinding:
        """Читает привязку из уже проверенного на состав ключей объекта документа."""
        return cls(
            _uuid(payload["agent_id"], "agent_id"),
            _uuid(payload["deployment_id"], "deployment_id"),
            _uuid(payload["instance_id"], "instance_id"),
            _revision(payload["profile_revision"], "profile_revision"),
        )


@dataclass(frozen=True)
class FirstJournal:
    """Первое поколение цепочки writer: previous всегда null, process seq начинается с 1."""

    journal_id: str
    journal_seq: int
    header_sha256: str
    first_seq: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "journal_id", _uuid(self.journal_id, "first_journal.journal_id"))
        object.__setattr__(
            self, "header_sha256", _hex64(self.header_sha256, "first_journal.header_sha256")
        )
        counter(self.journal_seq, "first_journal.journal_seq")
        if self.first_seq != 1:
            raise _invalid("first_journal.first_seq")

    def payload(self) -> dict[str, object]:
        return {
            "journal_id": self.journal_id,
            "journal_seq": counter(self.journal_seq, "first_journal.journal_seq"),
            "previous_journal_id": None,
            "first_seq": counter(self.first_seq, "first_journal.first_seq"),
            "header_sha256": self.header_sha256,
        }

    @classmethod
    def parse(cls, value: object, path: str) -> FirstJournal:
        """Читает поколение документа; previous_journal_id должен быть именно null."""
        payload = _keys(
            value,
            {"journal_id", "journal_seq", "previous_journal_id", "first_seq", "header_sha256"},
            path,
        )
        if payload["previous_journal_id"] is not None:
            raise _invalid(path + ".previous_journal_id")
        return cls(
            _uuid(payload["journal_id"], path + ".journal_id"),
            _counter_value(payload["journal_seq"], path + ".journal_seq"),
            _hex64(payload["header_sha256"], path + ".header_sha256"),
            _counter_value(payload["first_seq"], path + ".first_seq"),
        )


@dataclass(frozen=True)
class SnapshotEntry:
    """Закреплённая копия одного журнала закрываемого writer (§4, §8.3)."""

    journal_id: str
    journal_seq: int
    snapshot_ref: str
    size_bytes: int
    sha256: str
    complete_lf_end: int
    footer_present: bool

    def __post_init__(self) -> None:
        path = "previous_journals[]"
        object.__setattr__(self, "journal_id", _uuid(self.journal_id, path + ".journal_id"))
        object.__setattr__(self, "sha256", _hex64(self.sha256, path + ".sha256"))
        counter(self.journal_seq, path + ".journal_seq")
        counter(self.size_bytes, path + ".size_bytes", minimum=0)
        counter(self.complete_lf_end, path + ".complete_lf_end", minimum=0)
        _boolean(self.footer_present, path + ".footer_present")
        if (
            not isinstance(self.snapshot_ref, str)
            or _SNAPSHOT_REF.fullmatch(self.snapshot_ref) is None
            or ".." in self.snapshot_ref
        ):
            raise _invalid(path + ".snapshot_ref")
        if self.complete_lf_end > self.size_bytes:
            raise _invalid(path + ".complete_lf_end")

    @property
    def tail_bytes(self) -> int:
        """Длина неполного хвоста копии; хвост бывает только в конце файла (§8.5)."""
        return self.size_bytes - self.complete_lf_end

    def payload(self) -> dict[str, object]:
        path = "previous_journals[]"
        return {
            "journal_id": self.journal_id,
            "journal_seq": counter(self.journal_seq, path + ".journal_seq"),
            "snapshot_ref": self.snapshot_ref,
            "size_bytes": counter(self.size_bytes, path + ".size_bytes", minimum=0),
            "sha256": self.sha256,
            "complete_lf_end": counter(self.complete_lf_end, path + ".complete_lf_end", minimum=0),
            "footer_present": self.footer_present,
        }

    @classmethod
    def parse(cls, value: object, path: str) -> SnapshotEntry:
        """Читает элемент previous_journals; размеры приходят десятичными строками."""
        payload = _keys(
            value,
            {
                "journal_id",
                "journal_seq",
                "snapshot_ref",
                "size_bytes",
                "sha256",
                "complete_lf_end",
                "footer_present",
            },
            path,
        )
        reference = payload["snapshot_ref"]
        if not isinstance(reference, str):
            raise _invalid(path + ".snapshot_ref")
        return cls(
            _uuid(payload["journal_id"], path + ".journal_id"),
            _counter_value(payload["journal_seq"], path + ".journal_seq"),
            reference,
            _counter_value(payload["size_bytes"], path + ".size_bytes", minimum=0),
            _hex64(payload["sha256"], path + ".sha256"),
            _counter_value(payload["complete_lf_end"], path + ".complete_lf_end", minimum=0),
            _boolean(payload["footer_present"], path + ".footer_present"),
        )


@dataclass(frozen=True)
class PreviousWriter:
    """Закрытый writer: полный ID удалённого контейнера и время ограждения (§4)."""

    writer_id: str
    container_id: str
    fenced_at: str
    fence: str = FENCE_CONTAINER_REMOVED

    def __post_init__(self) -> None:
        path = "previous_writer"
        object.__setattr__(self, "writer_id", _uuid(self.writer_id, path + ".writer_id"))
        object.__setattr__(self, "container_id", _hex64(self.container_id, path + ".container_id"))
        object.__setattr__(self, "fenced_at", _timestamp(self.fenced_at, path + ".fenced_at"))
        _const(self.fence, FENCE_CONTAINER_REMOVED, path + ".fence")

    def payload(self) -> dict[str, object]:
        return {
            "writer_id": self.writer_id,
            "container_id": self.container_id,
            "fence": self.fence,
            "fenced_at": self.fenced_at,
        }

    @classmethod
    def parse(cls, value: object, path: str) -> PreviousWriter:
        """Читает закрытого writer; признаётся только ограждение container_removed."""
        payload = _keys(value, {"writer_id", "container_id", "fence", "fenced_at"}, path)
        return cls(
            _uuid(payload["writer_id"], path + ".writer_id"),
            _hex64(payload["container_id"], path + ".container_id"),
            _timestamp(payload["fenced_at"], path + ".fenced_at"),
            _const(payload["fence"], FENCE_CONTAINER_REMOVED, path + ".fence"),
        )


@dataclass(frozen=True)
class NextWriter:
    """Другой writer_id и другой полный container_id, привязанные к launch intent (§4)."""

    writer_id: str
    container_id: str
    first_journal: FirstJournal

    def __post_init__(self) -> None:
        path = "next_writer"
        object.__setattr__(self, "writer_id", _uuid(self.writer_id, path + ".writer_id"))
        object.__setattr__(self, "container_id", _hex64(self.container_id, path + ".container_id"))
        if not isinstance(self.first_journal, FirstJournal):
            raise _invalid(path + ".first_journal")

    def payload(self) -> dict[str, object]:
        return {
            "writer_id": self.writer_id,
            "container_id": self.container_id,
            "first_journal": self.first_journal.payload(),
        }

    @classmethod
    def parse(cls, value: object, path: str) -> NextWriter:
        """Читает нового writer вместе с его первым поколением."""
        payload = _keys(value, {"writer_id", "container_id", "first_journal"}, path)
        return cls(
            _uuid(payload["writer_id"], path + ".writer_id"),
            _hex64(payload["container_id"], path + ".container_id"),
            FirstJournal.parse(payload["first_journal"], path + ".first_journal"),
        )


@dataclass(frozen=True)
class WriterAnchor:
    """Начальная доверенная привязка writer нового instance (§8.1).

    Anchor выдаётся только при первом запуске instance и не доказывает ни ограждения,
    ни снимков: он задаёт точку, от которой проверяется первый переход.
    """

    binding: WriterBinding
    anchor_id: str
    publisher_epoch: str
    writer_id: str
    container_id: str
    launch_intent_id: str
    first_journal: FirstJournal
    committed_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.binding, WriterBinding):
            raise _invalid("binding")
        object.__setattr__(self, "anchor_id", _uuid(self.anchor_id, "anchor_id"))
        object.__setattr__(self, "publisher_epoch", _uuid(self.publisher_epoch, "publisher_epoch"))
        object.__setattr__(self, "writer_id", _uuid(self.writer_id, "writer_id"))
        object.__setattr__(self, "container_id", _hex64(self.container_id, "container_id"))
        object.__setattr__(
            self, "launch_intent_id", _uuid(self.launch_intent_id, "launch_intent_id")
        )
        object.__setattr__(self, "committed_at", _timestamp(self.committed_at, "committed_at"))
        if not isinstance(self.first_journal, FirstJournal):
            raise _invalid("first_journal")
        # Для нового instance ожидаются journal_seq=1, previous=null и first_seq=1 (§8.1).
        if self.first_journal.journal_seq != 1:
            raise _invalid("first_journal.journal_seq")

    def payload(self) -> dict[str, object]:
        """Полный объект документа; состав ключей совпадает с таблицей §8.1."""
        return {
            "schema": ANCHOR_SCHEMA,
            "anchor_id": self.anchor_id,
            "publisher_epoch": self.publisher_epoch,
            **self.binding.payload(),
            "writer_id": self.writer_id,
            "container_id": self.container_id,
            "launch_intent_id": self.launch_intent_id,
            "first_journal": self.first_journal.payload(),
            "committed_at": self.committed_at,
        }

    def canonical(self) -> bytes:
        """Канонические байты документа; предел §8.5 проверяется до выдачи."""
        data = CanonicalJson.encode(self.payload())
        if len(data) > DOCUMENT_MAX_BYTES:
            raise BoundaryError(BoundaryErrorCode.LIMIT_EXCEEDED, "anchor больше предела")
        return data

    @classmethod
    def parse(cls, data: bytes) -> WriterAnchor:
        """Строгая проверка anchor по каноническим байтам (та же, что у consumer)."""
        payload = CanonicalJson.decode(data)
        _keys(
            payload,
            {
                "schema",
                "anchor_id",
                "publisher_epoch",
                "agent_id",
                "deployment_id",
                "instance_id",
                "profile_revision",
                "writer_id",
                "container_id",
                "launch_intent_id",
                "first_journal",
                "committed_at",
            },
            "anchor",
        )
        _const(payload["schema"], ANCHOR_SCHEMA, "anchor.schema")
        return cls(
            WriterBinding.read(payload),
            _uuid(payload["anchor_id"], "anchor.anchor_id"),
            _uuid(payload["publisher_epoch"], "anchor.publisher_epoch"),
            _uuid(payload["writer_id"], "anchor.writer_id"),
            _hex64(payload["container_id"], "anchor.container_id"),
            _uuid(payload["launch_intent_id"], "anchor.launch_intent_id"),
            FirstJournal.parse(payload["first_journal"], "anchor.first_journal"),
            _timestamp(payload["committed_at"], "anchor.committed_at"),
        )


@dataclass(frozen=True)
class ProcessBoundary:
    """Доказательство одного перехода writer внутри прежнего instance (§4).

    Документ связывает закрытого writer, закреплённые копии его журналов и новую
    привязанную цепочку. Он не доказывает полноту сетевого захвата и не разрешает
    cleanup: это отдельные решения потребителя и оператора.
    """

    binding: WriterBinding
    boundary_id: str
    boundary_seq: int
    previous_boundary_id: str | None
    publisher_epoch: str
    previous_writer: PreviousWriter
    previous_journals: tuple[SnapshotEntry, ...]
    next_writer: NextWriter
    committed_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.binding, WriterBinding):
            raise _invalid("binding")
        object.__setattr__(self, "boundary_id", _uuid(self.boundary_id, "boundary_id"))
        object.__setattr__(self, "publisher_epoch", _uuid(self.publisher_epoch, "publisher_epoch"))
        object.__setattr__(self, "committed_at", _timestamp(self.committed_at, "committed_at"))
        counter(self.boundary_seq, "boundary_seq")
        if self.previous_boundary_id is not None:
            object.__setattr__(
                self,
                "previous_boundary_id",
                _uuid(self.previous_boundary_id, "previous_boundary_id"),
            )
        # Первый переход не имеет предшественника, остальные обязаны его называть (§5).
        if (self.previous_boundary_id is None) != (self.boundary_seq == 1):
            raise _invalid("previous_boundary_id")
        if self.previous_boundary_id == self.boundary_id:
            raise _invalid("previous_boundary_id")
        if not isinstance(self.previous_writer, PreviousWriter) or not isinstance(
            self.next_writer, NextWriter
        ):
            raise _invalid("writer")
        object.__setattr__(self, "previous_journals", tuple(self.previous_journals))
        self._check_journals()
        self._check_writers()

    def _check_journals(self) -> None:
        """Непустой массив всех журналов закрываемого writer в порядке journal_seq (§4)."""
        journals = self.previous_journals
        if not journals or any(not isinstance(item, SnapshotEntry) for item in journals):
            raise _invalid("previous_journals")
        if len(journals) > MAX_JOURNALS:
            raise BoundaryError(BoundaryErrorCode.LIMIT_EXCEEDED, "больше 64 журналов на переход")
        sequences = [item.journal_seq for item in journals]
        if sequences != sorted(set(sequences)):
            raise _invalid("previous_journals[].journal_seq")
        if len({item.journal_id for item in journals}) != len(journals):
            raise _invalid("previous_journals[].journal_id")
        if len({item.snapshot_ref for item in journals}) != len(journals):
            raise _invalid("previous_journals[].snapshot_ref")
        tail = sum(item.tail_bytes for item in journals)
        if any(item.tail_bytes > MAX_TAIL_BYTES for item in journals) or (
            tail > MAX_TOTAL_TAIL_BYTES
        ):
            raise BoundaryError(BoundaryErrorCode.LIMIT_EXCEEDED, "неполный хвост больше предела")

    def _check_writers(self) -> None:
        """Два разных writer одного instance; номер новой цепочки продолжает нумерацию."""
        if (
            self.previous_writer.writer_id == self.next_writer.writer_id
            or self.previous_writer.container_id == self.next_writer.container_id
        ):
            raise _invalid("next_writer")
        first = self.next_writer.first_journal
        # Для первого MVP требуется last previous journal_seq + 1 (§4).
        if first.journal_seq != self.previous_journals[-1].journal_seq + 1:
            raise _invalid("next_writer.first_journal.journal_seq")
        if first.journal_id in {item.journal_id for item in self.previous_journals}:
            raise _invalid("next_writer.first_journal.journal_id")

    def payload(self) -> dict[str, object]:
        """Полный объект документа; состав ключей совпадает с таблицей §4."""
        return {
            "schema": BOUNDARY_SCHEMA,
            "boundary_id": self.boundary_id,
            "boundary_seq": counter(self.boundary_seq, "boundary_seq"),
            "previous_boundary_id": self.previous_boundary_id,
            "publisher_epoch": self.publisher_epoch,
            **self.binding.payload(),
            "previous_writer": self.previous_writer.payload(),
            "previous_journals": [item.payload() for item in self.previous_journals],
            "next_writer": self.next_writer.payload(),
            "committed_at": self.committed_at,
        }

    def canonical(self) -> bytes:
        """Канонические байты документа; предел §8.5 проверяется до выдачи."""
        data = CanonicalJson.encode(self.payload())
        if len(data) > DOCUMENT_MAX_BYTES:
            raise BoundaryError(BoundaryErrorCode.LIMIT_EXCEEDED, "proof больше предела")
        return data

    @classmethod
    def parse(cls, data: bytes) -> ProcessBoundary:
        """Строгая проверка proof по каноническим байтам (та же, что у consumer)."""
        payload = CanonicalJson.decode(data)
        _keys(
            payload,
            {
                "schema",
                "boundary_id",
                "boundary_seq",
                "previous_boundary_id",
                "publisher_epoch",
                "agent_id",
                "deployment_id",
                "instance_id",
                "profile_revision",
                "previous_writer",
                "previous_journals",
                "next_writer",
                "committed_at",
            },
            "boundary",
        )
        _const(payload["schema"], BOUNDARY_SCHEMA, "boundary.schema")
        raw_journals = payload["previous_journals"]
        if not isinstance(raw_journals, list):
            raise _invalid("boundary.previous_journals")
        journals = tuple(
            SnapshotEntry.parse(item, f"boundary.previous_journals[{index}]")
            for index, item in enumerate(raw_journals)
        )
        previous_boundary = payload["previous_boundary_id"]
        return cls(
            WriterBinding.read(payload),
            _uuid(payload["boundary_id"], "boundary.boundary_id"),
            _counter_value(payload["boundary_seq"], "boundary.boundary_seq"),
            None
            if previous_boundary is None
            else _uuid(previous_boundary, "boundary.previous_boundary_id"),
            _uuid(payload["publisher_epoch"], "boundary.publisher_epoch"),
            PreviousWriter.parse(payload["previous_writer"], "boundary.previous_writer"),
            journals,
            NextWriter.parse(payload["next_writer"], "boundary.next_writer"),
            _timestamp(payload["committed_at"], "boundary.committed_at"),
        )
