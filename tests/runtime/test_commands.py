"""Контракт команд на настоящих SQLite-файлах, включая сбои и конкуренцию."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest

from agent.runtime.commands import (
    MAX_JSON_BYTES,
    Command,
    CommandAction,
    CommandJournal,
    CommandRecord,
    CommandStatus,
    JournalError,
    JournalErrorCode,
    RuntimeState,
)


def command(
    action: CommandAction = CommandAction.START,
    generation: int = 0,
    payload: object = None,
    command_id: str | None = None,
) -> Command:
    return Command(command_id or str(uuid4()), action, generation, payload)


def test_initial_state_and_pending_survive_reopen(tmp_path: Path) -> None:
    path = tmp_path / "commands.sqlite"
    request = command(payload={"profile": "linux"})
    with CommandJournal(path) as journal:
        assert journal.state == RuntimeState(0, None)
        accepted = journal.accept(request)
        assert accepted.status is CommandStatus.PENDING
        assert journal.state == RuntimeState(0, None)
    with CommandJournal(path) as reopened:
        assert reopened.pending == accepted
        assert reopened.get(request.command_id) == accepted
        assert reopened.accept(request) == accepted
        with pytest.raises(JournalError) as error:
            reopened.accept(command())
        assert error.value.code is JournalErrorCode.BUSY


def test_lost_response_duplicate_precedes_stale_generation(tmp_path: Path) -> None:
    path = tmp_path / "commands.sqlite"
    request = command(payload={"z": 1, "a": ["тест", True]})
    instance = str(uuid4())
    with CommandJournal(path) as journal:
        journal.accept(request)
        completed = journal.finish(
            request.command_id,
            CommandStatus.SUCCEEDED,
            result={"healthy": True},
            instance_id=instance,
        )
    with CommandJournal(path) as reopened:
        repeated = Command(
            request.command_id.upper(), request.action, 0, {"a": ["тест", True], "z": 1}
        )
        assert reopened.accept(repeated) == completed
        assert reopened.state == RuntimeState(1, instance)
        with pytest.raises(JournalError) as error:
            reopened.accept(command())
        assert error.value.code is JournalErrorCode.STALE_GENERATION


@pytest.mark.parametrize("change", ["action", "generation", "payload"])
def test_duplicate_id_with_changed_content_is_conflict(tmp_path: Path, change: str) -> None:
    with CommandJournal(tmp_path / "commands.sqlite") as journal:
        request = command(payload={"x": 1})
        journal.accept(request)
        changed = Command(
            request.command_id,
            CommandAction.STOP if change == "action" else request.action,
            1 if change == "generation" else 0,
            {"x": 2} if change == "payload" else request.payload,
        )
        with pytest.raises(JournalError) as error:
            journal.accept(changed)
        assert error.value.code is JournalErrorCode.COMMAND_CONFLICT


def test_start_reset_stop_and_failure_preserve_expected_metadata(tmp_path: Path) -> None:
    with CommandJournal(tmp_path / "commands.sqlite") as journal:
        first_instance = str(uuid4())
        second_instance = str(uuid4())
        start = command()
        journal.accept(start)
        journal.finish(start.command_id, CommandStatus.SUCCEEDED, instance_id=first_instance)
        failed_reset = command(CommandAction.RESET, 1)
        journal.accept(failed_reset)
        failed = journal.finish(
            failed_reset.command_id, CommandStatus.FAILED, result={"code": "EVIDENCE_SEAL_FAILED"}
        )
        assert failed.generation == 1
        assert failed.instance_id == first_instance
        assert journal.state == RuntimeState(1, first_instance)
        reset = command(CommandAction.RESET, 1)
        journal.accept(reset)
        with pytest.raises(ValueError, match="использовался"):
            journal.finish(reset.command_id, CommandStatus.SUCCEEDED, instance_id=first_instance)
        assert journal.pending is not None
        journal.finish(reset.command_id, CommandStatus.SUCCEEDED, instance_id=second_instance)
        stop = command(CommandAction.STOP, 2)
        journal.accept(stop)
        stopped = journal.finish(stop.command_id, CommandStatus.SUCCEEDED)
        assert stopped.generation == 2
        assert stopped.instance_id == second_instance
        assert journal.state == RuntimeState(2, second_instance)
        assert journal.pending is None


def test_completed_result_is_immutable_and_repeat_does_not_increment(tmp_path: Path) -> None:
    with CommandJournal(tmp_path / "commands.sqlite") as journal:
        request = command()
        journal.accept(request)
        instance = str(uuid4())
        completed = journal.finish(
            request.command_id,
            CommandStatus.SUCCEEDED,
            result={"a": 1, "b": 2},
            instance_id=instance,
        )
        assert (
            journal.finish(
                request.command_id,
                CommandStatus.SUCCEEDED,
                result={"b": 2, "a": 1},
                instance_id=instance.upper(),
            )
            == completed
        )
        for status, result, identifier in (
            (CommandStatus.FAILED, {"a": 1, "b": 2}, None),
            (CommandStatus.SUCCEEDED, {"a": 2, "b": 2}, instance),
            (CommandStatus.SUCCEEDED, {"a": 1, "b": 2}, str(uuid4())),
        ):
            with pytest.raises(JournalError) as error:
                journal.finish(request.command_id, status, result=result, instance_id=identifier)
            assert error.value.code is JournalErrorCode.RESULT_CONFLICT
        assert journal.get(request.command_id) == completed
        assert journal.state == RuntimeState(1, instance)


def test_old_completion_does_not_restore_old_slot_state(tmp_path: Path) -> None:
    with CommandJournal(tmp_path / "commands.sqlite") as journal:
        first = command()
        first_instance = str(uuid4())
        journal.accept(first)
        first_result = journal.finish(
            first.command_id, CommandStatus.SUCCEEDED, instance_id=first_instance
        )
        second = command(CommandAction.RESET, 1)
        second_instance = str(uuid4())
        journal.accept(second)
        journal.finish(second.command_id, CommandStatus.SUCCEEDED, instance_id=second_instance)
        assert (
            journal.finish(first.command_id, CommandStatus.SUCCEEDED, instance_id=first_instance)
            == first_result
        )
        assert journal.state == RuntimeState(2, second_instance)
        third = command(CommandAction.RESET, 2)
        journal.accept(third)
        with pytest.raises(ValueError, match="использовался"):
            journal.finish(third.command_id, CommandStatus.SUCCEEDED, instance_id=first_instance)


def test_failed_result_is_immutable_and_payload_is_snapshotted(tmp_path: Path) -> None:
    payload = {"items": [1]}
    with CommandJournal(tmp_path / "commands.sqlite") as journal:
        request = command(payload=payload)
        accepted = journal.accept(request)
        payload["items"].append(2)
        assert journal.get(request.command_id) == accepted
        failed = journal.finish(request.command_id, CommandStatus.FAILED, result={"code": "FAIL"})
        assert (
            journal.finish(request.command_id, CommandStatus.FAILED, result={"code": "FAIL"})
            == failed
        )
        with pytest.raises(JournalError) as error:
            journal.finish(request.command_id, CommandStatus.SUCCEEDED, instance_id=str(uuid4()))
        assert error.value.code is JournalErrorCode.RESULT_CONFLICT
        assert journal.state == RuntimeState(0, None)


@pytest.mark.parametrize("generation", [-1, True, False, 1.5, "0", 2**63])
def test_invalid_generation_does_not_write(tmp_path: Path, generation: int) -> None:
    with CommandJournal(tmp_path / "commands.sqlite") as journal:
        with pytest.raises(ValueError):
            journal.accept(command(generation=generation))
        assert journal.pending is None


# Идентификаторы заданы явно: pytest кладёт идентификатор теста в переменную окружения
# PYTEST_CURRENT_TEST, а Windows ограничивает значение 32767 символами. Автоматический
# идентификатор из строки в 64 КиБ ломал подготовку этих тестов на Windows.
@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"x": float("nan")}, id="nan-in-dict"),
        pytest.param([float("inf")], id="inf-in-list"),
        pytest.param(float("-inf"), id="minus-inf"),
        pytest.param({1: "x"}, id="non-string-key"),
        pytest.param({"x": {False: None}}, id="non-string-nested-key"),
        pytest.param(object(), id="arbitrary-object"),
        pytest.param((1, 2), id="tuple"),
        pytest.param("x" * MAX_JSON_BYTES, id="oversized-string"),
        pytest.param("\x00" * (MAX_JSON_BYTES // 3), id="oversized-after-json-escaping"),
        pytest.param("\ud800", id="lone-surrogate"),
    ],
)
def test_invalid_json_does_not_write(tmp_path: Path, payload: object) -> None:
    with CommandJournal(tmp_path / "commands.sqlite") as journal:
        with pytest.raises(ValueError):
            journal.accept(command(payload=payload))
        assert journal.pending is None


def test_cyclic_payload_is_rejected(tmp_path: Path) -> None:
    payload: list[object] = []
    payload.append(payload)
    with CommandJournal(tmp_path / "commands.sqlite") as journal:
        with pytest.raises(ValueError):
            journal.accept(command(payload=payload))


def test_uuid_validation_and_unknown_completion(tmp_path: Path) -> None:
    with CommandJournal(tmp_path / "commands.sqlite") as journal:
        with pytest.raises(ValueError):
            journal.accept(command(command_id="not-a-uuid"))
        with pytest.raises(JournalError) as error:
            journal.finish(str(uuid4()), CommandStatus.FAILED)
        assert error.value.code is JournalErrorCode.UNKNOWN_COMMAND
        assert journal.get(str(uuid4())) is None
        request = command()
        journal.accept(request)
        with pytest.raises(ValueError):
            journal.finish(request.command_id, CommandStatus.PENDING)
        with pytest.raises(ValueError):
            journal.finish(request.command_id, CommandStatus.SUCCEEDED)
        with pytest.raises(ValueError):
            journal.finish(request.command_id, CommandStatus.SUCCEEDED, instance_id="bad")
        with pytest.raises(ValueError):
            journal.finish(request.command_id, CommandStatus.FAILED, instance_id=str(uuid4()))
        assert journal.pending is not None


def test_two_connections_accept_only_one_operation(tmp_path: Path) -> None:
    path = tmp_path / "commands.sqlite"
    barrier = Barrier(2)

    def accept() -> CommandRecord | JournalErrorCode:
        with CommandJournal(path) as journal:
            barrier.wait(timeout=5)
            try:
                return journal.accept(command())
            except JournalError as exc:
                return exc.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: accept(), range(2)))
    assert sum(isinstance(result, CommandRecord) for result in results) == 1
    assert results.count(JournalErrorCode.BUSY) == 1
    with CommandJournal(path) as journal:
        assert journal.pending is not None
        assert journal.state == RuntimeState(0, None)


def test_two_connections_complete_once(tmp_path: Path) -> None:
    path = tmp_path / "commands.sqlite"
    request = command()
    instance = str(uuid4())
    with CommandJournal(path) as journal:
        journal.accept(request)
    barrier = Barrier(2)

    def complete() -> CommandRecord:
        with CommandJournal(path) as journal:
            barrier.wait(timeout=5)
            return journal.finish(
                request.command_id,
                CommandStatus.SUCCEEDED,
                result={"ready": True},
                instance_id=instance,
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: complete(), range(2)))
    assert results[0] == results[1]
    with CommandJournal(path) as journal:
        assert journal.state == RuntimeState(1, instance)


def test_two_connections_retry_same_command(tmp_path: Path) -> None:
    path = tmp_path / "commands.sqlite"
    request = command(payload={"profile": "linux"})
    barrier = Barrier(2)

    def accept() -> CommandRecord:
        with CommandJournal(path) as journal:
            barrier.wait(timeout=5)
            return journal.accept(request)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: accept(), range(2)))
    assert results[0] == results[1]
    with CommandJournal(path) as journal:
        assert journal.pending == results[0]


def test_transaction_rolls_back_completion_when_metadata_write_fails(tmp_path: Path) -> None:
    """Ошибка второй записи не должна оставлять терминальную команду без generation."""
    path = tmp_path / "commands.sqlite"
    request = command()
    with CommandJournal(path) as journal:
        accepted = journal.accept(request)
        with sqlite3.connect(path) as connection:
            connection.execute("""
                CREATE TRIGGER fail_state_update BEFORE UPDATE ON runtime_state
                BEGIN SELECT RAISE(ABORT, 'simulated storage failure'); END
            """)
        with pytest.raises(sqlite3.IntegrityError, match="simulated storage failure"):
            journal.finish(request.command_id, CommandStatus.SUCCEEDED, instance_id=str(uuid4()))
    with CommandJournal(path) as reopened:
        assert reopened.get(request.command_id) == accepted
        assert reopened.state == RuntimeState(0, None)
        with sqlite3.connect(path) as connection:
            connection.execute("DROP TRIGGER fail_state_update")
        completed = reopened.finish(
            request.command_id, CommandStatus.SUCCEEDED, instance_id=str(uuid4())
        )
        assert completed.generation == 1
