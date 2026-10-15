"""Startup-выделение первого поколения журнала Low (§8.1 предложения о границе).

Выделение передаёт runtime, когда привязывает нового писателя к новой цепочке.
Оно относится только к первому поколению процесса и проверяется до открытия
слушателей: запуск, который не подтверждает переданную идентичность, прекращается.
"""

import json
from pathlib import Path

import pytest

from honeypots.low.__main__ import build_parser, journal_from_args, main
from honeypots.low.journal import JournalAllocationError, JournalSettings, RotatingJournalSink

INSTANCE = "a2df557e-785f-48f3-ba6e-348b76730f41"
DEPLOYMENT = "e0d692f0-fb4c-4a37-920e-0c8683471639"
ALLOCATED = "61c4c12f-bcd3-45eb-8498-d04f84d6cc75"
OTHER = "20c648ef-de66-4bf1-94ef-deebdc14a298"


def settings(directory: Path, journal: str | None, number: int | None) -> JournalSettings:
    return JournalSettings(
        directory,
        INSTANCE,
        DEPLOYMENT,
        1,
        max_file_bytes=256 * 1024,
        max_total_bytes=4 * 256 * 1024,
        first_journal_id=journal,
        first_journal_seq=number,
    )


def header(path: Path) -> dict[str, object]:
    first = path.read_bytes().split(b"\n", 1)[0]
    parsed = json.loads(first)
    assert isinstance(parsed, dict)
    return parsed


def test_allocated_identity_is_applied_to_the_first_generation(tmp_path: Path) -> None:
    """Первое поколение получает именно выделенные journal_id и номер."""
    sink = RotatingJournalSink(settings(tmp_path, ALLOCATED, 1))
    sink.open()
    try:
        assert sink.current_journal_id == ALLOCATED
        path = tmp_path / f"low-{INSTANCE}-000001.jsonl"
        record = header(path)
        assert record["journal_id"] == ALLOCATED
        assert record["journal_seq"] == 1
        assert record["previous_journal_id"] is None
        assert record["first_seq"] == 1
    finally:
        sink.close()


def test_allocation_applies_only_to_the_first_generation(tmp_path: Path) -> None:
    """Следующие поколения Low выбирает сам, как и до появления выделения."""
    sink = RotatingJournalSink(settings(tmp_path, ALLOCATED, 1))
    sink.open()
    try:
        sink.emit({"seq": 1, "kind": "probe"})
        sink.close("rotation")
        sink.open()
        second = sink.current_journal_id
        assert second is not None and second != ALLOCATED
        record = header(tmp_path / f"low-{INSTANCE}-000002.jsonl")
        assert record["journal_seq"] == 2
        assert record["previous_journal_id"] == ALLOCATED
    finally:
        sink.close()


@pytest.mark.parametrize("allocated_seq", [1, 3])
def test_other_computed_number_stops_the_writer_before_any_file(
    tmp_path: Path, allocated_seq: int
) -> None:
    """Номер, вычисленный Low, обязан совпасть с выделенным: иначе отказ до записи."""
    existing = RotatingJournalSink(settings(tmp_path, None, None))
    existing.open()
    existing.close()
    assert sorted(path.name for path in tmp_path.iterdir()) == [f"low-{INSTANCE}-000001.jsonl"]
    sink = RotatingJournalSink(settings(tmp_path, ALLOCATED, allocated_seq))
    with pytest.raises(JournalAllocationError):
        sink.open()
    # Отказ не создаёт файла и не расходует номер поколения.
    assert sorted(path.name for path in tmp_path.iterdir()) == [f"low-{INSTANCE}-000001.jsonl"]


def test_allocation_is_not_consumed_by_a_failed_open(tmp_path: Path) -> None:
    """Неудачное открытие сохраняет выделение для следующей попытки."""
    sink = RotatingJournalSink(settings(tmp_path, ALLOCATED, 2))
    with pytest.raises(JournalAllocationError):
        sink.open()
    (tmp_path / f"low-{INSTANCE}-000001.jsonl").write_bytes(
        b'{"record":"header","journal_seq":1}\n'
    )
    sink.open()
    try:
        assert sink.current_journal_id == ALLOCATED
        assert header(tmp_path / f"low-{INSTANCE}-000002.jsonl")["journal_id"] == ALLOCATED
    finally:
        sink.close()


@pytest.mark.parametrize(
    ("journal", "number"),
    [(ALLOCATED, None), (None, 1), ("not-a-uuid", 1), (ALLOCATED, 0), (ALLOCATED, True)],
)
def test_allocation_settings_are_validated(
    tmp_path: Path, journal: str | None, number: object
) -> None:
    """Идентификатор и номер задаются вместе, оба строго проверяются."""
    with pytest.raises(ValueError):
        settings(tmp_path, journal, number)  # type: ignore[arg-type]


def test_cli_requires_a_journal_directory_for_the_allocation(tmp_path: Path) -> None:
    """Выделение без файлового журнала не имеет смысла и отвергается разбором."""
    arguments = build_parser().parse_args(
        [f"--journal-first-id={ALLOCATED}", "--journal-first-seq=1"]
    )
    with pytest.raises(ValueError):
        journal_from_args(arguments)


def test_cli_passes_the_allocation_into_journal_settings(tmp_path: Path) -> None:
    """Параметры командной строки доходят до настроек журнала без изменений."""
    arguments = build_parser().parse_args(
        [
            f"--journal-dir={tmp_path}",
            f"--instance-id={INSTANCE}",
            f"--deployment-id={DEPLOYMENT}",
            "--profile-revision=1",
            f"--journal-first-id={ALLOCATED}",
            "--journal-first-seq=7",
        ]
    )
    prepared = journal_from_args(arguments)
    assert prepared is not None
    assert (prepared.first_journal_id, prepared.first_journal_seq) == (ALLOCATED, 7)


def test_cli_stops_with_a_readable_error_before_opening_listeners(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Несовпадение выделения завершает процесс кодом 2 без трассировки."""
    existing = RotatingJournalSink(settings(tmp_path, None, None))
    existing.open()
    existing.close()
    with pytest.raises(SystemExit) as exit_info:
        main(
            [
                "--tcp-port=0",
                "--http-port=0",
                f"--journal-dir={tmp_path}",
                f"--instance-id={INSTANCE}",
                f"--deployment-id={DEPLOYMENT}",
                "--profile-revision=1",
                f"--journal-first-id={OTHER}",
                "--journal-first-seq=1",
            ]
        )
    assert exit_info.value.code == 2
    error = capsys.readouterr().err
    assert "выделенный номер первого поколения" in error
    assert "Traceback" not in error
    # Второй файл не создан: ловушка не открывала слушателей и не писала журнал.
    assert sorted(path.name for path in tmp_path.iterdir()) == [f"low-{INSTANCE}-000001.jsonl"]
