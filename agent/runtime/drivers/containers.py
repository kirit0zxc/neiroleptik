"""Описание контейнера ловушки и backend, который им управляет.

ContainerSpec проверяет все значения до построения команды: имя, образ, метки и
аргументы не могут содержать управляющих символов или выглядеть как опции docker.
DockerCli вызывает docker списком аргументов без shell, с ограничением времени и объёма
вывода. Живой прогон с настоящим Docker Engine не выполнялся: команды проверены только
на подмене исполнителя.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from infra.process import BoundedRunner, CommandResult, Runner

MANAGED_PREFIX = "honeyforge-"
LABEL_MANAGED = "honeyforge.managed"

_NAME = re.compile(r"honeyforge-[a-z0-9][a-z0-9_.-]{0,100}")
_NETWORK = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}")
_IMAGE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.:/@-]{0,254}")
_LABEL_KEY = re.compile(r"[a-z0-9][a-z0-9_.-]{0,62}")
_USER = re.compile(r"\d{1,10}:\d{1,10}")
_MAX_ARGUMENT = 1024
_MAX_LABEL_VALUE = 256
_LOG_SIZE = re.compile(r"[1-9][0-9]{0,4}[kmg]")
# Сети, отключающие изоляцию или публикацию портов: контейнер ловушки в них не запускается.
_FORBIDDEN_NETWORKS = frozenset({"host", "none"})
# Признаки недоступного демона или сокета. Проверяются раньше признаков отсутствия
# контейнера: «connect: no such file or directory» означает недоступный сокет, а не
# отсутствие контейнера.
_UNAVAILABLE_MARKERS = (
    "cannot connect to the docker daemon",
    "error during connect",
    "is the docker daemon running",
    "docker.sock",
    "docker_engine",
    "permission denied while trying to connect",
)
_MISSING_MARKERS = ("no such container", "no such object")
# Компактный шаблон inspect: только состояние и метки, чтобы ответ не упирался в лимит
# вывода и не зависел от прочих полей.
_INSPECT_FORMAT = '{"State":{{json .State}},"Labels":{{json .Config.Labels}}}'


class BackendError(Exception):
    """Отказ контейнерного backend. Сообщение усечено и не содержит сырого вывода целиком."""

    def __init__(self, message: str, *, unavailable: bool = False) -> None:
        self.message = message
        self.unavailable = unavailable
        super().__init__(message)


def _has_control_character(value: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in value)


@dataclass(frozen=True)
class PortPublish:
    """Публикация порта контейнера на адрес хоста."""

    host_ip: str
    host_port: int
    container_port: int

    def __post_init__(self) -> None:
        ipaddress.ip_address(self.host_ip)
        for port in (self.host_port, self.container_port):
            if type(port) is not int or not 1 <= port <= 65535:
                raise ValueError("порт должен быть целым от 1 до 65535")

    def argument(self) -> str:
        """Формат docker --publish; адрес IPv6 берётся в квадратные скобки."""
        host = self.host_ip
        if ipaddress.ip_address(host).version == 6:
            host = f"[{host}]"
        return f"{host}:{self.host_port}:{self.container_port}"


@dataclass(frozen=True)
class ContainerSpec:
    """Запуск одного контейнера с обязательными мерами ограничения.

    Файловая система только для чтения, без привилегий, без перезапуска средствами Docker
    (решение о перезапуске принимает агент после сверки состояния), с init-процессом.
    """

    name: str
    image: str
    command: tuple[str, ...]
    publish: tuple[PortPublish, ...] = ()
    labels: tuple[tuple[str, str], ...] = ()
    network: str | None = None
    user: str = "65532:65532"
    memory_mb: int = 128
    cpus: float = 0.5
    pids_limit: int = 64
    # Журнал контейнера ограничен так же, как в compose.runtime.yml: события Low в stdout
    # не должны заполнить диск хоста.
    log_max_size: str = "10m"
    log_max_files: int = 3

    def __post_init__(self) -> None:
        if _NAME.fullmatch(self.name) is None:
            raise ValueError("имя контейнера должно начинаться с honeyforge-")
        self.check_image(self.image)
        if self.network is not None:
            self.check_network(self.network)
        if _USER.fullmatch(self.user) is None:
            raise ValueError("пользователь задаётся как uid:gid")
        # Сравнение чисел, а не строк: «00:5» — тоже root.
        if any(int(part) == 0 for part in self.user.split(":")):
            raise ValueError("контейнер не должен работать от root")
        if _LOG_SIZE.fullmatch(self.log_max_size) is None:
            raise ValueError("размер журнала контейнера задаётся как число с k, m или g")
        if type(self.log_max_files) is not int or not 1 <= self.log_max_files <= 20:
            raise ValueError("число файлов журнала контейнера должно быть от 1 до 20")
        if type(self.memory_mb) is not int or not 16 <= self.memory_mb <= 4096:
            raise ValueError("память должна быть от 16 до 4096 МиБ")
        if isinstance(self.cpus, bool) or not 0.1 <= self.cpus <= 8:
            raise ValueError("число CPU должно быть от 0.1 до 8")
        if type(self.pids_limit) is not int or not 1 <= self.pids_limit <= 4096:
            raise ValueError("лимит процессов должен быть от 1 до 4096")
        for argument in self.command:
            if (
                not isinstance(argument, str)
                or len(argument) > _MAX_ARGUMENT
                or _has_control_character(argument)
            ):
                raise ValueError(
                    "недопустимый аргумент команды: тип, длина или управляющие символы"
                )
        for key, value in self.labels:
            if _LABEL_KEY.fullmatch(key) is None:
                raise ValueError("недопустимый ключ метки")
            if len(value) > _MAX_LABEL_VALUE or _has_control_character(value):
                raise ValueError("недопустимое значение метки")
        host_ports = [(item.host_ip, item.host_port) for item in self.publish]
        if len(set(host_ports)) != len(host_ports):
            raise ValueError("публикуемые порты хоста повторяются")

    @staticmethod
    def check_image(image: str) -> None:
        """Имя образа не может выглядеть как опция docker или содержать пробелы."""
        if _IMAGE.fullmatch(image) is None:
            raise ValueError("недопустимое имя образа")

    @staticmethod
    def check_network(network: str) -> None:
        """Сеть задаёт оператор; host и none снимают изоляцию либо публикацию портов."""
        if _NETWORK.fullmatch(network) is None:
            raise ValueError("недопустимое имя сети")
        if network.lower() in _FORBIDDEN_NETWORKS:
            raise ValueError("сети host и none для ловушки запрещены")


@dataclass(frozen=True)
class ContainerObservation:
    """Факты о контейнере из docker inspect."""

    name: str
    status: str
    running: bool
    exit_code: int | None
    labels: Mapping[str, str] = field(default_factory=dict)


class ContainerBackend(Protocol):
    """Минимальный набор операций над контейнерами слота."""

    async def run(self, spec: ContainerSpec) -> None:
        """Создаёт и запускает контейнер; ошибка — BackendError."""
        ...

    async def inspect(self, name: str) -> ContainerObservation | None:
        """Возвращает наблюдение либо None, если контейнера нет."""
        ...

    async def stop(self, name: str, timeout: int) -> None:
        """Мягко останавливает контейнер (SIGTERM, затем SIGKILL по таймауту)."""
        ...

    async def remove(self, name: str) -> None:
        """Останавливает и удаляет контейнер; отсутствующий контейнер — не ошибка."""
        ...


class DockerCli:
    """Backend через консольную утилиту docker; аргументы передаются списком."""

    def __init__(self, runner: Runner | None = None, executable: str = "docker") -> None:
        self._runner: Runner = runner if runner is not None else BoundedRunner(30.0, 16384)
        self._executable = executable

    def run_command(self, spec: ContainerSpec) -> list[str]:
        """Строит аргументы docker run; всё после образа docker передаёт контейнеру как есть."""
        argv = [
            self._executable,
            "run",
            "--detach",
            "--name",
            spec.name,
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--init",
            "--restart",
            "no",
            "--user",
            spec.user,
            "--pids-limit",
            str(spec.pids_limit),
            "--memory",
            f"{spec.memory_mb}m",
            "--cpus",
            f"{spec.cpus:g}",
            "--log-driver",
            "local",
            "--log-opt",
            f"max-size={spec.log_max_size}",
            "--log-opt",
            f"max-file={spec.log_max_files}",
            # Драйвер не скачивает образы: образ закрепляется и доставляется заранее.
            "--pull",
            "never",
        ]
        if spec.network is not None:
            argv += ["--network", spec.network]
        for item in spec.publish:
            argv += ["--publish", item.argument()]
        for key, value in spec.labels:
            argv += ["--label", f"{key}={value}"]
        argv.append(spec.image)
        argv.extend(spec.command)
        return argv

    async def run(self, spec: ContainerSpec) -> None:
        """Запускает контейнер из описания; любой отказ docker — BackendError."""
        result = await self._execute(self.run_command(spec))
        self._require_success(result, "docker run")

    async def inspect(self, name: str) -> ContainerObservation | None:
        """Читает состояние контейнера слота; None, если контейнера нет."""
        self._require_managed_name(name)
        result = await self._execute(
            [self._executable, "inspect", "--type", "container", "--format", _INSPECT_FORMAT, name]
        )
        if self._is_missing(result):
            return None
        self._require_success(result, "docker inspect")
        return self._parse_observation(name, result.output)

    async def stop(self, name: str, timeout: int) -> None:
        """docker stop: SIGTERM процессу ловушки, SIGKILL через timeout секунд."""
        self._require_managed_name(name)
        if type(timeout) is not int or not 0 <= timeout <= 25:
            raise BackendError("таймаут остановки должен быть от 0 до 25 секунд")
        result = await self._execute([self._executable, "stop", "--time", str(timeout), name])
        if self._is_missing(result):
            return
        self._require_success(result, "docker stop")

    async def remove(self, name: str) -> None:
        """Принудительно удаляет контейнер слота вместе с томами; отсутствующий — не ошибка."""
        self._require_managed_name(name)
        result = await self._execute([self._executable, "rm", "--force", "--volumes", name])
        if self._is_missing(result):
            return
        self._require_success(result, "docker rm")

    async def _execute(self, argv: list[str]) -> CommandResult:
        """Блокирующий исполнитель уходит в поток: цикл событий агента не должен ждать docker."""
        return await asyncio.to_thread(self._runner.run, argv)

    @staticmethod
    def _is_unavailable(output: str) -> bool:
        lowered = output.lower()
        return any(marker in lowered for marker in _UNAVAILABLE_MARKERS)

    @classmethod
    def _is_missing(cls, result: CommandResult) -> bool:
        """Контейнера нет — только по точной фразе docker и только при доступном демоне."""
        if result.returncode in (0, None) or result.timed_out or result.truncated:
            return False
        if cls._is_unavailable(result.output):
            return False
        lowered = result.output.lower()
        return any(marker in lowered for marker in _MISSING_MARKERS)

    @staticmethod
    def _require_managed_name(name: str) -> None:
        """Агент никогда не трогает контейнеры, которые не принадлежат HoneyForge."""
        if _NAME.fullmatch(name) is None:
            raise BackendError("имя контейнера не принадлежит HoneyForge")

    @classmethod
    def _require_success(cls, result: CommandResult, action: str) -> None:
        if result.error is not None:
            raise BackendError(f"{action}: команда недоступна", unavailable=True)
        if result.timed_out:
            raise BackendError(f"{action}: истёк таймаут", unavailable=True)
        if result.truncated:
            raise BackendError(f"{action}: превышен лимит вывода")
        if result.returncode != 0:
            tail = " ".join(result.output.split())[-200:]
            raise BackendError(
                f"{action}: код {result.returncode}: {tail}",
                unavailable=cls._is_unavailable(result.output),
            )

    @staticmethod
    def _parse_observation(name: str, output: str) -> ContainerObservation:
        try:
            # stderr docker слит с stdout: предупреждения CLI могут стоять до JSON-строки.
            line = next(item for item in output.splitlines() if item.lstrip().startswith("{"))
            data = json.loads(line)
            state = data["State"]
            labels = data.get("Labels") or {}
            if not isinstance(labels, dict) or not all(
                isinstance(key, str) and isinstance(value, str) for key, value in labels.items()
            ):
                raise ValueError("метки")
            exit_code = state.get("ExitCode")
            return ContainerObservation(
                name=name,
                status=str(state["Status"]),
                running=bool(state["Running"]),
                exit_code=exit_code if type(exit_code) is int else None,
                labels=labels,
            )
        except (StopIteration, ValueError, KeyError, TypeError, AttributeError) as error:
            raise BackendError("docker inspect: неожиданный формат ответа") from error
