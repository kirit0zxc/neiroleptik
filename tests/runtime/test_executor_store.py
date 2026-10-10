"""Состояние исполнителя на настоящем диске: повторы, поколения и атомарные отказы."""

import json
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from agent.runtime.executor.store import DeploymentHistory, ExecutionStore, StoreError


def metadata(
    *,
    action: str = "start",
    expected: int = 0,
    deployment: str | None = None,
    instance: str | None = None,
    reconcile: bool = False,
) -> dict[str, object]:
    return {
        "command_id": str(uuid4()),
        "deployment_id": deployment or str(uuid4()),
        "action": action,
        "expected_generation": expected,
        "generation": expected + 1,
        "revision": 3,
        "expected_instance_id": instance,
        "reconcile": reconcile,
    }


def result(
    request: dict[str, object],
    instance: str | None,
    *,
    revision: int | None = 3,
    failed: bool = False,
    runtime: str = "running",
) -> dict[str, object]:
    return {
        "deployment_id": request["deployment_id"],
        "generation": request["generation"],
        "instance_id": instance,
        "applied_revision": revision,
        "outcome": "failed" if failed else "succeeded",
        "runtime_state": runtime,
        "error": {"code": "LAUNCH_FAILED", "message": "Запуск не подтверждён"} if failed else None,
    }


def store_path(tmp_path: Path) -> Path:
    return tmp_path / "state" / "executor.sqlite"


def test_prepared_attempt_and_secret_free_metadata_survive_restart(tmp_path: Path) -> None:
    path = store_path(tmp_path)
    request = metadata()
    request.update(
        {"config": {"password": "NEVER_PERSIST_PROFILE_SECRET"}, "config_sha256": "c" * 64}
    )
    with ExecutionStore(path) as store:
        assert store.pending() is None
        assert store.history(str(request["deployment_id"])) == DeploymentHistory()
        accepted = store.accept(request, "a" * 64, "private-lease")
        assert accepted.phase == "prepared"
        assert accepted.attempt_instance_id is not None
        assert "private-lease" not in repr(accepted)
        assert "NEVER_PERSIST_PROFILE_SECRET" not in repr(accepted)
    with ExecutionStore(path) as reopened:
        assert reopened.pending() == accepted
        assert reopened.accept(request, "a" * 64, "changed-token") == accepted
        assert reopened.update_lease(accepted.command_id, "new-token").lease_token == "new-token"
        executing = reopened.mark_executing(accepted.command_id)
        assert executing.phase == "executing"
    with ExecutionStore(path) as reopened:
        assert reopened.pending() == executing
    assert b"NEVER_PERSIST_PROFILE_SECRET" not in path.read_bytes()


def test_lost_ack_replay_is_immutable_and_precedes_stale_generation(tmp_path: Path) -> None:
    path = store_path(tmp_path)
    request = metadata()
    with ExecutionStore(path) as store:
        accepted = store.accept(request, "a" * 64, "token")
        ack = result(request, accepted.attempt_instance_id)
        completed = store.complete(accepted.command_id, ack)
        assert store.pending() == completed
        assert store.history(accepted.deployment_id) == DeploymentHistory(
            1, accepted.attempt_instance_id, 3
        )
    with ExecutionStore(path) as store:
        assert store.accept(request, "a" * 64, "token") == completed
        assert store.complete(accepted.command_id, dict(reversed(list(ack.items())))) == completed
        store.update_lease(accepted.command_id, "new-token")
        assert store.get(accepted.command_id).result == ack  # type: ignore[union-attr]
        ack["runtime_state"] = "failed"
        with pytest.raises(StoreError, match="RESULT_CONFLICT"):
            store.complete(accepted.command_id, ack)
        assert store.acknowledge(accepted.command_id).acked
        assert store.pending() is None
        assert store.accept(request, "a" * 64, "token").acked


def test_start_stop_and_failed_commands_keep_history_with_generation_gaps(tmp_path: Path) -> None:
    path = store_path(tmp_path)
    first = metadata()
    deployment = str(first["deployment_id"])
    with ExecutionStore(path) as store:
        accepted = store.accept(first, "a" * 64, "token")
        instance = accepted.attempt_instance_id
        store.complete(accepted.command_id, result(first, instance))
        store.acknowledge(accepted.command_id)
        stop = metadata(action="stop", expected=3, deployment=deployment, instance=instance)
        stopped = store.accept(stop, "b" * 64, "token")
        store.complete(stopped.command_id, result(stop, instance, runtime="stopped"))
        store.acknowledge(stopped.command_id)
        assert store.history(deployment) == DeploymentHistory(4, instance, 3)
        failed = metadata(action="start", expected=4, deployment=deployment, instance=instance)
        failed_command = store.accept(failed, "c" * 64, "token")
        store.complete(
            failed_command.command_id, result(failed, instance, failed=True, runtime="failed")
        )
        store.acknowledge(failed_command.command_id)
        assert store.history(deployment) == DeploymentHistory(5, instance, 3)
    with ExecutionStore(path) as reopened:
        assert reopened.history(deployment) == DeploymentHistory(5, instance, 3)
        with pytest.raises(StoreError, match="STALE_GENERATION"):
            reopened.accept(metadata(expected=4, deployment=deployment), "d" * 64, "token")


@pytest.mark.parametrize("change", ["fingerprint", "action", "generation", "deployment_id"])
def test_repeated_id_rejects_changed_fingerprint_or_metadata(tmp_path: Path, change: str) -> None:
    request = metadata()
    with ExecutionStore(store_path(tmp_path)) as store:
        accepted = store.accept(request, "a" * 64, "token")
        altered = dict(request)
        fingerprint = "a" * 64
        if change == "fingerprint":
            fingerprint = "b" * 64
        elif change == "action":
            altered["action"] = "stop"
        elif change == "generation":
            altered["expected_generation"] = 1
            altered["generation"] = 2
        else:
            altered["deployment_id"] = str(uuid4())
        with pytest.raises(StoreError, match="COMMAND_CONFLICT"):
            store.accept(altered, fingerprint, "token")
        assert store.get(accepted.command_id) == accepted


def test_one_outstanding_delivery_includes_unacknowledged_completion(tmp_path: Path) -> None:
    with ExecutionStore(store_path(tmp_path)) as store:
        first = metadata()
        accepted = store.accept(first, "a" * 64, "token")
        store.complete(accepted.command_id, result(first, accepted.attempt_instance_id))
        with pytest.raises(StoreError, match="BUSY"):
            store.accept(metadata(), "b" * 64, "token")
        store.acknowledge(accepted.command_id)
        assert store.accept(metadata(), "b" * 64, "token").phase == "prepared"


def test_abandon_preserves_result_and_advances_expired_generation(tmp_path: Path) -> None:
    first = metadata()
    deployment = str(first["deployment_id"])
    path = store_path(tmp_path)
    with ExecutionStore(path) as store:
        accepted = store.accept(first, "a" * 64, "token")
        store.mark_executing(accepted.command_id)
        abandoned = store.abandon(accepted.command_id)
        assert abandoned.phase == "abandoned"
        assert abandoned.result is None
        assert store.pending() is None
        assert store.history(deployment) == DeploymentHistory(1, None, None)
        with pytest.raises(StoreError, match="INVALID_PHASE"):
            store.complete(accepted.command_id, result(first, accepted.attempt_instance_id))
        stop = metadata(action="stop", expected=1, deployment=deployment, reconcile=True)
        next_command = store.accept(stop, "b" * 64, "token")
        ack = result(stop, accepted.attempt_instance_id, runtime="stopped")
        store.complete(next_command.command_id, ack)
        abandoned_completed = store.abandon(next_command.command_id)
        assert abandoned_completed.result == ack
        assert store.history(deployment) == DeploymentHistory(2, accepted.attempt_instance_id, 3)
    with ExecutionStore(path) as reopened:
        assert reopened.get(next_command.command_id) == abandoned_completed
        assert reopened.pending() is None


def test_history_seed_does_not_replace_local_truth(tmp_path: Path) -> None:
    deployment, instance = str(uuid4()), str(uuid4())
    with ExecutionStore(store_path(tmp_path)) as store:
        assert not store.has_history(deployment)
        initial = store.seed_history(deployment, 12, instance, 3)
        assert initial == DeploymentHistory(12, instance, 3)
        assert store.has_history(deployment)
        assert store.seed_history(deployment, 12, instance, 3) == initial
        with pytest.raises(StoreError, match="HISTORY_CONFLICT"):
            store.seed_history(deployment, 0, None, None)
        assert store.history(deployment) == initial


def test_late_seed_cannot_make_completion_roll_generation_backwards(tmp_path: Path) -> None:
    request = metadata()
    with ExecutionStore(store_path(tmp_path)) as store:
        accepted = store.accept(request, "a" * 64, "token")
        newer = store.seed_history(accepted.deployment_id, 8, None, None)
        with pytest.raises(StoreError, match="STALE_GENERATION"):
            store.complete(accepted.command_id, result(request, accepted.attempt_instance_id))
        assert store.get(accepted.command_id) == accepted
        assert store.history(accepted.deployment_id) == newer


@pytest.mark.parametrize("expected", [True, -1, 1.5, 2**63 - 1, "1"])
def test_invalid_generations_are_rejected_before_disk_write(
    tmp_path: Path, expected: object
) -> None:
    request = metadata()
    request["expected_generation"] = expected
    request["generation"] = 2**63 if expected == 2**63 - 1 else 1
    with ExecutionStore(store_path(tmp_path)) as store:
        with pytest.raises(StoreError, match="INVALID_COMMAND"):
            store.accept(request, "a" * 64, "token")
        assert store.pending() is None


@pytest.mark.parametrize(
    "change", ["generation", "deployment_id", "lease_token", "bad_error", "revision"]
)
def test_invalid_result_cannot_change_command_or_history(tmp_path: Path, change: str) -> None:
    request = metadata()
    with ExecutionStore(store_path(tmp_path)) as store:
        accepted = store.accept(request, "a" * 64, "token")
        ack = result(request, accepted.attempt_instance_id)
        if change == "generation":
            ack["generation"] = 2
        elif change == "deployment_id":
            ack["deployment_id"] = str(uuid4())
        elif change == "lease_token":
            ack["lease_token"] = "secret"
        elif change == "bad_error":
            ack["error"] = {"code": "secret", "message": "secret"}
        else:
            ack["applied_revision"] = 4
        with pytest.raises(StoreError, match="INVALID_RESULT"):
            store.complete(accepted.command_id, ack)
        assert store.get(accepted.command_id) == accepted
        assert store.history(accepted.deployment_id) == DeploymentHistory()


def test_failed_start_cannot_claim_running_for_new_unhealthy_attempt(tmp_path: Path) -> None:
    previous = str(uuid4())
    request = metadata(instance=previous)
    with ExecutionStore(store_path(tmp_path)) as store:
        store.seed_history(str(request["deployment_id"]), 0, previous, 3)
        accepted = store.accept(request, "a" * 64, "token")
        with pytest.raises(StoreError, match="INVALID_RESULT"):
            store.complete(
                accepted.command_id, result(request, accepted.attempt_instance_id, failed=True)
            )
        confirmed_previous = store.complete(
            accepted.command_id, result(request, previous, failed=True)
        )
        assert confirmed_previous.result is not None
        assert confirmed_previous.result["runtime_state"] == "running"


def test_stop_cannot_erase_historical_revision_or_change_identity(tmp_path: Path) -> None:
    previous = str(uuid4())
    request = metadata(action="stop", expected=4, instance=previous)
    with ExecutionStore(store_path(tmp_path)) as store:
        store.seed_history(str(request["deployment_id"]), 4, previous, 3)
        accepted = store.accept(request, "a" * 64, "token")
        with pytest.raises(StoreError, match="INVALID_RESULT"):
            store.complete(
                accepted.command_id, result(request, previous, revision=None, runtime="stopped")
            )
        with pytest.raises(StoreError, match="INVALID_RESULT"):
            store.complete(accepted.command_id, result(request, str(uuid4()), runtime="stopped"))
        store.complete(accepted.command_id, result(request, previous, runtime="stopped"))
        assert store.history(accepted.deployment_id) == DeploymentHistory(5, previous, 3)


def test_sqlite_failure_rolls_back_result_and_history_together(tmp_path: Path) -> None:
    path = store_path(tmp_path)
    request = metadata()
    with ExecutionStore(path) as store:
        store.seed_history(str(request["deployment_id"]), 0, None, None)
        accepted = store.accept(request, "a" * 64, "token")
        with sqlite3.connect(path) as fault:
            fault.execute("""
                CREATE TRIGGER reject_history BEFORE UPDATE ON deployment_history
                BEGIN SELECT RAISE(ABORT, 'PRIVATE_INTERNAL_DETAIL'); END
            """)
        with pytest.raises(StoreError, match="STORE_IO") as error:
            store.complete(accepted.command_id, result(request, accepted.attempt_instance_id))
        assert "PRIVATE_INTERNAL_DETAIL" not in str(error.value)
        assert store.get(accepted.command_id) == accepted
        assert store.history(accepted.deployment_id) == DeploymentHistory()
    with ExecutionStore(path) as reopened:
        assert reopened.get(accepted.command_id) == accepted


def test_result_snapshot_mutation_does_not_modify_persisted_ack(tmp_path: Path) -> None:
    request = metadata()
    with ExecutionStore(store_path(tmp_path)) as store:
        accepted = store.accept(request, "a" * 64, "token")
        completed = store.complete(
            accepted.command_id, result(request, accepted.attempt_instance_id)
        )
        assert completed.result is not None
        completed.result["instance_id"] = str(uuid4())
        current = store.get(accepted.command_id)
        assert current is not None and current.result is not None
        assert current.result["instance_id"] == accepted.attempt_instance_id


def test_lock_is_held_between_transactions_and_released_on_close(tmp_path: Path) -> None:
    path = store_path(tmp_path)
    with ExecutionStore(path):
        with pytest.raises(StoreError, match="STORE_LOCKED"):
            ExecutionStore(path)
        with pytest.raises(StoreError, match="STORE_LOCKED"):
            ExecutionStore(path.parent / "different.sqlite")
    store = ExecutionStore(path)
    store.close()
    store.close()
    with pytest.raises(StoreError, match="CLOSED"):
        store.pending()
    with ExecutionStore(path):
        pass


def test_second_process_cannot_own_state_directory(tmp_path: Path) -> None:
    path = store_path(tmp_path)
    script = """
import sys
from agent.runtime.executor.store import ExecutionStore, StoreError
try:
    ExecutionStore(sys.argv[1])
except StoreError as error:
    print(error.code)
else:
    raise SystemExit(2)
"""
    with ExecutionStore(path):
        execution = subprocess.run(
            [sys.executable, "-c", script, str(path)],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert execution.returncode == 0, execution.stderr
        assert execution.stdout.strip() == "STORE_LOCKED"


def test_legacy_schema_is_rejected_without_mutation(tmp_path: Path) -> None:
    path = store_path(tmp_path)
    path.parent.mkdir(mode=0o700)
    with sqlite3.connect(path) as legacy:
        assert legacy.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
        legacy.execute("CREATE TABLE commands(payload TEXT)")
        legacy.execute("INSERT INTO commands VALUES (?)", ("historical-record",))
        legacy.execute("PRAGMA user_version = 2")
    if os.name != "nt":
        path.chmod(0o600)
    with pytest.raises(StoreError, match="SCHEMA_MISMATCH"):
        ExecutionStore(path)
    with sqlite3.connect(path) as legacy:
        assert legacy.execute("SELECT payload FROM commands").fetchall() == [("historical-record",)]
        assert legacy.execute("PRAGMA user_version").fetchone()[0] == 2
        assert legacy.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions требуют Unix")
def test_private_permissions_and_symlink_rejection(tmp_path: Path) -> None:
    path = store_path(tmp_path)
    with ExecutionStore(path):
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE((path.parent / "state.lock").stat().st_mode) == 0o600
    path.chmod(0o644)
    with pytest.raises(StoreError, match="UNSAFE_STATE_PATH"):
        ExecutionStore(path)
    path.chmod(0o600)
    alias = tmp_path / "alias"
    alias.symlink_to(path.parent, target_is_directory=True)
    with pytest.raises(StoreError, match="UNSAFE_STATE_PATH"):
        ExecutionStore(alias / "executor.sqlite")
    other = path.parent / "sidecar-target"
    other.write_text(json.dumps({"private": True}))
    journal = Path(str(path) + "-journal")
    journal.symlink_to(other)
    with pytest.raises(StoreError, match="UNSAFE_STATE_PATH"):
        ExecutionStore(path)
