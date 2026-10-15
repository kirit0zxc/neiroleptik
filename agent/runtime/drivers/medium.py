"""Драйвер Medium: контейнер Cowrie (shell backend) по профилю центра.

Драйвер не собирает и не скачивает образы: образ `honeyforge-medium` закрепляется и
доставляется заранее (`honeypots/medium/`), а `docker run` выполняется с `--pull never`.
Общий жизненный цикл слота реализован в `slot.py`; здесь остаются перевод профиля
Medium, подготовка каталога экземпляра на хосте (конфигурация Cowrie и userdb) и проба
строки идентификации SSH.

Учётные данные профиля попадают только в один файл `userdb.txt` с правами 0600,
смонтированный в контейнер только для чтения. Пароль не попадает в аргументы docker,
метки, журналы, ACK и сообщения об ошибках: аргументы контейнера видны в
`docker inspect` и в списке процессов хоста.

Ни контейнер Cowrie, ни разметка его конфигурации не проверялись на настоящем Docker
Engine и на закреплённом релизе Cowrie: имена параметров взяты из документации и
требуют подтверждения живым прогоном (specs/runtime-drivers.md, infra/runtime/README.md).
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar
from uuid import uuid4

from agent.runtime.contract import CredentialSpec, DeploymentSpec, Level, ServiceSpec
from agent.runtime.drivers.base import DriverError, DriverErrorCode
from agent.runtime.drivers.containers import (
    LABEL_DEPLOYMENT,
    LABEL_INSTANCE,
    LABEL_LEVEL,
    LABEL_MANAGED,
    LABEL_REVISION,
    BindMount,
    ContainerBackend,
    ContainerSpec,
    PortPublish,
    TmpfsMount,
)
from agent.runtime.drivers.health import (
    HealthCheckFailed,
    PortProbe,
    ReleaseTargets,
    check_probe_timings,
    probe_host,
)
from agent.runtime.drivers.slot import ContainerSlotDriver

# Порт внутри контейнера фиксирован; публичный порт профиля публикуется на хост.
MEDIUM_SSH_CONTAINER_PORT = 2222
# Каталоги внутри контейнера: конфигурация только для чтения и записываемое состояние.
CONFIG_CONTAINER_DIR = "/honeyforge/conf"
STATE_CONTAINER_DIR = "/honeyforge/state"
TMP_CONTAINER_DIR = "/tmp"
CONFIG_FILE_NAME = "cowrie.cfg"
USERDB_FILE_NAME = "userdb.txt"
# Строка идентификации SSH обязана начинаться с этого префикса (RFC 4253, раздел 4.2),
# а вместе с CR LF не превышать 255 байт.
SSH_VERSION_PREFIX = "SSH-2.0-"
MAX_IDENTIFICATION_WITH_CRLF = 255
# Поддеревья записываемого состояния: журнал Cowrie, данные сессий, pid и ключи хоста.
STATE_SUBDIRECTORIES = ("log", "lib", "lib/tty", "lib/downloads", "run", "keys")
# Символы, которые userdb Cowrie трактует как разделитель или шаблон пароля.
_USERDB_FORBIDDEN_CHARACTERS = frozenset(":*?")
_USERDB_FORBIDDEN_PREFIXES = ("!", "/")


@dataclass(frozen=True)
class MediumSettings:
    """Настройки оператора хоста; профиль центра их не меняет.

    `state_root` обязателен: Cowrie нельзя запустить без записываемого состояния и без
    сгенерированной конфигурации. Пользователь по умолчанию — непривилегированный
    пользователь образа Cowrie; его uid:gid должен совпадать с образом, иначе ловушка
    не сможет писать в смонтированные каталоги.
    """

    state_root: Path
    image: str = "honeyforge-medium:local"
    bind_address: str = "127.0.0.1"
    network: str | None = None
    user: str = "1000:1000"
    # Cowrie — процесс Python/Twisted с эмуляцией shell: лимиты Low (128 МиБ, 0.5 CPU,
    # 64 процесса) для него занижены. Значения подобраны с запасом и уточняются
    # по результатам живого прогона.
    memory_mb: int = 512
    cpus: float = 1.0
    pids_limit: int = 256
    tmp_size_mb: int = 16
    # Запуск Cowrie дольше Low: при первом старте экземпляра генерируются ключи хоста.
    start_timeout: float = 60.0
    probe_interval: float = 0.5
    probe_timeout: float = 5.0
    # Секунды на мягкую остановку (SIGTERM) перед принудительной (SIGKILL): Cowrie
    # закрывает сессии и дописывает TTY-записи.
    stop_timeout: int = 15

    def __post_init__(self) -> None:
        ipaddress.ip_address(self.bind_address)
        # Негодные образ, сеть или пользователь обнаруживаются при настройке, а не
        # после удаления работающего экземпляра.
        ContainerSpec.check_image(self.image)
        ContainerSpec.check_user(self.user)
        if self.network is not None:
            ContainerSpec.check_network(self.network)
        if type(self.stop_timeout) is not int or not 0 <= self.stop_timeout <= 25:
            raise ValueError("stop_timeout должен быть целым от 0 до 25 секунд")
        object.__setattr__(self, "state_root", Path(self.state_root))
        if not Path(self.state_root).is_absolute():
            raise ValueError("state_root должен быть абсолютным путём текущей ОС")
        BindMount.check_source(str(self.state_root))
        check_probe_timings(self, ("start_timeout", "probe_interval", "probe_timeout"))


@dataclass(frozen=True)
class MediumBlueprint:
    """Производное от профиля описание ловушки Medium.

    Учётные данные нужны при каждом запуске (в том числе `reset`), поэтому хранятся
    в памяти драйвера, но только внутри `Secret`: `repr` и `str` значения не раскрывают.
    """

    deployment_id: str
    revision: int
    ssh: ServiceSpec
    hostname: str
    credentials: tuple[CredentialSpec, ...] = field(repr=False)


@dataclass(frozen=True)
class MediumTargets:
    """Опубликованный порт SSH и ожидаемая строка идентификации.

    `identification` задана, только если профиль указал баннер: иначе Cowrie
    объявляет собственную версию, и проба проверяет лишь префикс протокола.
    """

    host: str
    ssh_port: int
    identification: bytes | None


class CowrieConfiguration:
    """Текст `cowrie.cfg` и `userdb.txt` для одного экземпляра Medium.

    Все пути указывают внутрь контейнера. Значения берутся из профиля центра; ни одно
    из них не попадает в аргументы docker. Имена параметров Cowrie взяты из
    документации проекта и подлежат проверке на закреплённом релизе при живом прогоне.
    """

    def __init__(self, blueprint: MediumBlueprint) -> None:
        self._blueprint = blueprint

    def cowrie_cfg(self) -> str:
        """Конфигурация shell-ловушки: telnet и проброс портов выключены."""
        keys = f"{STATE_CONTAINER_DIR}/keys"
        sections: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
            (
                "honeypot",
                (
                    ("hostname", self._blueprint.hostname),
                    ("backend", "shell"),
                    ("log_path", f"{STATE_CONTAINER_DIR}/log"),
                    ("download_path", f"{STATE_CONTAINER_DIR}/lib/downloads"),
                    ("state_path", f"{STATE_CONTAINER_DIR}/lib"),
                    ("data_path", f"{STATE_CONTAINER_DIR}/lib"),
                    ("ttylog", "true"),
                    ("ttylog_path", f"{STATE_CONTAINER_DIR}/lib/tty"),
                    ("auth_class", "UserDB"),
                ),
            ),
            (
                "ssh",
                (
                    ("enabled", "true"),
                    (
                        "listen_endpoints",
                        f"tcp:{MEDIUM_SSH_CONTAINER_PORT}:interface=0.0.0.0",
                    ),
                    # Проброс портов и туннели открыли бы ловушке путь в сеть хоста.
                    ("forwarding", "false"),
                    ("forward_redirect", "false"),
                    ("forward_tunnel", "false"),
                    # SFTP пишет загруженные файлы в состояние на хосте, где размер и
                    # квота ещё не согласованы: до согласования канал закрыт.
                    ("sftp_enabled", "false"),
                    ("rsa_public_key", f"{keys}/ssh_host_rsa_key.pub"),
                    ("rsa_private_key", f"{keys}/ssh_host_rsa_key"),
                    ("ecdsa_public_key", f"{keys}/ssh_host_ecdsa_key.pub"),
                    ("ecdsa_private_key", f"{keys}/ssh_host_ecdsa_key"),
                    ("ed25519_public_key", f"{keys}/ssh_host_ed25519_key.pub"),
                    ("ed25519_private_key", f"{keys}/ssh_host_ed25519_key"),
                )
                + (
                    (("version", self._blueprint.ssh.banner),) if self._blueprint.ssh.banner else ()
                ),
            ),
            ("telnet", (("enabled", "false"),)),
            (
                "output_jsonlog",
                (("enabled", "true"), ("logfile", f"{STATE_CONTAINER_DIR}/log/cowrie.json")),
            ),
            ("output_textlog", (("enabled", "false"),)),
        )
        # Заголовок и значения остаются ASCII: кодировку, в которой Cowrie читает
        # cowrie.cfg, задаёт он сам, и ASCII читается одинаково в любой из них.
        lines = [
            "# Generated by the HoneyForge Medium driver for a single honeypot instance.",
            "# Do not edit: every start rewrites this file (specs/runtime-drivers.md).",
        ]
        for name, options in sections:
            lines.append("")
            lines.append(f"[{name}]")
            lines.extend(f"{key} = {value}" for key, value in options)
        return "\n".join(lines) + "\n"

    def userdb(self) -> str:
        """Строки `логин:x:пароль` в формате userdb Cowrie; второе поле Cowrie не использует.

        Файл содержит только эти строки: комментарии не добавляются, потому что их
        обработка построчным разбором userdb не проверена, а лишняя строка стала бы
        лишней учётной записью.
        """
        return (
            "\n".join(
                f"{item.username}:x:{item.password.reveal()}"
                for item in self._blueprint.credentials
            )
            + "\n"
        )


class MediumProfileTranslator:
    """Переводит профиль центра в запуск Cowrie; отказывает, а не искажает профиль."""

    def __init__(self, settings: MediumSettings) -> None:
        self._settings = settings

    @staticmethod
    def container_name(deployment_id: str) -> str:
        """Имя контейнера слота; префикс honeyforge- защищает чужие контейнеры."""
        return f"honeyforge-medium-{deployment_id}"

    def blueprint(self, deployment: DeploymentSpec) -> MediumBlueprint:
        """Проверяет профиль на реализуемость; нарушение — UNSUPPORTED_PROFILE."""
        profile = deployment.config
        if profile.interaction_level != "medium":
            raise self._unsupported("профиль не уровня medium")
        ssh = [service for service in profile.services if service.protocol == "ssh"]
        if len(ssh) != len(profile.services):
            raise self._unsupported("Medium поддерживает только ssh")
        if len(ssh) != 1:
            raise self._unsupported("Medium открывает ровно один ssh-сервис")
        if ssh[0].banner:
            self._check_identification(ssh[0].banner)
        if not profile.credentials:
            raise self._unsupported("Medium требует хотя бы одной учётной записи")
        for item in profile.credentials:
            self._check_credential(item)
        return MediumBlueprint(
            deployment_id=deployment.deployment_id,
            revision=deployment.revision,
            ssh=ssh[0],
            hostname=profile.hostname,
            credentials=profile.credentials,
        )

    def instance_directory(self, deployment_id: str, instance_id: str) -> Path:
        """Каталог экземпляра на хосте: `<state_root>/<deployment_id>/<instance_id>`."""
        return self._settings.state_root / deployment_id / instance_id

    def container_spec(self, blueprint: MediumBlueprint, instance_id: str) -> ContainerSpec:
        """Описание запуска нового экземпляра: порт, метки, тома и меры ограничения.

        Корневая файловая система остаётся только для чтения: Cowrie получает
        записываемое состояние отдельным bind mount, а `/tmp` — ограниченным tmpfs.
        Конфигурация и userdb монтируются только для чтения и лежат вне записываемого
        поддерева, поэтому процесс ловушки не может переписать свои учётные данные.
        """
        instance = self.instance_directory(blueprint.deployment_id, instance_id)
        return ContainerSpec(
            name=self.container_name(blueprint.deployment_id),
            image=self._settings.image,
            # Профиль не попадает в аргументы: конфигурация и пароли лежат в файлах.
            command=(
                f"--config-dir={CONFIG_CONTAINER_DIR}",
                f"--state-dir={STATE_CONTAINER_DIR}",
            ),
            publish=(
                PortPublish(
                    self._settings.bind_address, blueprint.ssh.port, MEDIUM_SSH_CONTAINER_PORT
                ),
            ),
            mounts=(
                BindMount(str(instance / "conf"), CONFIG_CONTAINER_DIR, read_only=True),
                BindMount(str(instance / "state"), STATE_CONTAINER_DIR),
            ),
            tmpfs=(TmpfsMount(TMP_CONTAINER_DIR, self._settings.tmp_size_mb),),
            labels=(
                (LABEL_MANAGED, "true"),
                (LABEL_LEVEL, "medium"),
                (LABEL_DEPLOYMENT, blueprint.deployment_id),
                (LABEL_INSTANCE, instance_id),
                (LABEL_REVISION, str(blueprint.revision)),
            ),
            network=self._settings.network,
            user=self._settings.user,
            memory_mb=self._settings.memory_mb,
            cpus=self._settings.cpus,
            pids_limit=self._settings.pids_limit,
        )

    def health_targets(self, blueprint: MediumBlueprint) -> MediumTargets:
        """Проба идёт с хоста на опубликованный порт; wildcard-адрес заменяется loopback."""
        identification = None
        if blueprint.ssh.banner:
            identification = (blueprint.ssh.banner + "\r\n").encode("ascii")
        return MediumTargets(
            host=probe_host(self._settings.bind_address),
            ssh_port=blueprint.ssh.port,
            identification=identification,
        )

    def release_targets(self, deployment: DeploymentSpec) -> ReleaseTargets:
        """Порты для подтверждения остановки: все порты профиля, без проверки состава.

        Остановка не должна зависеть от того, принимает ли драйвер состав профиля:
        профиль с несколькими ssh-сервисами или с непригодным баннером центр
        допускает, и освобождение всех его портов всё равно нужно подтвердить.
        Проверяется только уровень: профиль не уровня medium этот драйвер не обслуживает.
        """
        if deployment.config.interaction_level != "medium":
            raise self._unsupported("профиль не уровня medium")
        return ReleaseTargets(
            probe_host(self._settings.bind_address),
            frozenset(service.port for service in deployment.config.services),
        )

    @classmethod
    def _check_identification(cls, banner: str) -> None:
        """Строка идентификации SSH: иначе ловушка объявила бы не тот протокол.

        `%` исключён, потому что значение попадает в `cowrie.cfg`, где его может
        истолковать интерполяция configparser; ведущие и завершающие пробелы
        исключены, потому что configparser их отбрасывает, и проба здоровья
        сравнивала бы с другой строкой.
        """
        if not banner.startswith(SSH_VERSION_PREFIX):
            raise cls._unsupported(f"баннер ssh должен начинаться с {SSH_VERSION_PREFIX}")
        if any(not 32 <= ord(char) <= 126 or char == "%" for char in banner):
            raise cls._unsupported("баннер ssh должен быть печатным ASCII без %")
        if banner != banner.strip():
            raise cls._unsupported("баннер ssh не может начинаться или заканчиваться пробелом")
        if len(banner) + 2 > MAX_IDENTIFICATION_WITH_CRLF:
            raise cls._unsupported("баннер ssh длиннее строки идентификации SSH")

    @classmethod
    def _check_credential(cls, item: CredentialSpec) -> None:
        """Пароль должен попасть в userdb как литерал, а не как шаблон Cowrie.

        Сообщение не содержит ни пароля, ни имени пользователя. Центр допускает
        более широкий набор символов, чем построчный userdb: такие профили драйвер
        отклоняет, а не искажает смысл проверки пароля.
        """
        password = item.password.reveal()
        if any(not 32 <= ord(char) <= 126 for char in password):
            raise cls._unsupported("пароль Medium должен быть печатным ASCII")
        if any(char in _USERDB_FORBIDDEN_CHARACTERS for char in password) or password.startswith(
            _USERDB_FORBIDDEN_PREFIXES
        ):
            raise cls._unsupported("пароль содержит символ, который userdb Cowrie считает шаблоном")

    @staticmethod
    def _unsupported(reason: str) -> DriverError:
        return DriverError(DriverErrorCode.UNSUPPORTED_PROFILE, reason)


class MediumHostState:
    """Каталог экземпляра Medium на хосте: конфигурация и записываемое состояние.

    Схема `<state_root>/<deployment_id>/<instance_id>/`:

    - `conf/` — `cowrie.cfg` (0640) и `userdb.txt` (0600), монтируется только для чтения;
    - `state/` — `log`, `lib/tty`, `lib/downloads`, `run`, `keys`, монтируется записываемым.

    Каталоги прежних экземпляров не удаляются: их журнал и TTY-записи могут быть ещё не
    прочитаны сборщиком. Биты доступа не доказывают доступ при дополнительных ACL или
    read-only файловой системе; на Windows `os.chown`, `os.fchown` и `O_NOFOLLOW`
    отсутствуют, и локальные тесты работают от текущего пользователя.
    """

    DIRECTORY_MODE = 0o750
    CONFIG_MODE = 0o640
    USERDB_MODE = 0o600

    def __init__(self, directory: Path, user: str) -> None:
        self._directory = directory
        uid, gid = user.split(":")
        self._uid, self._gid = int(uid), int(gid)

    def prepare(self, configuration: CowrieConfiguration) -> None:
        """Идемпотентно готовит каталоги и файлы одной попытки запуска."""
        # Каталог назначения и каталог экземпляра создаются явно: иначе промежуточные
        # уровни получили бы права по umask, а не 0750 и пользователя контейнера.
        self._make_directory(self._directory.parent)
        self._make_directory(self._directory)
        config = self._make_directory(self._directory / "conf")
        state = self._make_directory(self._directory / "state")
        for name in STATE_SUBDIRECTORIES:
            self._make_directory(state / name)
        self._write(config / CONFIG_FILE_NAME, configuration.cowrie_cfg(), self.CONFIG_MODE)
        self._write(config / USERDB_FILE_NAME, configuration.userdb(), self.USERDB_MODE)

    def _make_directory(self, path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True, mode=self.DIRECTORY_MODE)
        observed = path.lstat()
        if stat.S_ISLNK(observed.st_mode) or not stat.S_ISDIR(observed.st_mode):
            raise OSError("каталог состояния не может быть ссылкой или обычным файлом")
        # Владельцу нужны чтение, запись и проход: Cowrie создаёт в них файлы.
        # Уже существующие права не расширяем: отказ происходит до остановки ловушки.
        if observed.st_mode & stat.S_IRWXU != stat.S_IRWXU:
            raise OSError("владельцу каталога состояния нужны права чтения, записи и прохода")
        chown = getattr(os, "chown", None)
        if chown is not None and (observed.st_uid, observed.st_gid) != (self._uid, self._gid):
            chown(path, self._uid, self._gid, follow_symlinks=False)
        return path

    def _write(self, path: Path, text: str, mode: int) -> None:
        """Создаёт файл с закрытыми правами до записи содержимого.

        Режим задаётся повторно: `os.open` применяет его только при создании, а повтор
        подготовки той же попытки запуска перезаписывает уже существующий файл.
        """
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, mode)
        try:
            handle = os.fdopen(descriptor, "w", encoding="ascii", newline="\n")
        except BaseException:
            os.close(descriptor)
            raise
        with handle:
            for name, arguments in (("fchmod", (mode,)), ("fchown", (self._uid, self._gid))):
                operation: Callable[..., None] | None = getattr(os, name, None)
                if operation is not None:
                    operation(handle.fileno(), *arguments)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())


class MediumHealthCheck(PortProbe[MediumTargets]):
    """Читает строку идентификации SSH и закрывает соединение, ничего не отправляя.

    Собственная строка клиента не отправляется и обмен ключами не начинается: проба
    оставляет в журнале Cowrie только запись о соединении. Проверяется первая строка
    ответа; Cowrie не отправляет строк до строки идентификации (подтвердить живым
    прогоном).
    """

    async def _probe_once(self, targets: MediumTargets, deadline: float) -> None:
        async with asyncio.timeout(self._budget(deadline)):
            reader, writer = await asyncio.open_connection(targets.host, targets.ssh_port)
            try:
                received = await reader.readline()
            finally:
                await self._close(writer)
        if targets.identification is not None:
            if received != targets.identification:
                raise HealthCheckFailed("ssh: строка идентификации не совпала с профилем")
        elif not received.startswith(SSH_VERSION_PREFIX.encode("ascii")) or not received.endswith(
            b"\n"
        ):
            raise HealthCheckFailed("ssh: ответ не является строкой идентификации SSH")


class MediumDriver(ContainerSlotDriver[MediumBlueprint]):
    """Один слот Medium: одно назначение, один контейнер Cowrie, новый instance_id."""

    level: ClassVar[Level] = "medium"

    def __init__(
        self,
        deployment_id: str,
        backend: ContainerBackend,
        settings: MediumSettings,
        health: MediumHealthCheck | None = None,
        new_instance_id: Callable[[], str] = lambda: str(uuid4()),
    ) -> None:
        self._settings = settings
        self._translator = MediumProfileTranslator(settings)
        self._health = (
            health
            if health is not None
            else MediumHealthCheck(
                settings.start_timeout, settings.probe_interval, settings.probe_timeout
            )
        )
        self._make_instance_id = new_instance_id
        super().__init__(deployment_id, backend, settings.stop_timeout)

    def _container_name(self) -> str:
        return MediumProfileTranslator.container_name(self.deployment_id)

    def _new_instance_id(self) -> str:
        return self._make_instance_id()

    def _translate_profile(self, deployment: DeploymentSpec) -> MediumBlueprint:
        return self._translator.blueprint(deployment)

    def _container_spec(self, blueprint: MediumBlueprint, instance_id: str) -> ContainerSpec:
        return self._translator.container_spec(blueprint, instance_id)

    async def _check_healthy(self, blueprint: MediumBlueprint) -> None:
        await self._health.wait_until_healthy(self._translator.health_targets(blueprint))

    async def _check_released(self, deployment: DeploymentSpec) -> bool:
        return await self._health.wait_for_release(self._translator.release_targets(deployment))

    def _prepare_host(
        self, blueprint: MediumBlueprint, instance_id: str, spec: ContainerSpec
    ) -> None:
        """Готовит каталог экземпляра и конфигурацию до удаления прежнего экземпляра.

        Недоступный каталог или файл обнаруживаются, пока исправная ловушка ещё
        работает. Сообщение об ошибке не содержит пути и содержимого конфигурации.
        """
        directory = self._translator.instance_directory(self.deployment_id, instance_id)
        try:
            MediumHostState(directory, spec.user).prepare(CowrieConfiguration(blueprint))
        except OSError as error:
            raise DriverError(
                DriverErrorCode.START_FAILED, "каталог состояния Cowrie недоступен для подготовки"
            ) from error
