"""Проверяет статические предпосылки runtime без изменения хоста и сети."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import stat
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Literal, Protocol

from infra.process import BoundedRunner, Runner

Status = Literal["PASS", "FAIL", "UNKNOWN"]


class Probe(Protocol):
    """Чтение окружения отделено от логики для воспроизводимой проверки без KVM."""

    def system(self) -> str:
        """Название операционной системы, как у platform.system()."""
        ...

    def is_character_device(self, path: str) -> bool:
        """True, если путь — символьное устройство; ошибки чтения передаются вызывающему."""
        ...

    def can_read_write(self, path: str) -> bool:
        """True, если текущий пользователь может читать и писать по пути."""
        ...

    def which(self, name: str) -> str | None:
        """Полный путь к исполняемому файлу в PATH либо None."""
        ...


class SystemProbe:
    """Читает свойства текущего хоста и прав текущего пользователя."""

    def system(self) -> str:
        """Название операционной системы текущего хоста."""
        return platform.system()

    def is_character_device(self, path: str) -> bool:
        """Определяет по режиму файла, что путь — символьное устройство."""
        return stat.S_ISCHR(os.stat(path).st_mode)

    def can_read_write(self, path: str) -> bool:
        """Проверяет права текущего пользователя на чтение и запись."""
        return os.access(path, os.R_OK | os.W_OK)

    def which(self, name: str) -> str | None:
        """Ищет исполняемый файл в PATH."""
        return shutil.which(name)


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
        """True, только если все статические проверки имеют статус PASS."""
        return all(check.status == "PASS" for check in self.checks)

    def as_dict(self) -> dict[str, object]:
        """Отчёт в виде словаря для JSON; живой smoke всегда помечен невыполненным."""
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
        """Выполняет все статические проверки; на неподдерживаемой ОС команды не запускаются."""
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
