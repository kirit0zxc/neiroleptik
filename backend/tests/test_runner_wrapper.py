"""Ошибки запуска проверок не должны превращаться в успешный результат."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

PWSH = shutil.which("pwsh")
WRAPPER = Path(__file__).resolve().parents[2] / "scripts/run_backend_tests_locked.ps1"


@pytest.fixture(autouse=True)
def clean_database() -> None:
    """Проверка запуска процесса не использует БД и отменяет общий SQL cleanup."""


@pytest.mark.skipif(PWSH is None, reason="Для проверки wrapper требуется PowerShell 7")
@pytest.mark.parametrize(
    "scenario", ["success", "test_failure", "missing_runner", "missing_backend"]
)
def test_wrapper_reports_actual_process_result(tmp_path: Path, scenario: str) -> None:
    runner = tmp_path / "runner.ps1"
    marker = tmp_path / "started.txt"
    expected = 7 if scenario == "test_failure" else 0
    runner.write_text(
        "[System.IO.File]::WriteAllText($env:HF_WRAPPER_TEST_MARKER, (Get-Location).Path)\n"
        f"exit {expected}\n",
        encoding="utf-8",
    )
    backend = tmp_path / "backend"
    backend.mkdir()
    env = {**os.environ, "HF_WRAPPER_TEST_MARKER": str(marker)}
    result = subprocess.run(
        [
            str(PWSH),
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(WRAPPER),
            "-UvPath",
            str(runner if scenario != "missing_runner" else tmp_path / "absent.ps1"),
            "-BackendPath",
            str(backend if scenario != "missing_backend" else tmp_path / "absent"),
            "-LockPath",
            str(tmp_path / "test.lock"),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if scenario in {"missing_runner", "missing_backend"}:
        assert result.returncode != 0, result.stderr
        assert not marker.exists(), "Запуск из неверного каталога запрещён"
    else:
        assert result.returncode == expected, result.stderr
        assert Path(marker.read_text()) == backend
