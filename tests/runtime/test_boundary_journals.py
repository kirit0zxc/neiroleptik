"""Наблюдение каталога слота Low для границы процесса (§8.1, §8.3).

Проверки опираются и на точные байты фикстур репозитория, и на журналы, записанные
настоящим писателем ловушки: правило границы полных строк, цепочка поколений и
отказы не должны расходиться между этими источниками.
"""

import json
from pathlib import Path

import pytest

from agent.runtime.boundary.documents import WriterBinding
from agent.runtime.boundary.errors import BoundaryError, BoundaryErrorCode
from agent.runtime.boundary.journals import (
    JournalReader,
    JournalSlot,
    is_footer_line,
    journal_name,
)
from honeypots.low.journal import JournalSettings, RotatingJournalSink

SAMPLES = Path(__file__).resolve().parents[2] / "specs/samples/low-process-boundary"
INSTANCE = "a2df557e-785f-48f3-ba6e-348b76730f41"
DEPLOYMENT = "e0d692f0-fb4c-4a37-920e-0c8683471639"
AGENT = "00000000-0000-4000-8000-000000000001"


def binding(revision: int = 1) -> WriterBinding:
    """Привязка перехода из фикстуры наблюдения 11.10."""
    return WriterBinding(AGENT, DEPLOYMENT, INSTANCE, revision)


def place(directory: Path, number: int, source: str) -> Path:
    """Кладёт байты фикстуры под каноническим именем поколения слота."""
    target = directory / journal_name(INSTANCE, number)
    target.write_bytes((SAMPLES / source).read_bytes())
    return target


def header_id(path: Path) -> str:
    """journal_id из заголовка файла."""
    first = path.read_bytes().split(b"\n", 1)[0]
    return str(json.loads(first)["journal_id"])


def write_real_journal(directory: Path, generations: int) -> list[str]:
    """Пишет настоящие поколения писателем ловушки и возвращает их journal_id."""
    settings = JournalSettings(
        directory,
        INSTANCE,
        DEPLOYMENT,
        1,
        max_file_bytes=256 * 1024,
        max_total_bytes=4 * 256 * 1024,
    )
    sink = RotatingJournalSink(settings)
    identifiers: list[str] = []
    sink.open()
    for number in range(generations):
        current = sink.current_journal_id
        assert current is not None
        identifiers.append(current)
        sink.emit({"seq": number + 1, "kind": "probe"})
        if number + 1 < generations:
            sink.close("rotation")
            sink.open()
    sink.close()
    return identifiers


def test_fixture_measurements_match_the_published_manifest() -> None:
    """Размер, граница полных строк и хвост совпадают с manifest.json."""
    data = json.loads((SAMPLES / "manifest.json").read_text(encoding="utf-8"))
    reader = JournalReader()
    for item in data["files"]:
        path = SAMPLES / str(item["file"])
        header = reader.read_header(path)
        measured = reader.measure(path, header.journal_id)
        assert measured.size_bytes == int(str(item["size_bytes"]))
        assert measured.sha256 == item["sha256"]
        assert measured.complete_lf_end == int(str(item["complete_lf_end"]))
        assert measured.tail_bytes == int(str(item["tail_bytes"]))
        # Ни один файл фикстуры не закрыт концовкой: это аварийное завершение.
        assert measured.footer_present is False


def test_header_hash_covers_the_line_together_with_its_newline() -> None:
    """header_sha256 считается по полной первой строке вместе с LF (§8.1)."""
    import hashlib

    path = SAMPLES / "old-crash.jsonl"
    raw = path.read_bytes()
    line = raw[: raw.index(b"\n") + 1]
    header = JournalReader().read_header(path)
    assert header.header_bytes == len(line)
    assert header.header_sha256 == hashlib.sha256(line).hexdigest()
    assert hashlib.sha256(line.rstrip(b"\n")).hexdigest() != header.header_sha256


def test_real_writer_chain_is_collected_in_order(tmp_path: Path) -> None:
    """Цепочка поколений настоящего писателя собирается без пропусков и развилок."""
    identifiers = write_real_journal(tmp_path, 3)
    slot = JournalSlot(tmp_path, binding())
    chain = slot.collect_writer_chain(identifiers[0], 1)
    assert [item.header.journal_seq for item in chain] == [1, 2, 3]
    assert [item.header.journal_id for item in chain] == identifiers
    assert [item.header.previous_journal_id for item in chain] == [None, *identifiers[:2]]
    # Закрытые поколения имеют концовку; все строки учтены границей полных строк.
    assert all(item.measurement.footer_present for item in chain)
    assert all(item.measurement.tail_bytes == 0 for item in chain)


def test_new_writer_generation_breaks_the_chain_of_the_closed_writer(tmp_path: Path) -> None:
    """Пока новый writer не ограждён, его поколение делает набор недоказанным.

    Снимки создаются после ограждения и до выдачи нового RW (§3 шаг 3): поколение с
    previous_journal_id=null внутри набора означает, что порядок нарушен.
    """
    first = place(tmp_path, 1, "old-crash.jsonl")
    place(tmp_path, 2, "new-process.jsonl")
    slot = JournalSlot(tmp_path, binding())
    with pytest.raises(BoundaryError) as error:
        slot.collect_writer_chain(header_id(first), 1)
    assert error.value.code is BoundaryErrorCode.BOUNDARY_SNAPSHOT_UNAVAILABLE


def test_closed_writer_set_is_collected_before_the_new_generation(tmp_path: Path) -> None:
    """До появления нового поколения набор закрываемого writer собирается полностью."""
    first = place(tmp_path, 1, "old-crash.jsonl")
    slot = JournalSlot(tmp_path, binding())
    chain = slot.collect_writer_chain(header_id(first), 1)
    assert len(chain) == 1
    assert chain[0].measurement.size_bytes == 885
    assert chain[0].header.journal_seq == 1


def test_gap_in_numbering_blocks_the_transition(tmp_path: Path) -> None:
    """Пропуск номера поколения — BOUNDARY_SNAPSHOT_UNAVAILABLE (§8.3)."""
    identifiers = write_real_journal(tmp_path, 3)
    (tmp_path / journal_name(INSTANCE, 2)).unlink()
    slot = JournalSlot(tmp_path, binding())
    with pytest.raises(BoundaryError) as error:
        slot.collect_writer_chain(identifiers[0], 1)
    assert error.value.code is BoundaryErrorCode.BOUNDARY_SNAPSHOT_UNAVAILABLE


def test_unknown_or_non_canonical_instance_names_block_the_transition(tmp_path: Path) -> None:
    """Неизвестное имя с префиксом экземпляра и неканонический номер блокируют набор."""
    identifiers = write_real_journal(tmp_path, 1)
    slot = JournalSlot(tmp_path, binding())
    assert set(slot.entries()) == {1}
    (tmp_path / f"low-{INSTANCE}-pending.jsonl").write_bytes(b"{}\n")
    with pytest.raises(BoundaryError):
        slot.collect_writer_chain(identifiers[0], 1)
    (tmp_path / f"low-{INSTANCE}-pending.jsonl").unlink()
    (tmp_path / f"low-{INSTANCE}-0000001.jsonl").write_bytes(b"{}\n")
    with pytest.raises(BoundaryError):
        slot.collect_writer_chain(identifiers[0], 1)


def test_foreign_binding_in_header_is_a_binding_mismatch(tmp_path: Path) -> None:
    """Заголовок другого назначения или ревизии к этому переходу не относится."""
    first = place(tmp_path, 1, "old-crash.jsonl")
    slot = JournalSlot(tmp_path, binding(revision=2))
    with pytest.raises(BoundaryError) as error:
        slot.collect_writer_chain(header_id(first), 1)
    assert error.value.code is BoundaryErrorCode.BOUNDARY_BINDING_MISMATCH


def test_first_journal_must_match_the_allocation(tmp_path: Path) -> None:
    """Первый журнал набора сверяется с выделением runtime, а не с именем файла."""
    place(tmp_path, 1, "old-crash.jsonl")
    slot = JournalSlot(tmp_path, binding())
    with pytest.raises(BoundaryError) as error:
        slot.collect_writer_chain("11111111-1111-4111-8111-111111111111", 1)
    assert error.value.code is BoundaryErrorCode.BOUNDARY_BINDING_MISMATCH


def test_journal_without_a_complete_header_line_is_refused(tmp_path: Path) -> None:
    """Файл без полной строки заголовка не входит в набор (§8.3)."""
    target = tmp_path / journal_name(INSTANCE, 1)
    raw = (SAMPLES / "old-crash.jsonl").read_bytes()
    target.write_bytes(raw[:100])
    with pytest.raises(BoundaryError) as error:
        JournalReader().read_header(target)
    assert error.value.code is BoundaryErrorCode.BOUNDARY_SNAPSHOT_UNAVAILABLE


def test_tail_fixture_keeps_every_byte_and_the_last_complete_line(tmp_path: Path) -> None:
    """Неполный хвост сохраняется в размере и хеше, но не в границе полных строк."""
    target = place(tmp_path, 1, "old-with-tail.jsonl")
    header = JournalReader().read_header(target)
    measured = JournalReader().measure(target, header.journal_id)
    assert (measured.size_bytes, measured.complete_lf_end, measured.tail_bytes) == (910, 885, 25)


def test_footer_of_another_generation_does_not_close_this_file() -> None:
    """Строка footer с чужим journal_id закрытием файла не считается."""
    own = "61c4c12f-bcd3-45eb-8498-d04f84d6cc75"
    line = json.dumps({"record": "footer", "journal_id": own, "last_seq": 1}).encode()
    assert is_footer_line(line, own) is True
    assert is_footer_line(line, "20c648ef-de66-4bf1-94ef-deebdc14a298") is False
    assert is_footer_line(b'{"record":"event","seq":1}', own) is False
    assert is_footer_line(b"not json", own) is False


def test_directory_usage_counts_every_instance_of_the_slot(tmp_path: Path) -> None:
    """Квота Low считается по каталогу: в неё входят поколения всех экземпляров."""
    place(tmp_path, 1, "old-crash.jsonl")
    other = "b2df557e-785f-48f3-ba6e-348b76730f41"
    (tmp_path / journal_name(other, 1)).write_bytes(b"x" * 100)
    slot = JournalSlot(tmp_path, binding())
    assert set(slot.entries()) == {1}
    assert slot.directory_usage() == 985


def test_missing_slot_directory_is_an_empty_slot(tmp_path: Path) -> None:
    """Каталог слота ещё не создан: поколений нет, занятое место равно нулю."""
    slot = JournalSlot(tmp_path / "absent", binding())
    assert slot.entries() == {}
    assert slot.directory_usage() == 0


def test_canonical_generation_name_pads_to_six_digits() -> None:
    """Имя поколения совпадает с тем, которое создаёт сам Low."""
    assert journal_name(INSTANCE, 2) == f"low-{INSTANCE}-000002.jsonl"
    assert journal_name(INSTANCE, 1234567) == f"low-{INSTANCE}-1234567.jsonl"
    with pytest.raises(BoundaryError):
        journal_name(INSTANCE, 0)


def header_line(**changes: object) -> bytes:
    """Строит строку header по образцу фикстуры с заменой отдельных полей."""
    record = json.loads((SAMPLES / "old-crash.jsonl").read_bytes().split(b"\n", 1)[0])
    record.update(changes)
    return json.dumps(record, ensure_ascii=True, separators=(",", ":")).encode("ascii") + b"\n"


def place_header(directory: Path, number: int, **changes: object) -> None:
    """Кладёт поколение, состоящее только из заголовка с заданными полями."""
    (directory / journal_name(INSTANCE, number)).write_bytes(
        header_line(journal_seq=number, **changes)
    )


def test_fork_of_the_previous_chain_blocks_the_transition(tmp_path: Path) -> None:
    """Две цепочки от одного предшественника — развилка, а не продолжение (§8.3)."""
    first = place(tmp_path, 1, "old-crash.jsonl")
    root = header_id(first)
    second = "20c648ef-de66-4bf1-94ef-deebdc14a298"
    third = "31d759f0-ef77-4c02-a5f0-efffcd25b309"
    place_header(tmp_path, 2, journal_id=second, previous_journal_id=root)
    place_header(tmp_path, 3, journal_id=third, previous_journal_id=root)
    slot = JournalSlot(tmp_path, binding())
    with pytest.raises(BoundaryError) as error:
        slot.collect_writer_chain(root, 1)
    assert error.value.code is BoundaryErrorCode.BOUNDARY_SNAPSHOT_UNAVAILABLE
    assert "previous_journal_id" in error.value.detail


def test_repeated_journal_identifier_blocks_the_transition(tmp_path: Path) -> None:
    """Повтор journal_id внутри набора writer недопустим (§4, §8.3)."""
    first = place(tmp_path, 1, "old-crash.jsonl")
    root = header_id(first)
    place_header(tmp_path, 2, journal_id=root, previous_journal_id=root)
    with pytest.raises(BoundaryError) as error:
        JournalSlot(tmp_path, binding()).collect_writer_chain(root, 1)
    assert error.value.code is BoundaryErrorCode.BOUNDARY_SNAPSHOT_UNAVAILABLE
    assert "journal_id" in error.value.detail


def test_generation_that_is_not_a_regular_file_blocks_the_transition(tmp_path: Path) -> None:
    """Каталог с именем поколения не принимается за журнал."""
    (tmp_path / journal_name(INSTANCE, 1)).mkdir()
    with pytest.raises(BoundaryError) as error:
        JournalSlot(tmp_path, binding()).entries()
    assert error.value.code is BoundaryErrorCode.BOUNDARY_SNAPSHOT_UNAVAILABLE
    with pytest.raises(BoundaryError):
        JournalReader().read_header(tmp_path / journal_name(INSTANCE, 1))


def test_uppercase_instance_prefix_is_not_silently_ignored(tmp_path: Path) -> None:
    """Имя с префиксом экземпляра в другом регистре блокирует переход.

    На регистронезависимой файловой системе это тот же файл, поэтому молча
    пропускать такое имя нельзя: иначе набор writer оказался бы неполным.
    """
    identifiers = write_real_journal(tmp_path, 1)
    (tmp_path / f"low-{INSTANCE.upper()}-000009.jsonl").write_bytes(b"{}\n")
    with pytest.raises(BoundaryError) as error:
        JournalSlot(tmp_path, binding()).collect_writer_chain(identifiers[0], 1)
    assert error.value.code is BoundaryErrorCode.BOUNDARY_SNAPSHOT_UNAVAILABLE
