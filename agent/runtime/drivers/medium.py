"""Cowrie использует проверенный жизненный цикл контейнера и отдельный перевод профиля."""

import ipaddress
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import ClassVar
from uuid import uuid4

from agent.runtime.contract import DeploymentSpec, Level, ProfileSpec
from agent.runtime.drivers.containers import (
    LABEL_MANAGED,
    BindMount,
    ContainerBackend,
    ContainerSpec,
    PortPublish,
)
from agent.runtime.drivers.low import (
    LABEL_DEPLOYMENT,
    LABEL_INSTANCE,
    LABEL_LEVEL,
    LABEL_REVISION,
    HealthTargets,
    LowBlueprint,
    LowDriver,
    LowProfileTranslator,
    LowSettings,
)
from agent.runtime.drivers.medium_config import MediumConfigStore

SSH_BANNER = "SSH-2.0-OpenSSH_9.2p1 Debian-2+deb12u3"


@dataclass(frozen=True)
class MediumBlueprint(LowBlueprint):
    """Профиль содержит секреты и исключён из диагностического repr."""

    profile: ProfileSpec = field(repr=False)


class MediumProfileTranslator(LowProfileTranslator):
    """Один SSH-порт; пути и образ задаёт оператор, не профиль центра."""

    @staticmethod
    def container_name(deployment_id: str) -> str:
        return f"honeyforge-medium-{deployment_id}"

    def blueprint(self, deployment: DeploymentSpec) -> LowBlueprint:
        if not ipaddress.ip_address(self._settings.bind_address).is_loopback:
            raise self._unsupported("Medium demo разрешён только на loopback")
        profile = deployment.config
        if profile.interaction_level != "medium":
            raise self._unsupported("профиль не уровня medium")
        if len(profile.services) != 1 or profile.services[0].protocol != "ssh":
            raise self._unsupported("Medium поддерживает ровно один SSH-сервис")
        if not 1 <= len(profile.credentials) <= 8:
            raise self._unsupported("Medium требует от одного до восьми пользователей")
        banner = profile.services[0].banner
        if banner and (
            not banner.startswith("SSH-2.0-") or not all(32 <= ord(c) < 127 for c in banner)
        ):
            raise self._unsupported("SSH banner должен начинаться SSH-2.0- и содержать ASCII")
        if self._settings.network is None or self._settings.journal_root is None:
            raise self._unsupported("Medium требует отдельную сеть и постоянный каталог журналов")
        return MediumBlueprint(
            deployment.deployment_id, deployment.revision, profile.services[0], None, (), profile
        )

    def container_spec(self, blueprint: LowBlueprint, instance_id: str) -> ContainerSpec:
        directory = self.journal_directory(blueprint.deployment_id)
        assert directory is not None and blueprint.tcp is not None
        assert isinstance(blueprint, MediumBlueprint) and self._settings.journal_root is not None
        try:
            config = MediumConfigStore(self._settings.journal_root.parent / "medium-config").write(
                blueprint.deployment_id, instance_id, blueprint.profile
            )
        except (OSError, ValueError):
            raise self._unsupported(
                "Не удалось безопасно подготовить конфигурацию Cowrie"
            ) from None
        return ContainerSpec(
            name=self.container_name(blueprint.deployment_id),
            image=self._settings.image,
            command=(instance_id,),
            publish=(PortPublish(self._settings.bind_address, blueprint.tcp.port, 2222),),
            mounts=(
                BindMount(str(directory), "/cowrie/cowrie-git/var"),
                BindMount(str(config), "/honeyforge-config", read_only=True),
            ),
            labels=(
                (LABEL_MANAGED, "true"),
                (LABEL_LEVEL, "medium"),
                (LABEL_DEPLOYMENT, blueprint.deployment_id),
                (LABEL_INSTANCE, instance_id),
                (LABEL_REVISION, str(blueprint.revision)),
            ),
            network=self._settings.network,
            user="999:999",
            memory_mb=256,
        )

    def health_targets(self, blueprint: LowBlueprint) -> HealthTargets:
        assert blueprint.tcp is not None
        return HealthTargets(
            self._probe_host(),
            blueprint.tcp.port,
            ((blueprint.tcp.banner or SSH_BANNER) + "\r\n").encode(),
            None,
        )

    def release_targets(self, deployment: DeploymentSpec) -> HealthTargets:
        if deployment.config.interaction_level != "medium":
            raise self._unsupported("профиль не уровня medium")
        return HealthTargets(
            self._probe_host(),
            None,
            None,
            None,
            frozenset(s.port for s in deployment.config.services),
        )


class MediumDriver(LowDriver):
    """Отдельные имена и метки исключают удаление Low при операциях Medium."""

    level: ClassVar[Level] = "medium"

    def __init__(
        self,
        deployment_id: str,
        backend: ContainerBackend,
        settings: LowSettings,
        new_instance_id: Callable[[], str] = lambda: str(uuid4()),
    ) -> None:
        super().__init__(
            deployment_id,
            backend,
            settings,
            new_instance_id=new_instance_id,
            translator=MediumProfileTranslator(settings),
        )
