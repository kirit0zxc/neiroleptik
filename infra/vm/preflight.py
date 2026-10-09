"""Проверяет статические предпосылки runtime без изменения хоста и сети."""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import shutil
import signal
import stat
import subprocess
import sys
import threading
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Literal, Protocol

Status = Literal["PASS", "FAIL", "UNKNOWN"]


@dataclass(frozen=True)
class CommandResult:
    """Ограниченный вывод команды; UNKNOWN отличает неполное наблюдение от успеха."""

    returncode: int | None
    output: str = ""
    timed_out: bool = False
    truncated: bool = False
    error: str | None = None


class Probe(Protocol):
    """Чтение окружения отделено от логики для воспроизводимой проверки без KVM."""

    def system(self) -> str: ...

    def is_character_device(self, path: str) -> bool: ...

    def can_read_write(self, path: str) -> bool: ...

    def which(self, name: str) -> str | None: ...


class Runner(Protocol):
    """Исполнитель принимает только готовый список аргументов диагностической команды."""

    def run(self, command: Sequence[str]) -> CommandResult: ...


class SystemProbe:
    """Читает свойства текущего хоста и прав текущего пользователя."""

    def system(self) -> str:
        return platform.system()

    def is_character_device(self, path: str) -> bool:
        return stat.S_ISCHR(os.stat(path).st_mode)

    def can_read_write(self, path: str) -> bool:
        return os.access(path, os.R_OK | os.W_OK)

    def which(self, name: str) -> str | None:
        return shutil.which(name)


class BoundedRunner:
    """Ограничивает время и общий объём stdout/stderr, не используя shell."""

    def __init__(self, timeout: float = 5.0, output_limit: int = 8192) -> None:
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or not 0 < timeout <= 30
        ):
            raise ValueError("Таймаут должен быть больше 0 и не больше 30 секунд")
        if (
            isinstance(output_limit, bool)
            or not isinstance(output_limit, int)
            or not 1 <= output_limit <= 65536
        ):
            raise ValueError("Лимит вывода должен быть от 1 до 65536 байт")
        self.timeout = timeout
        self.output_limit = output_limit

    @staticmethod
    def _kill(process: subprocess.Popen[bytes]) -> None:
        """На POSIX завершает также потомков диагностической команды при отказе.

        Проверка идёт по sys.platform, а не по os.name: именно такое условие
        понимает mypy, поэтому типы проверяются одинаково на Windows и Linux.
        """
        try:
            if sys.platform == "win32":
                process.kill()
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def run(self, command: Sequence[str]) -> CommandResult:
        try:
            process = subprocess.Popen(
                list(command),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                shell=False,
                start_new_session=sys.platform != "win32",
            )
        except OSError as exc:
            return CommandResult(None, error=str(exc))

        captured = bytearray()
        exceeded = threading.Event()
        read_errors: list[str] = []
        stream = process.stdout
        assert stream is not None

        def read_output() -> None:
            # Читаем не более лимита плюс один байт: communicate() копит весь
            # вывод в памяти и не подходит для диагностики недоверенного хоста.
            try:
                while True:
                    chunk = stream.read(min(4096, self.output_limit + 1 - len(captured)))
                    if not chunk:
                        return
                    captured.extend(chunk)
                    if len(captured) > self.output_limit:
                        exceeded.set()
                        self._kill(process)
                        return
            except OSError as exc:
                read_errors.append(str(exc))
            finally:
                stream.close()

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        timed_out = False
        try:
            process.wait(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._kill(process)
            process.wait(timeout=1.0)
        reader.join(timeout=1.0)
        if reader.is_alive():
            # Потомок мог унаследовать канал вывода после выхода родителя.
            self._kill(process)
            reader.join(timeout=1.0)
            read_errors.append("Канал вывода не закрылся после завершения команды")
        return CommandResult(
            process.returncode,
            bytes(captured[: self.output_limit]).decode("utf-8", errors="replace"),
            timed_out,
            exceeded.is_set(),
            "; ".join(read_errors) or None,
        )


@dataclass(frozen=True)
class CheckResult:
    """Один наблюдаемый факт; наличие инструмента не означает рабочую изоляцию."""

    check: str
    status: Status
    message: str
    command: tuple[str, ...] = ()
    output: str = ""


@dataclass(frozen=True)
class PreflightReport:
    """Статическая готовность и ещё не выполненные живые проверки разделены."""

    system: str
    checks: tuple[CheckResult, ...]
    manual_checks: tuple[CheckResult, ...]

    @property
    def static_ready(self) -> bool:
        return all(check.status == "PASS" for check in self.checks)

    def as_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["static_ready"] = self.static_ready
        result["live_smoke_verified"] = False
        return result


class RuntimePreflight:
    """Собирает безопасную диагностику; никогда не запускает и не изменяет VM."""

    _dependent_checks = (
        "kvm_device",
        "kvm_access",
        "qemu_img",
        "virsh_version",
        "libvirt_connection",
        "ip",
        "firewall",
    )

    def __init__(self, probe: Probe | None = None, runner: Runner | None = None) -> None:
        self.probe = probe if probe is not None else SystemProbe()
        self.runner = runner if runner is not None else BoundedRunner()

    @staticmethod
    def _manual_checks() -> tuple[CheckResult, ...]:
        return (
            CheckResult("vm_creation", "UNKNOWN", "Нужен живой smoke создания и запуска VM"),
            CheckResult("network_permissions", "UNKNOWN", "Нужен согласованный smoke сетевых прав"),
            CheckResult("network_isolation", "UNKNOWN", "Нужны отрицательные проверки IPv4/IPv6"),
        )

    def _command(self, name: str, binary: str | None, *args: str) -> CheckResult:
        if binary is None:
            return CheckResult(name, "FAIL", "Не найден необходимый исполняемый файл")
        command = (binary, *args)
        result = self.runner.run(command)
        if result.timed_out:
            status: Status = "UNKNOWN"
            message = "Истёк таймаут команды; предпосылка не подтверждена"
        elif result.truncated:
            status = "UNKNOWN"
            message = "Превышен лимит вывода; команда остановлена"
        elif result.error is not None:
            status = "UNKNOWN"
            message = "Не удалось выполнить диагностику: " + result.error
        elif result.returncode != 0:
            status = "FAIL"
            message = f"Команда завершилась с кодом {result.returncode}"
        elif not result.output.strip():
            status = "UNKNOWN"
            message = "Команда не вернула диагностический результат"
        elif name == "libvirt_connection" and "qemu:///system" not in result.output.splitlines():
            status = "UNKNOWN"
            message = "Не подтверждён URI системного подключения libvirt"
        else:
            status = "PASS"
            message = "Статическая предпосылка подтверждена; живой smoke не выполнялся"
        return CheckResult(name, status, message, command, result.output)

    def run(self) -> PreflightReport:
        system = self.probe.system()
        if system != "Linux":
            checks: tuple[CheckResult, ...] = (
                CheckResult("linux", "FAIL", f"Нужен Linux runtime; обнаружено: {system}"),
            )
            checks += tuple(
                CheckResult(name, "UNKNOWN", "Проверка пропущена на неподдерживаемой ОС")
                for name in self._dependent_checks
            )
            return PreflightReport(system, checks, self._manual_checks())

        checks_list = [CheckResult("linux", "PASS", "Обнаружен Linux")]
        try:
            is_device = self.probe.is_character_device("/dev/kvm")
        except FileNotFoundError:
            is_device = False
        except OSError as exc:
            is_device = False
            checks_list.append(CheckResult("kvm_device", "UNKNOWN", str(exc)))
        if len(checks_list) == 1:
            checks_list.append(
                CheckResult(
                    "kvm_device",
                    "PASS" if is_device else "FAIL",
                    "/dev/kvm — символьное устройство"
                    if is_device
                    else "/dev/kvm отсутствует или не устройство",
                )
            )
        if not is_device:
            checks_list.append(CheckResult("kvm_access", "UNKNOWN", "Нет подтверждённого /dev/kvm"))
        else:
            try:
                accessible = self.probe.can_read_write("/dev/kvm")
                checks_list.append(
                    CheckResult(
                        "kvm_access",
                        "PASS" if accessible else "FAIL",
                        "Есть доступ на чтение/запись"
                        if accessible
                        else "Нет доступа на чтение/запись",
                    )
                )
            except OSError as exc:
                checks_list.append(CheckResult("kvm_access", "UNKNOWN", str(exc)))

        qemu = self.probe.which("qemu-img")
        virsh = self.probe.which("virsh")
        checks_list.append(self._command("qemu_img", qemu, "--version"))
        checks_list.append(self._command("virsh_version", virsh, "--version"))
        checks_list.append(
            self._command("libvirt_connection", virsh, "-c", "qemu:///system", "uri")
        )
        checks_list.append(self._command("ip", self.probe.which("ip"), "-Version"))
        firewall = self.probe.which("nft") or self.probe.which("iptables")
        checks_list.append(self._command("firewall", firewall, "--version"))
        return PreflightReport(system, tuple(checks_list), self._manual_checks())


def main(argv: Sequence[str] | None = None) -> int:
    """Печатает JSON; успешная статика не заменяет три обязательных живых smoke."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--output-limit", type=int, default=8192)
    args = parser.parse_args(argv)
    try:
        runner = BoundedRunner(args.timeout, args.output_limit)
    except ValueError as exc:
        parser.error(str(exc))
    report = RuntimePreflight(runner=runner).run()
    print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    return 0 if report.static_ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
