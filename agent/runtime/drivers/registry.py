"""Выбор драйвера по уровню назначения центра.

Реестр — единственное место, которое знает соответствие «уровень → драйвер». Уровень
считается поддерживаемым только при наличии настроек хоста: Medium без каталога
состояния и закреплённого образа Cowrie запускать нельзя, поэтому команда такого
уровня получает отказ без единого обращения к Docker. Этот же набор уровней
объявляется центру при регистрации и сверяется в heartbeat.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agent.runtime.contract import Level
from agent.runtime.drivers.containers import ContainerBackend
from agent.runtime.drivers.low import LowDriver, LowSettings
from agent.runtime.drivers.medium import MediumDriver, MediumSettings
from agent.runtime.drivers.slot import ContainerSlotDriver

# Уровни, для которых в агенте есть драйвер. High требует отдельного менеджера VM
# и здесь не реализован.
SUPPORTED_LEVELS: tuple[Level, ...] = ("low", "medium")


class DriverRegistry:
    """Создаёт драйвер слота для уровня назначения по настройкам оператора хоста."""

    def __init__(
        self, low: LowSettings | None = None, medium: MediumSettings | None = None
    ) -> None:
        self._low = low if low is not None else LowSettings()
        self._medium = medium

    @property
    def levels(self) -> tuple[Level, ...]:
        """Настроенные уровни в порядке SUPPORTED_LEVELS; Low настроен всегда."""
        return ("low",) if self._medium is None else ("low", "medium")

    def supports(self, level: str) -> bool:
        """Проверяется до любых обращений к Docker и до создания драйвера."""
        return level in self.levels

    def create(
        self,
        level: str,
        deployment_id: str,
        backend: ContainerBackend,
        new_instance_id: Callable[[], str],
    ) -> ContainerSlotDriver[Any]:
        """Драйвер одного слота; уровень без настроек запрашивать нельзя.

        Тип описания профиля у уровней разный, а исполнителю нужны только общие
        операции слота, поэтому параметр описания не уточняется.
        """
        if level == "low":
            return LowDriver(deployment_id, backend, self._low, new_instance_id=new_instance_id)
        if level == "medium" and self._medium is not None:
            return MediumDriver(
                deployment_id, backend, self._medium, new_instance_id=new_instance_id
            )
        raise ValueError("уровень не настроен на этом узле")
