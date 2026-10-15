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
import os
import re
import sys
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath, PureWindowsPath
from typing import Protocol

from infra.process import BoundedRunner, CommandResult, Runner

MANAGED_PREFIX = "honeyforge-"
LABEL_MANAGED = "honeyforge.managed"

_NAME = re.compile(r"honeyforge-[a-z0-9][a-z0-9_.-]{0,100}")
_CONTAINER_ID = re.compile(r"[a-f0-9]{64}")
_NETWORK = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}")
_IMAGE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.:/@-]{0,254}")
_LABEL_KEY = re.compile(r"[a-z0-9][a-z0-9_.-]{0,62}")
_USER = re.compile(r"\d{1,10}:\d{1,10}")
# Путь к каталогу хоста попадает в одно значение --mount, которое docker разбирает как
# CSV: запятая разделяет поля, кавычка начинает экранированное поле. «=» безопасен: ключ
# отделяется по первому знаку. Пробелы и не-ASCII допустимы.
_MOUNT_FORBIDDEN_CHARACTERS = frozenset(",\"'")
_MAX_MOUNT_SOURCE = 512
# Каталоги, монтирование которых открыло бы контейнеру сокет Docker (вместе с корнем).
_MOUNT_FORBIDDEN_SOURCES = frozenset({"/run", "/var", "/var/run"})
_MOUNT_TARGET = re.compile(r"/[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*")
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
# Компактный шаблон inspect: неизменяемый ID, состояние и метки. Удаление по ID сохраняет
# контейнер, который занял то же имя после наблюдения.
_INSPECT_FORMAT = '{"Id":{{json .Id}},"State":{{json .State}},"Labels":{{json .Config.Labels}}}'
# Наблюдение по полному ID дополнительно возвращает имя: вызывающий его не знает.
_INSPECT_BY_ID_FORMAT = (
    '{"Id":{{json .Id}},"State":{{json .State}},"Labels":{{json .Config.Labels}},'
    '"Name":{{json .Name}}}'
)
# Идентичность демона и версия сервера; другой daemon делает исход неизвестным.
_ENGINE_FORMAT = '{"id":{{json .ID}},"version":{{json .ServerVersion}}}'
# Только записываемые mounts: их источники сверяются с каталогом журнала слота.
_MOUNTS_FORMAT = '{"id":{{json .Id}},"mounts":{{json .Mounts}}}'
# Сверка доступа ограничена: при большем числе контейнеров исход считается неизвестным.
_MAX_SWEPT_CONTAINERS = 256
# Размер пачки docker inspect: ответ должен помещаться в предел вывода исполнителя.
_SWEEP_BATCH = 8


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
    """Каталог хоста с явным режимом доступа контейнера.

    По умолчанию каталог записываемый, как журнал Low. Read-only подходит для отдельной
    конфигурации; ограничения пути сохраняются и не разрешают доступ к сокету Docker.
    Режим не запрещает изменения источника на хосте и не обещает защиту вложенных mounts.
    """

    source: str
    target: str
    read_only: bool = False

    def __post_init__(self) -> None:
        if type(self.read_only) is not bool:
            raise ValueError("режим read_only должен быть bool")
        self.check_source(self.source)
        if _MOUNT_TARGET.fullmatch(self.target) is None or ".." in self.target.split("/"):
            raise ValueError("недопустимый путь внутри контейнера")

    @staticmethod
    def check_source(source: str) -> None:
        """Проверяет каталог хоста: абсолютный, без «..», не корень и не путь к сокету Docker.

        Управляющие и форматирующие символы Unicode (в том числе невидимые и меняющие
        направление текста) запрещены: путь попадает в аргументы docker и в журналы.
        """
        if not 1 <= len(source) <= _MAX_MOUNT_SOURCE or any(
            char in _MOUNT_FORBIDDEN_CHARACTERS or unicodedata.category(char).startswith("C")
            for char in source
        ):
            raise ValueError("недопустимый путь каталога хоста")
        posix, windows = PurePosixPath(source), PureWindowsPath(source)
        path = posix if posix.is_absolute() else windows if windows.is_absolute() else None
        if path is None or ".." in re.split(r"[\\/]", source):
            raise ValueError("путь каталога хоста должен быть абсолютным и без «..»")
        normalized = "/" + "/".join(posix.parts[1:]) if path is posix else None
        if (
            path.parent == path
            or path.name.lower() == "docker.sock"
            or normalized in _MOUNT_FORBIDDEN_SOURCES
        ):
            raise ValueError("каталог хоста не может быть корнем или открывать сокет Docker")

    def argument(self) -> str:
        """Одно значение docker --mount; readonly добавляется только в явном режиме."""
        argument = f"type=bind,source={self.source},target={self.target}"
        return argument + ",readonly" if self.read_only else argument


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
        targets = [mount.target for mount in self.mounts]
        if len(set(targets)) != len(targets):
            raise ValueError("цели монтирования повторяются")
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
    # Старые подмены backend могут создавать наблюдение без ID, но удалять его нельзя.
    container_id: str | None = None


class ContainerBackend(Protocol):
    """Минимальный набор операций над контейнерами слота."""

    async def run(self, spec: ContainerSpec) -> None:
        """Создаёт и запускает контейнер; ошибка — BackendError."""
        ...

    async def inspect(self, name: str) -> ContainerObservation | None:
        """Возвращает наблюдение либо None, если контейнера нет."""
        ...

    async def stop(self, name: str | ContainerObservation, timeout: int) -> None:
        """Мягко останавливает контейнер; наблюдение адресуется только по полному ID."""
        ...

    async def remove(self, name: str | ContainerObservation) -> None:
        """Удаляет контейнер по имени либо ID наблюдения; отсутствующий — не ошибка."""
        ...


class DockerCli:
    """Backend через консольную утилиту docker; аргументы передаются списком."""

    def __init__(self, runner: Runner | None = None, executable: str = "docker") -> None:
        self._runner: Runner = runner if runner is not None else BoundedRunner(30.0, 16384)
        self._executable = executable

    def run_command(self, spec: ContainerSpec) -> list[str]:
        """Строит аргументы docker run; всё после образа docker передаёт контейнеру как есть."""
        return self._lifecycle_command("run", spec, detach=True)

    def create_command(self, spec: ContainerSpec) -> list[str]:
        """Строит аргументы docker create: те же меры ограничения, но без запуска.

        Создание отделено от запуска, потому что доверенная граница процесса сохраняет
        полный Docker ID из ответа Engine на create **до** первого start (§8.1).
        """
        return self._lifecycle_command("create", spec, detach=False)

    def _lifecycle_command(self, action: str, spec: ContainerSpec, *, detach: bool) -> list[str]:
        """Общая часть run и create; отличаются только подкоманда и --detach."""
        argv = [
            self._executable,
            action,
            *(["--detach"] if detach else []),
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
        for mount in spec.mounts:
            argv += ["--mount", mount.argument()]
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

    async def stop(self, name: str | ContainerObservation, timeout: int) -> None:
        """docker stop: SIGTERM процессу ловушки, SIGKILL через timeout секунд."""
        target = self._operation_target(name)
        if type(timeout) is not int or not 0 <= timeout <= 25:
            raise BackendError("таймаут остановки должен быть от 0 до 25 секунд")
        result = await self._execute([self._executable, "stop", "--time", str(timeout), target])
        if self._is_missing(result):
            return
        self._require_success(result, "docker stop")

    async def remove(self, name: str | ContainerObservation) -> None:
        """Принудительно удаляет контейнер слота вместе с томами; отсутствующий — не ошибка."""
        target = self._operation_target(name)
        result = await self._execute([self._executable, "rm", "--force", "--volumes", target])
        if self._is_missing(result):
            return
        self._require_success(result, "docker rm")

    async def create(self, spec: ContainerSpec) -> str:
        """Создаёт контейнер без запуска и возвращает его полный Docker ID.

        ID берётся из ответа Engine, а не из имени: имя может быть занято другим
        контейнером сразу после наблюдения, а полный ID неизменен.
        """
        result = await self._execute(self.create_command(spec))
        self._require_success(result, "docker create")
        for line in reversed(result.output.splitlines()):
            candidate = line.strip()
            if _CONTAINER_ID.fullmatch(candidate) is not None:
                return candidate
        raise BackendError("docker create: ответ не содержит полного Docker ID")

    async def start_created(self, container_id: str) -> None:
        """Запускает уже созданный контейнер по полному ID; исчезновение ID — ошибка."""
        target = self._require_container_id(container_id)
        result = await self._execute([self._executable, "start", target])
        self._require_success(result, "docker start")

    async def inspect_by_id(self, container_id: str) -> ContainerObservation | None:
        """Наблюдает контейнер по неизменяемому ID; None — такого контейнера нет.

        Имя приходит из ответа Engine и обязано принадлежать пространству HoneyForge:
        контейнеры вне него агент не наблюдает и не трогает.
        """
        target = self._require_container_id(container_id)
        result = await self._execute(
            [
                self._executable,
                "inspect",
                "--type",
                "container",
                "--format",
                _INSPECT_BY_ID_FORMAT,
                target,
            ]
        )
        if self._is_missing(result):
            return None
        self._require_success(result, "docker inspect")
        name = self._parse_name(result.output)
        self._require_managed_name(name)
        observation = self._parse_observation(name, result.output)
        if observation.container_id != target:
            raise BackendError("docker inspect: ответ относится к другому контейнеру")
        return observation

    async def containers_by_label(self, key: str, value: str) -> tuple[str, ...]:
        """Полные ID контейнеров с указанной меткой, включая остановленные.

        Нужен восстановлению: create, полный ID которого не успел сохраниться, ищется
        по метке writer_id. Несколько совпадений означают неизвестный исход, поэтому
        фильтр возвращает весь список, а решение принимает вызывающая сторона.
        """
        if _LABEL_KEY.fullmatch(key) is None:
            raise BackendError("недопустимый ключ метки")
        if not 1 <= len(value) <= _MAX_LABEL_VALUE or _has_control_character(value):
            raise BackendError("недопустимое значение метки")
        result = await self._execute(
            [
                self._executable,
                "ps",
                "--all",
                "--no-trunc",
                "--filter",
                f"label={key}={value}",
                "--format",
                "{{.ID}}",
            ]
        )
        self._require_success(result, "docker ps")
        found = tuple(
            line.strip()
            for line in result.output.splitlines()
            if _CONTAINER_ID.fullmatch(line.strip()) is not None
        )
        if len(found) > _MAX_SWEPT_CONTAINERS:
            raise BackendError("docker ps: слишком много контейнеров с этой меткой")
        return found

    async def engine_identity(self) -> str:
        """Идентичность Engine и демона: другой daemon означает неизвестный исход (§2)."""
        result = await self._execute([self._executable, "info", "--format", _ENGINE_FORMAT])
        self._require_success(result, "docker info")
        try:
            line = next(
                item for item in result.output.splitlines() if item.lstrip().startswith("{")
            )
            data = json.loads(line)
            identity = f"{data['id']}/{data['version']}"
        except (StopIteration, ValueError, KeyError, TypeError) as error:
            raise BackendError("docker info: неожиданный формат ответа") from error
        if not 3 <= len(identity) <= 256 or _has_control_character(identity):
            raise BackendError("docker info: недопустимая идентичность Engine")
        return identity

    async def writable_mount_holders(self, source: str) -> tuple[str, ...]:
        """Полные ID контейнеров, у которых есть запись в каталог хоста или его предка.

        Управление одним именем слота не доказывает отсутствие других контейнеров с RW
        доступом к тому же журналу (§2), поэтому ограждение сверяет весь список Engine.
        Пересечением считается совпадение каталога, его вложенность в источник mount
        или обратная вложенность. Сравнение работает с путями хоста, как их сообщает
        Engine. На Windows Docker Desktop сообщает источник в виде `C:/...`, который
        не является путём POSIX, поэтому сверка там не выполняется и явно отказывает:
        молчаливый пустой список выглядел бы как подтверждённое ограждение.
        """
        BindMount.check_source(source)
        if not PurePosixPath(source).is_absolute():
            raise BackendError("сверка доступа к каталогу поддерживается только на POSIX")
        listing = await self._execute(
            [self._executable, "ps", "--all", "--no-trunc", "--format", "{{.ID}}"]
        )
        self._require_success(listing, "docker ps")
        identifiers = [
            line.strip()
            for line in listing.output.splitlines()
            if _CONTAINER_ID.fullmatch(line.strip()) is not None
        ]
        if len(identifiers) > _MAX_SWEPT_CONTAINERS:
            raise BackendError("docker ps: слишком много контейнеров для сверки доступа")
        holders: list[str] = []
        target = self._resolve_posix(source)
        for start in range(0, len(identifiers), _SWEEP_BATCH):
            holders.extend(await self._sweep(identifiers[start : start + _SWEEP_BATCH], target))
        return tuple(holders)

    async def _sweep(self, batch: list[str], target: PurePosixPath) -> list[str]:
        """Наблюдает mounts пачки контейнеров; каждый ID обязан дать свою строку.

        Отсутствие строки означает неполное наблюдение, а по §2 неполный охват
        писателей — неизвестный исход, не пустой список. Поэтому ID без строки
        наблюдается отдельно: только явное «нет такого контейнера» считается
        исчезновением между `docker ps` и `docker inspect`.
        """
        result = await self._execute(
            [
                self._executable,
                "inspect",
                "--type",
                "container",
                "--format",
                _MOUNTS_FORMAT,
                *batch,
            ]
        )
        missing = self._is_missing(result)
        if not missing:
            self._require_success(result, "docker inspect")
        parsed = self._parse_mounts(result.output, batch)
        holders = [
            identifier
            for identifier, sources in parsed
            if any(self._overlaps(target, item) for item in sources)
        ]
        observed = {identifier for identifier, _ in parsed}
        absent = [identifier for identifier in batch if identifier not in observed]
        if not absent:
            return holders
        if len(batch) == 1:
            if not missing:
                raise BackendError("docker inspect: ответ не содержит наблюдения контейнера")
            return holders
        for identifier in absent:
            holders.extend(await self._sweep([identifier], target))
        return holders

    @staticmethod
    def _overlaps(target: PurePosixPath, mounted: str) -> bool:
        """Каталог журнала и источник mount пересекаются в любую сторону вложенности.

        Пути приводятся к фактическим: второй bind одного каталога или путь через
        символическую ссылку иначе прошёл бы лексическую сверку, и ограждение было бы
        объявлено полным. Нормализация опирается на то, что Engine сообщает путь
        того же хоста, где работает producer.
        """
        try:
            source = DockerCli._resolve_posix(mounted)
        except (TypeError, ValueError):
            return False
        if not source.is_absolute():
            return False
        return source == target or source in target.parents or target in source.parents

    @staticmethod
    def _resolve_posix(path: str) -> PurePosixPath:
        """Приводит путь POSIX к фактическому; на Windows только разбирает его.

        Разыменование ссылок имеет смысл лишь на том хосте, где работает Engine, а
        producer поддерживается только на Linux. На Windows остаётся лексический
        разбор: сверка там всё равно отказывает раньше, в writable_mount_holders.
        """
        if sys.platform == "win32":
            return PurePosixPath(path)
        return PurePosixPath(os.path.realpath(path))

    @staticmethod
    def _parse_mounts(output: str, batch: list[str]) -> list[tuple[str, tuple[str, ...]]]:
        """Разбирает по строке на контейнер: ID и источники его записываемых mounts."""
        known = set(batch)
        parsed: list[tuple[str, tuple[str, ...]]] = []
        for line in output.splitlines():
            item = line.strip()
            if not item.startswith("{"):
                continue
            try:
                data = json.loads(item)
                identifier = data["id"]
                mounts = data["mounts"] or []
                if identifier not in known or not isinstance(mounts, list):
                    raise ValueError("состав ответа")
                sources = tuple(
                    str(mount["Source"])
                    for mount in mounts
                    if isinstance(mount, dict) and mount.get("RW") is True
                )
            except (ValueError, KeyError, TypeError) as error:
                raise BackendError("docker inspect: неожиданный формат mounts") from error
            parsed.append((identifier, sources))
        return parsed

    @staticmethod
    def _parse_name(output: str) -> str:
        """Имя из ответа Engine приходит с ведущей косой чертой."""
        try:
            line = next(item for item in output.splitlines() if item.lstrip().startswith("{"))
            name = json.loads(line)["Name"]
            if not isinstance(name, str):
                raise ValueError("имя контейнера")
        except (StopIteration, ValueError, KeyError, TypeError) as error:
            raise BackendError("docker inspect: неожиданный формат ответа") from error
        return name[1:] if name.startswith("/") else name

    @staticmethod
    def _require_container_id(container_id: str) -> str:
        """Операции доверенной границы адресуют контейнер только полным Docker ID."""
        if not isinstance(container_id, str) or _CONTAINER_ID.fullmatch(container_id) is None:
            raise BackendError("операция требует полного Docker ID контейнера")
        return container_id

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
    def _operation_target(cls, container: str | ContainerObservation) -> str:
        """Проверяет пространство имён и фиксирует цель без повторного поиска по имени."""
        if isinstance(container, str):
            cls._require_managed_name(container)
            return container
        cls._require_managed_name(container.name)
        container_id = container.container_id
        if not isinstance(container_id, str) or _CONTAINER_ID.fullmatch(container_id) is None:
            raise BackendError("наблюдение контейнера не содержит полного Docker ID")
        return container_id

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
            container_id = data["Id"]
            if not isinstance(container_id, str) or _CONTAINER_ID.fullmatch(container_id) is None:
                raise ValueError("идентификатор контейнера")
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
                container_id=container_id,
            )
        except (StopIteration, ValueError, KeyError, TypeError, AttributeError) as error:
            raise BackendError("docker inspect: неожиданный формат ответа") from error
