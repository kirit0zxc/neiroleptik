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
from pathlib import PurePosixPath, PureWindowsPath
from typing import Protocol

from infra.process import BoundedRunner, CommandResult, Runner

MANAGED_PREFIX = "honeyforge-"
LABEL_MANAGED = "honeyforge.managed"

_NAME = re.compile(r"honeyforge-[a-z0-9][a-z0-9_.-]{0,100}")
_NETWORK = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}")
_IMAGE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.:/@-]{0,254}")
_LABEL_KEY = re.compile(r"[a-z0-9][a-z0-9_.-]{0,62}")
_USER = re.compile(r"\d{1,10}:\d{1,10}")
# Путь к каталогу хоста попадает в одно значение --mount, разобранное как CSV: запятые,
# кавычки и "=" в нём недопустимы.
_MOUNT_SOURCE = re.compile(r"[A-Za-z0-9_.:/\\ +@()-]{1,512}")
_MOUNT_TARGET = re.compile(r"/[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*")
_MAX_ARGUMENT = 1024
_MAX_LABEL_VALUE = 256


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
class BindMount:
    """Каталог хоста, доступный контейнеру на запись.

    Единственный разрешённый вид монтирования: корневая файловая система остаётся только
    для чтения, поэтому контейнер пишет лишь сюда (например, журнал для сборщика).
    """

    source: str
    target: str

    def __post_init__(self) -> None:
        absolute = (
            PurePosixPath(self.source).is_absolute() or PureWindowsPath(self.source).is_absolute()
        )
        parts = re.split(r"[\\/]", self.source)
        if _MOUNT_SOURCE.fullmatch(self.source) is None or not absolute or ".." in parts:
            raise ValueError("недопустимый путь каталога хоста")
        if _MOUNT_TARGET.fullmatch(self.target) is None or ".." in self.target.split("/"):
            raise ValueError("недопустимый путь внутри контейнера")

    def argument(self) -> str:
        """Значение docker --mount."""
        return f"type=bind,source={self.source},target={self.target}"


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
    mounts: tuple[BindMount, ...] = ()
    labels: tuple[tuple[str, str], ...] = ()
    network: str | None = None
    user: str = "65532:65532"
    memory_mb: int = 128
    cpus: float = 0.5
    pids_limit: int = 64

    def __post_init__(self) -> None:
        if _NAME.fullmatch(self.name) is None:
            raise ValueError("имя контейнера должно начинаться с honeyforge-")
        if _IMAGE.fullmatch(self.image) is None:
            raise ValueError("недопустимое имя образа")
        if self.network is not None and _NETWORK.fullmatch(self.network) is None:
            raise ValueError("недопустимое имя сети")
        if _USER.fullmatch(self.user) is None:
            raise ValueError("пользователь задаётся как uid:gid")
        if self.user.startswith("0:") or self.user.endswith(":0"):
            raise ValueError("контейнер не должен работать от root")
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
        targets = [mount.target for mount in self.mounts]
        if len(set(targets)) != len(targets):
            raise ValueError("цели монтирования повторяются")
        host_ports = [(item.host_ip, item.host_port) for item in self.publish]
        if len(set(host_ports)) != len(host_ports):
            raise ValueError("публикуемые порты хоста повторяются")


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
        ]
        if spec.network is not None:
            argv += ["--network", spec.network]
        for item in spec.publish:
            argv += ["--publish", item.argument()]
        for mount in spec.mounts:
            argv += ["--mount", mount.argument()]
        for key, value in spec.labels:
            argv += ["--label", f"{key}={value}"]
        argv.append(spec.image)
        argv.extend(spec.command)
        return argv

    async def run(self, spec: ContainerSpec) -> None:
        result = await self._execute(self.run_command(spec))
        self._require_success(result, "docker run")

    async def inspect(self, name: str) -> ContainerObservation | None:
        self._require_managed_name(name)
        result = await self._execute(
            [self._executable, "inspect", "--type", "container", "--format", "{{json .}}", name]
        )
        if result.returncode not in (0, None) and "no such" in result.output.lower():
            return None
        self._require_success(result, "docker inspect")
        return self._parse_observation(name, result.output)

    async def remove(self, name: str) -> None:
        self._require_managed_name(name)
        result = await self._execute([self._executable, "rm", "--force", "--volumes", name])
        if result.returncode not in (0, None) and "no such" in result.output.lower():
            return
        self._require_success(result, "docker rm")

    async def _execute(self, argv: list[str]) -> CommandResult:
        """Блокирующий исполнитель уходит в поток: цикл событий агента не должен ждать docker."""
        return await asyncio.to_thread(self._runner.run, argv)

    @staticmethod
    def _require_managed_name(name: str) -> None:
        """Агент никогда не трогает контейнеры, которые не принадлежат HoneyForge."""
        if _NAME.fullmatch(name) is None:
            raise BackendError("имя контейнера не принадлежит HoneyForge")

    @staticmethod
    def _require_success(result: CommandResult, action: str) -> None:
        if result.error is not None:
            raise BackendError(f"{action}: команда недоступна", unavailable=True)
        if result.timed_out:
            raise BackendError(f"{action}: истёк таймаут", unavailable=True)
        if result.truncated:
            raise BackendError(f"{action}: превышен лимит вывода")
        if result.returncode != 0:
            tail = " ".join(result.output.split())[-200:]
            lowered = result.output.lower()
            daemon_down = "cannot connect" in lowered or (
                "daemon" in lowered and "running" in lowered
            )
            raise BackendError(
                f"{action}: код {result.returncode}: {tail}", unavailable=daemon_down
            )

    @staticmethod
    def _parse_observation(name: str, output: str) -> ContainerObservation:
        try:
            data = json.loads(output)
            state = data["State"]
            labels = data.get("Config", {}).get("Labels") or {}
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
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            raise BackendError("docker inspect: неожиданный формат ответа") from error
