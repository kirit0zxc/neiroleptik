"""Проверки ограниченного запуска внешних команд на настоящих процессах."""

from __future__ import annotations

import sys
import time

import pytest

from infra.process import BoundedRunner


def test_real_runner_preserves_literal_arguments() -> None:
    result = BoundedRunner().run(
        [
            sys.executable,
            "-c",
            "import sys; print(sys.argv[1])",
            "literal;$(not-a-command)",
        ]
    )
    assert result.returncode == 0
    assert result.output.strip() == "literal;$(not-a-command)"
    assert not result.timed_out
    assert not result.truncated


def test_real_runner_bounds_combined_output() -> None:
    result = BoundedRunner(output_limit=128).run(
        [
            sys.executable,
            "-c",
            "import sys; sys.stderr.write('x' * 100000); sys.stderr.flush()",
        ]
    )
    assert result.truncated
    assert len(result.output) == 128
    assert not result.error


def test_real_runner_times_out_and_returns_promptly() -> None:
    started = time.monotonic()
    result = BoundedRunner(timeout=0.15).run(
        [
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
        ]
    )
    assert result.timed_out
    assert time.monotonic() - started < 3.5


def test_real_runner_reports_spawn_failure() -> None:
    result = BoundedRunner().run(["/nonexistent/honeyforge-preflight-tool"])
    assert result.error
    assert result.returncode is None


@pytest.mark.parametrize("timeout", [0, -1, 31, float("nan"), float("inf"), True, "5", None])
def test_invalid_timeout_rejected(timeout: float) -> None:
    with pytest.raises(ValueError):
        BoundedRunner(timeout=timeout)


@pytest.mark.parametrize("limit", [0, -1, 65537, True, 1.5, "128", None])
def test_invalid_output_limit_rejected(limit: int) -> None:
    with pytest.raises(ValueError):
        BoundedRunner(output_limit=limit)
