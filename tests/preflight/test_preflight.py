"""Проверяет диагностику на фикстурах и ограничения настоящего subprocess."""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Sequence

import pytest

from infra.vm.preflight import BoundedRunner, CommandResult, RuntimePreflight, main


class FakeProbe:
    """Моделирует Linux/KVM без доступа к реальному хосту."""

    def __init__(self, system: str = "Linux") -> None:
        self.os_name = system
        self.device = True
        self.access = True
        self.device_error: OSError | None = None
        self.binaries = {
            name: f"/usr/bin/{name}" for name in ("qemu-img", "virsh", "ip", "nft", "iptables")
        }
        self.reads: list[str] = []

    def system(self) -> str:
        return self.os_name

    def is_character_device(self, path: str) -> bool:
        self.reads.append(path)
        if self.device_error is not None:
            raise self.device_error
        return self.device

    def can_read_write(self, path: str) -> bool:
        self.reads.append(path)
        return self.access

    def which(self, name: str) -> str | None:
        self.reads.append(name)
        return self.binaries.get(name)


class FakeRunner:
    """Возвращает заданный результат, сохраняя точный список команд."""

    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.results: dict[tuple[str, ...], CommandResult] = {}

    def run(self, command: Sequence[str]) -> CommandResult:
        args = tuple(command)
        self.commands.append(args)
        output = "qemu:///system\n" if args[-1] == "uri" else "test version 1\n"
        return self.results.get(args, CommandResult(0, output))


def test_supported_static_checks_do_not_claim_live_readiness() -> None:
    runner = FakeRunner()
    report = RuntimePreflight(FakeProbe(), runner).run()

    assert report.static_ready
    assert len(report.checks) == 8
    assert {check.status for check in report.checks} == {"PASS"}
    assert {check.check for check in report.manual_checks} == {
        "vm_creation", "network_permissions", "network_isolation",
    }
    assert {check.status for check in report.manual_checks} == {"UNKNOWN"}
    assert report.as_dict()["live_smoke_verified"] is False
    assert runner.commands == [
        ("/usr/bin/qemu-img", "--version"),
        ("/usr/bin/virsh", "--version"),
        ("/usr/bin/virsh", "-c", "qemu:///system", "uri"),
        ("/usr/bin/ip", "-Version"),
        ("/usr/bin/nft", "--version"),
    ]


@pytest.mark.parametrize("system", ["Windows", "Darwin", "Unknown"])
def test_unsupported_os_does_not_probe_linux_or_run_commands(system: str) -> None:
    probe, runner = FakeProbe(system), FakeRunner()
    report = RuntimePreflight(probe, runner).run()

    assert not report.static_ready
    assert report.checks[0].status == "FAIL"
    assert {check.status for check in report.checks[1:]} == {"UNKNOWN"}
    assert not runner.commands
    assert not probe.reads


@pytest.mark.parametrize("device_error", [None, FileNotFoundError("missing")])
def test_missing_or_regular_kvm_is_not_ready(device_error: OSError | None) -> None:
    probe = FakeProbe()
    probe.device = False
    probe.device_error = device_error
    report = RuntimePreflight(probe, FakeRunner()).run()
    checks = {check.check: check for check in report.checks}

    assert not report.static_ready
    assert checks["kvm_device"].status == "FAIL"
    assert checks["kvm_access"].status == "UNKNOWN"


def test_kvm_permission_denial_is_not_ready() -> None:
    probe = FakeProbe()
    probe.access = False
    report = RuntimePreflight(probe, FakeRunner()).run()
    assert not report.static_ready
    assert next(check for check in report.checks if check.check == "kvm_access").status == "FAIL"


def test_unreadable_kvm_metadata_is_unknown() -> None:
    probe = FakeProbe()
    probe.device_error = PermissionError("denied")
    report = RuntimePreflight(probe, FakeRunner()).run()
    assert not report.static_ready
    assert next(check for check in report.checks if check.check == "kvm_device").status == "UNKNOWN"


def test_missing_tools_fail_without_attempted_execution() -> None:
    probe, runner = FakeProbe(), FakeRunner()
    probe.binaries.clear()
    report = RuntimePreflight(probe, runner).run()

    assert not report.static_ready
    assert not runner.commands
    checks = {check.check: check.status for check in report.checks}
    assert all(checks[name] == "FAIL" for name in (
        "qemu_img", "virsh_version", "libvirt_connection", "ip", "firewall",
    ))


def test_iptables_is_read_only_fallback_for_nft() -> None:
    probe, runner = FakeProbe(), FakeRunner()
    del probe.binaries["nft"]
    report = RuntimePreflight(probe, runner).run()
    assert report.static_ready
    assert runner.commands[-1] == ("/usr/bin/iptables", "--version")


@pytest.mark.parametrize(("result", "expected"), [
    (CommandResult(1, "permission denied"), "FAIL"),
    (CommandResult(-9, timed_out=True), "UNKNOWN"),
    (CommandResult(-9, "partial", truncated=True), "UNKNOWN"),
    (CommandResult(None, error="cannot execute"), "UNKNOWN"),
    (CommandResult(0, ""), "UNKNOWN"),
    (CommandResult(0, "qemu:///session\n"), "UNKNOWN"),
])
def test_failed_or_incomplete_libvirt_probe_never_passes(
    result: CommandResult, expected: str,
) -> None:
    runner = FakeRunner()
    runner.results[("/usr/bin/virsh", "-c", "qemu:///system", "uri")] = result
    report = RuntimePreflight(FakeProbe(), runner).run()
    check = next(check for check in report.checks if check.check == "libvirt_connection")
    assert check.status == expected
    assert not report.static_ready


def test_real_runner_preserves_literal_arguments() -> None:
    result = BoundedRunner().run([
        sys.executable, "-c", "import sys; print(sys.argv[1])", "literal;$(not-a-command)",
    ])
    assert result.returncode == 0
    assert result.output.strip() == "literal;$(not-a-command)"
    assert not result.timed_out
    assert not result.truncated


def test_real_runner_bounds_combined_output() -> None:
    result = BoundedRunner(output_limit=128).run([
        sys.executable, "-c", "import sys; sys.stderr.write('x' * 100000); sys.stderr.flush()",
    ])
    assert result.truncated
    assert len(result.output) == 128
    assert not result.error


def test_real_runner_times_out_and_returns_promptly() -> None:
    started = time.monotonic()
    result = BoundedRunner(timeout=0.15).run([
        sys.executable, "-c", "import time; time.sleep(30)",
    ])
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


def test_cli_returns_json_and_failure_on_unsupported_os(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("infra.vm.preflight.platform.system", lambda: "Windows")
    assert main([]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["system"] == "Windows"
    assert payload["static_ready"] is False
    assert payload["live_smoke_verified"] is False


def test_cli_returns_zero_for_static_success_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("infra.vm.preflight.SystemProbe", FakeProbe)
    monkeypatch.setattr("infra.vm.preflight.BoundedRunner", lambda *_: FakeRunner())
    assert main([]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["static_ready"] is True
    assert payload["live_smoke_verified"] is False
    assert {check["status"] for check in payload["manual_checks"]} == {"UNKNOWN"}
