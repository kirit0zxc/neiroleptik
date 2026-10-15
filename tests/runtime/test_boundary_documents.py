"""Форма и канонические байты anchor и proof границы процесса Low (§4, §8.1).

Опорой служат фикстуры репозитория: их канонический размер и SHA-256 зафиксированы
в manifest.json, поэтому любое изменение правил канонизации обнаруживается сразу.
Фикстура остаётся примером формы: доверенным runtime-документом она не является.
"""

import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent.runtime.boundary.documents import (
    ANCHOR_SCHEMA,
    BOUNDARY_SCHEMA,
    DOCUMENT_MAX_BYTES,
    MAX_SAFE_INTEGER,
    CanonicalJson,
    FirstJournal,
    NextWriter,
    PreviousWriter,
    ProcessBoundary,
    SnapshotEntry,
    WriterAnchor,
    WriterBinding,
    utc_timestamp,
)
from agent.runtime.boundary.errors import BoundaryError, BoundaryErrorCode

SAMPLES = Path(__file__).resolve().parents[2] / "specs/samples/low-process-boundary"


def manifest() -> dict[str, object]:
    """Происхождение и контрольные значения фикстур из manifest.json."""
    return json.loads((SAMPLES / "manifest.json").read_text(encoding="utf-8"))


def sample(name: str) -> dict[str, object]:
    """Читает пример документа; отступы примера новый переход не создают."""
    return json.loads((SAMPLES / name).read_text(encoding="utf-8"))


def anchor_payload() -> dict[str, object]:
    return sample("anchor-example.json")


def boundary_payload() -> dict[str, object]:
    return sample("boundary-example.json")


def binding(payload: dict[str, object]) -> WriterBinding:
    return WriterBinding(
        str(payload["agent_id"]),
        str(payload["deployment_id"]),
        str(payload["instance_id"]),
        int(str(payload["profile_revision"])),
    )


def built_boundary() -> ProcessBoundary:
    """Собирает тот же переход из полей, а не из разбора готового документа."""
    payload = boundary_payload()
    previous = payload["previous_writer"]
    assert isinstance(previous, dict)
    journals = payload["previous_journals"]
    assert isinstance(journals, list)
    following = payload["next_writer"]
    assert isinstance(following, dict)
    first = following["first_journal"]
    assert isinstance(first, dict)
    return ProcessBoundary(
        binding=binding(payload),
        boundary_id=str(payload["boundary_id"]),
        boundary_seq=int(str(payload["boundary_seq"])),
        previous_boundary_id=payload["previous_boundary_id"],
        publisher_epoch=str(payload["publisher_epoch"]),
        previous_writer=PreviousWriter(
            writer_id=str(previous["writer_id"]),
            container_id=str(previous["container_id"]),
            fenced_at=str(previous["fenced_at"]),
        ),
        previous_journals=tuple(
            SnapshotEntry(
                journal_id=str(item["journal_id"]),
                journal_seq=int(item["journal_seq"]),
                snapshot_ref=str(item["snapshot_ref"]),
                size_bytes=int(item["size_bytes"]),
                sha256=str(item["sha256"]),
                complete_lf_end=int(item["complete_lf_end"]),
                footer_present=bool(item["footer_present"]),
            )
            for item in journals
        ),
        next_writer=NextWriter(
            writer_id=str(following["writer_id"]),
            container_id=str(following["container_id"]),
            first_journal=FirstJournal(
                journal_id=str(first["journal_id"]),
                journal_seq=int(str(first["journal_seq"])),
                header_sha256=str(first["header_sha256"]),
            ),
        ),
        committed_at=str(payload["committed_at"]),
    )


def built_anchor() -> WriterAnchor:
    """Собирает тот же anchor из полей примера."""
    payload = anchor_payload()
    first = payload["first_journal"]
    assert isinstance(first, dict)
    return WriterAnchor(
        binding=binding(payload),
        anchor_id=str(payload["anchor_id"]),
        publisher_epoch=str(payload["publisher_epoch"]),
        writer_id=str(payload["writer_id"]),
        container_id=str(payload["container_id"]),
        launch_intent_id=str(payload["launch_intent_id"]),
        first_journal=FirstJournal(
            journal_id=str(first["journal_id"]),
            journal_seq=int(str(first["journal_seq"])),
            header_sha256=str(first["header_sha256"]),
        ),
        committed_at=str(payload["committed_at"]),
    )


def test_samples_match_documented_canonical_bytes_and_hashes() -> None:
    """Канонические размер и SHA-256 обоих примеров совпадают с manifest.json."""
    import hashlib

    data = manifest()
    anchor_note = data["anchor_example"]
    proof_note = data["proof_example"]
    assert isinstance(anchor_note, dict) and isinstance(proof_note, dict)
    for note, document in (
        (anchor_note, built_anchor().canonical()),
        (proof_note, built_boundary().canonical()),
    ):
        assert len(document) == int(str(note["canonical_bytes"]))
        assert hashlib.sha256(document).hexdigest() == note["canonical_sha256"]


def test_documents_round_trip_through_strict_parsing() -> None:
    """Разбор канонических байт возвращает те же поля и те же байты."""
    for built in (built_anchor(), built_boundary()):
        data = built.canonical()
        parsed = type(built).parse(data)
        assert parsed == built
        assert parsed.canonical() == data


def test_schema_and_binding_are_shared_by_both_documents() -> None:
    """У anchor и proof одного instance одна привязка и свои постоянные schema."""
    anchor, boundary = built_anchor(), built_boundary()
    assert anchor.payload()["schema"] == ANCHOR_SCHEMA
    assert boundary.payload()["schema"] == BOUNDARY_SCHEMA
    assert anchor.binding == boundary.binding
    # Первый переход начинается именно от anchor: writer и первый журнал совпадают.
    assert boundary.previous_writer.writer_id == anchor.writer_id
    assert boundary.previous_writer.container_id == anchor.container_id
    assert boundary.previous_journals[0].journal_id == anchor.first_journal.journal_id


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda raw: b"\xef\xbb\xbf" + raw, id="bom"),
        pytest.param(lambda raw: raw + b"\n", id="trailing-lf"),
        pytest.param(lambda raw: raw.replace(b'{"', b'{ "', 1), id="space"),
        pytest.param(lambda raw: raw[:-1], id="truncated"),
    ],
)
def test_non_canonical_bytes_are_refused(mutate: object) -> None:
    """Повторы и публикация сравнивают именно канонические байты документа."""
    assert callable(mutate)
    raw = built_boundary().canonical()
    with pytest.raises(BoundaryError) as error:
        ProcessBoundary.parse(mutate(raw))
    assert error.value.code is BoundaryErrorCode.INVALID_DOCUMENT


def test_duplicate_keys_and_not_a_number_are_refused() -> None:
    """Дубль ключа и NaN в документе недопустимы даже при корректном остальном составе."""
    raw = built_anchor().canonical()
    duplicated = raw.replace(b'"schema":', b'"schema":"x","schema":', 1)
    with pytest.raises(BoundaryError):
        WriterAnchor.parse(duplicated)
    with pytest.raises(BoundaryError):
        WriterAnchor.parse(raw.replace(b'"profile_revision":1', b'"profile_revision":NaN', 1))


def test_indented_sample_is_not_a_document_but_its_fields_are() -> None:
    """Пример в samples форматирован для чтения: его байты документом не считаются."""
    indented = (SAMPLES / "boundary-example.json").read_bytes()
    with pytest.raises(BoundaryError):
        ProcessBoundary.parse(indented)
    assert ProcessBoundary.parse(CanonicalJson.encode(boundary_payload())) == built_boundary()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("boundary_id", "D6E623C9-425A-4CE4-9B31-24A8AD509283"),
        ("boundary_seq", 1),
        ("boundary_seq", "01"),
        ("profile_revision", "1"),
        ("publisher_epoch", "not-a-uuid"),
        ("committed_at", "2026-10-14T10:00:00+00:00"),
        ("committed_at", "2026-10-14 10:00:00Z"),
    ],
)
def test_strict_field_forms_are_required(field: str, value: object) -> None:
    """UUID в lowercase, счётчики десятичными строками, время RFC3339 UTC Z."""
    payload = boundary_payload()
    payload[field] = value
    with pytest.raises(BoundaryError) as error:
        ProcessBoundary.parse(CanonicalJson.encode(payload))
    assert error.value.code is BoundaryErrorCode.INVALID_DOCUMENT


def test_unknown_and_missing_keys_are_refused() -> None:
    """Объект без неизвестных ключей и без отсутствующих, включая nullable (§4)."""
    extra = boundary_payload()
    extra["writer_notes"] = "x"
    with pytest.raises(BoundaryError):
        ProcessBoundary.parse(CanonicalJson.encode(extra))
    missing = boundary_payload()
    del missing["previous_boundary_id"]
    with pytest.raises(BoundaryError):
        ProcessBoundary.parse(CanonicalJson.encode(missing))


def test_first_boundary_has_no_predecessor_and_later_ones_require_it() -> None:
    """previous_boundary_id равен null ровно для первого перехода (§5)."""
    first = built_boundary()
    with pytest.raises(BoundaryError):
        copy_with(first, previous_boundary_id=first.boundary_id)
    with pytest.raises(BoundaryError):
        copy_with(first, boundary_seq=2)
    later = copy_with(first, boundary_seq=2, previous_boundary_id=anchor_payload()["anchor_id"])
    assert later.boundary_seq == 2


def copy_with(boundary: ProcessBoundary, **changes: object) -> ProcessBoundary:
    """Пересобирает переход с изменённым полем; проверки выполняет сам класс."""
    import dataclasses

    return dataclasses.replace(boundary, **changes)


def test_next_writer_must_differ_and_continue_the_numbering() -> None:
    """Два разных writer; первый журнал новой цепочки — последний previous плюс один."""
    boundary = built_boundary()
    with pytest.raises(BoundaryError):
        copy_with(
            boundary,
            next_writer=NextWriter(
                boundary.previous_writer.writer_id,
                boundary.next_writer.container_id,
                boundary.next_writer.first_journal,
            ),
        )
    with pytest.raises(BoundaryError):
        copy_with(
            boundary,
            next_writer=NextWriter(
                boundary.next_writer.writer_id,
                boundary.previous_writer.container_id,
                boundary.next_writer.first_journal,
            ),
        )
    gap = FirstJournal(
        boundary.next_writer.first_journal.journal_id,
        boundary.previous_journals[-1].journal_seq + 2,
        boundary.next_writer.first_journal.header_sha256,
    )
    with pytest.raises(BoundaryError):
        copy_with(
            boundary,
            next_writer=NextWriter(
                boundary.next_writer.writer_id, boundary.next_writer.container_id, gap
            ),
        )


def test_new_chain_cannot_reuse_a_closed_journal_identity() -> None:
    """journal_id новой цепочки не повторяет закреплённые журналы прежнего writer."""
    boundary = built_boundary()
    reused = FirstJournal(
        boundary.previous_journals[0].journal_id,
        boundary.next_writer.first_journal.journal_seq,
        boundary.next_writer.first_journal.header_sha256,
    )
    with pytest.raises(BoundaryError):
        copy_with(
            boundary,
            next_writer=NextWriter(
                boundary.next_writer.writer_id, boundary.next_writer.container_id, reused
            ),
        )


def entry(seq: int, *, size: int = 885, lf_end: int | None = None) -> SnapshotEntry:
    """Элемент перечня закреплённых копий с рабочими значениями фикстуры."""
    journals = boundary_payload()["previous_journals"]
    assert isinstance(journals, list)
    source = copy.deepcopy(journals[0])
    return SnapshotEntry(
        journal_id=f"61c4c12f-bcd3-45eb-8498-d04f84d6cc{seq:02d}",
        journal_seq=seq,
        snapshot_ref=f"low-instance-{seq:06d}.jsonl",
        size_bytes=size,
        sha256=str(source["sha256"]),
        complete_lf_end=size if lf_end is None else lf_end,
        footer_present=False,
    )


def test_previous_journals_must_be_a_non_empty_ordered_set() -> None:
    """Непустой массив в порядке journal_seq без повторов journal_id и snapshot_ref."""
    boundary = built_boundary()
    with pytest.raises(BoundaryError):
        copy_with(boundary, previous_journals=())
    with pytest.raises(BoundaryError):
        copy_with(
            boundary,
            previous_journals=(entry(2), entry(1)),
            next_writer=NextWriter(
                boundary.next_writer.writer_id,
                boundary.next_writer.container_id,
                FirstJournal(
                    boundary.next_writer.first_journal.journal_id,
                    2,
                    boundary.next_writer.first_journal.header_sha256,
                ),
            ),
        )


def test_journal_and_tail_limits_are_refused_as_limit_exceeded() -> None:
    """Больше 64 журналов и хвост больше 65535 байт — LIMIT_EXCEEDED (§8.5)."""
    boundary = built_boundary()
    many = tuple(entry(number) for number in range(1, 66))
    with pytest.raises(BoundaryError) as error:
        copy_with(
            boundary,
            previous_journals=many,
            next_writer=NextWriter(
                boundary.next_writer.writer_id,
                boundary.next_writer.container_id,
                FirstJournal(
                    boundary.next_writer.first_journal.journal_id,
                    66,
                    boundary.next_writer.first_journal.header_sha256,
                ),
            ),
        )
    assert error.value.code is BoundaryErrorCode.LIMIT_EXCEEDED
    long_tail = entry(1, size=70000, lf_end=0)
    with pytest.raises(BoundaryError) as error:
        copy_with(boundary, previous_journals=(long_tail,))
    assert error.value.code is BoundaryErrorCode.LIMIT_EXCEEDED


def test_complete_lf_end_cannot_exceed_the_size_of_the_copy() -> None:
    """Граница полных строк не может быть дальше конца файла."""
    with pytest.raises(BoundaryError):
        entry(1, size=885, lf_end=886)


@pytest.mark.parametrize(
    "reference",
    ["", "a/b.jsonl", "..", "../escape.jsonl", ".hidden.jsonl", "back\\slash.jsonl"],
)
def test_snapshot_reference_is_a_single_safe_name(reference: str) -> None:
    """snapshot_ref — basename без разделителей, «..» и ведущей точки (§8.2)."""
    with pytest.raises(BoundaryError):
        SnapshotEntry(
            journal_id="61c4c12f-bcd3-45eb-8498-d04f84d6cc75",
            journal_seq=1,
            snapshot_ref=reference,
            size_bytes=1,
            sha256="0" * 64,
            complete_lf_end=0,
            footer_present=False,
        )


def test_only_container_removed_counts_as_fencing() -> None:
    """Иное значение fence документом не принимается (§4)."""
    payload = boundary_payload()
    previous = payload["previous_writer"]
    assert isinstance(previous, dict)
    previous["fence"] = "process_exited"
    with pytest.raises(BoundaryError):
        ProcessBoundary.parse(CanonicalJson.encode(payload))


def test_anchor_requires_the_first_generation_of_a_new_instance() -> None:
    """У anchor journal_seq=1, previous=null и first_seq=1 (§8.1)."""
    anchor = built_anchor()
    with pytest.raises(BoundaryError):
        FirstJournal(anchor.first_journal.journal_id, 1, anchor.first_journal.header_sha256, 2)
    payload = anchor_payload()
    first = payload["first_journal"]
    assert isinstance(first, dict)
    first["journal_seq"] = "2"
    with pytest.raises(BoundaryError):
        WriterAnchor.parse(CanonicalJson.encode(payload))
    restored = anchor_payload()
    nested = restored["first_journal"]
    assert isinstance(nested, dict)
    nested["previous_journal_id"] = anchor.first_journal.journal_id
    with pytest.raises(BoundaryError):
        WriterAnchor.parse(CanonicalJson.encode(restored))


def test_counters_stay_inside_the_safe_integer_range() -> None:
    """Счётчик больше 9007199254740991 документом не выдаётся (§4)."""
    boundary = built_boundary()
    with pytest.raises(BoundaryError):
        copy_with(boundary, boundary_seq=MAX_SAFE_INTEGER + 1, previous_boundary_id=None)


def test_overlong_counter_is_refused_as_a_field_not_as_a_crash() -> None:
    """Очень длинная строка цифр отвергается до разбора в int (§4).

    Документ приходит из файла, поэтому его поля недоверенные: разбор не должен
    поднимать ValueError мимо кодов отказа и не должен тратить время на число,
    которое всё равно больше предела точного целого.
    """
    payload = boundary_payload()
    for value in ("9" * 5000, "9" * 17, str(MAX_SAFE_INTEGER + 1)):
        payload["boundary_seq"] = value
        with pytest.raises(BoundaryError) as error:
            ProcessBoundary.parse(CanonicalJson.encode(payload))
        assert error.value.code is BoundaryErrorCode.INVALID_DOCUMENT


def test_document_above_the_agreed_limit_is_refused_on_decode() -> None:
    """Предел 1 MiB одинаков до и после разбора (§8.5)."""
    with pytest.raises(BoundaryError) as error:
        CanonicalJson.decode(b"{}" + b" " * DOCUMENT_MAX_BYTES)
    assert error.value.code is BoundaryErrorCode.LIMIT_EXCEEDED


def test_timestamp_helper_requires_an_aware_moment() -> None:
    """Время документа всегда UTC Z: наивный момент не принимается."""
    assert utc_timestamp(datetime(2026, 10, 14, 10, 0, tzinfo=UTC)) == "2026-10-14T10:00:00Z"
    with pytest.raises(BoundaryError):
        utc_timestamp(datetime(2026, 10, 14, 10, 0))  # noqa: DTZ001
