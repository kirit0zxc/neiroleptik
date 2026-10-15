"""Cowrie использует проверенный жизненный цикл контейнера и отдельный перевод профиля."""

import ipaddress
from collections.abc import Callable
from typing import ClassVar
from uuid import uuid4

from agent.runtime.contract import DeploymentSpec, Level
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

SSH_BANNER = "SSH-2.0-OpenSSH_9.2p1 Debian-2+deb12u3"


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
        valid_credentials = len(profile.credentials) == 1 and (
            profile.credentials[0].username == "root"
            and profile.credentials[0].password.reveal() == "demo"
        )
        if profile.services[0].port != 2222 or not valid_credentials:
            raise self._unsupported(
                "Демо Medium: порт 2222, единственные credentials профиля root/demo"
            )
        if profile.services[0].banner:
            raise self._unsupported("пользовательский SSH banner пока не поддерживается")
        if self._settings.network is None or self._settings.journal_root is None:
            raise self._unsupported("Medium требует отдельную сеть и постоянный каталог журналов")
        return LowBlueprint(
            deployment.deployment_id, deployment.revision, profile.services[0], None, ()
        )

    def container_spec(self, blueprint: LowBlueprint, instance_id: str) -> ContainerSpec:
        directory = self.journal_directory(blueprint.deployment_id)
        assert directory is not None and blueprint.tcp is not None
        return ContainerSpec(
            name=self.container_name(blueprint.deployment_id),
            image=self._settings.image,
            command=(instance_id,),
            publish=(PortPublish(self._settings.bind_address, blueprint.tcp.port, 2222),),
            mounts=(BindMount(str(directory), "/cowrie/cowrie-git/var"),),
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
            self._probe_host(), blueprint.tcp.port, (SSH_BANNER + "\r\n").encode(), None
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
