"""Стенд совместного теста доверенной границы процесса Low (§6 предложения).

Модуль не подменяет Docker и не имитирует ограждение: он вызывает настоящий runtime
producer на настоящем Docker Engine и печатает пути опубликованных документов, чтобы
сборщик Димы читал их из согласованных корней. Аварийное завершение писателя стенд
**не выполняет сам**: он печатает полный Docker ID, а участник завершает процесс
выбранным способом (`docker kill --signal KILL <id>`). Так в журнале остаётся честно
аварийный хвост, а стенд не получает лишних прав над контейнерами.

Этапы соответствуют §6: `anchor` — первый запуск writer A с выдачей anchor,
`hold` — шаги 1–4 §3 без фиксации (сборщик видит B, но доказательства нет),
`finish` — только шаг 5, `status` — текущее состояние привязки и опубликованные файлы.
Удержание export между `hold` и `finish` — управляемая точка fault injection стенда,
а не разрешение выдавать RW до снимков.

Публикация поддерживается только на Linux (§8.2). На другой ОС producer выключен,
и стенд сообщает об этом вместо имитации результата.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

from agent.runtime.boundary.documents import ProcessBoundary, WriterAnchor, WriterBinding
from agent.runtime.boundary.errors import BoundaryError
from agent.runtime.boundary.producer import (
    ANCHOR_NAME,
    CONTROL_PREFIX,
    ProducerResult,
    boundary_name,
    build_producer,
)
from agent.runtime.boundary.publication import PublicationRoots
from agent.runtime.boundary.registry import BoundaryRegistry
from agent.runtime.boundary.snapshots import SNAPSHOT_PREFIX
from agent.runtime.contract import (
    ChannelSpec,
    DeploymentSpec,
    LoggingSpec,
    ProfileSpec,
    ServiceSpec,
)
from agent.runtime.drivers.containers import DockerCli
from agent.runtime.drivers.low import LowSettings


@dataclass(frozen=True)
class StandLayout:
    """Корни оператора и каталог состояния runtime; collector читает те же корни."""

    control_root: Path
    snapshot_root: Path
    journal_root: Path
    state: Path
    group_gid: int | None
    snapshot_quota_bytes: int | None = None

    def roots(self) -> PublicationRoots:
        return PublicationRoots(
            self.control_root,
            self.snapshot_root,
            self.group_gid,
            self.snapshot_quota_bytes,
        )


class BoundaryStand:
    """Один прогон совместного теста поверх настоящего producer.

    Стенд владеет только собственным назначением слота: он не трогает чужие контейнеры,
    не удаляет журналы и не публикует ничего, кроме документов самого producer.
    """

    def __init__(self, layout: StandLayout, binding: WriterBinding, settings: LowSettings) -> None:
        self._layout = layout
        self._binding = binding
        self._settings = settings

    def deployment(self, tcp_port: int, banner: str) -> DeploymentSpec:
        """Минимальный профиль уровня low с одним tcp-сервисом согласованного стенда."""
        return DeploymentSpec(
            self._binding.deployment_id,
            self._binding.deployment_id,
            self._binding.profile_revision,
            "running",
            ProfileSpec(
                "low",
                "service-node",
                (ServiceSpec("tcp", tcp_port, banner),),
                LoggingSpec(),
                ChannelSpec(),
                None,
                (),
            ),
            generation=1,
            applied_revision=self._binding.profile_revision,
            instance_id=self._binding.instance_id,
        )

    def control_directory(self) -> Path:
        """Каталог опубликованных anchor и proof этой привязки (§8.2)."""
        return (
            self._layout.control_root
            / CONTROL_PREFIX
            / self._binding.agent_id
            / self._binding.deployment_id
            / self._binding.instance_id
        )

    def snapshot_directory(self, boundary_id: str) -> Path:
        """Каталог закреплённых копий одного перехода (§8.2)."""
        return (
            self._layout.snapshot_root
            / SNAPSHOT_PREFIX
            / self._binding.agent_id
            / self._binding.deployment_id
            / self._binding.instance_id
            / boundary_id
        )

    async def run(self, stage: str, tcp_port: int, banner: str) -> dict[str, object]:
        """Выполняет один этап стенда и возвращает описание результата для печати."""
        with BoundaryRegistry(self._layout.state) as registry:
            producer = build_producer(registry, DockerCli(), self._layout.roots(), self._settings)
            deployment = self.deployment(tcp_port, banner)
            if stage == "status":
                return self._status(registry)
            if stage == "anchor":
                return self._anchor_report(
                    await producer.launch_first_writer(self._binding, deployment)
                )
            if stage == "hold":
                held = await producer.restart_writer(self._binding, deployment, hold_export=True)
                return {
                    "stage": "hold",
                    "state": held.state,
                    "note": (
                        "Писатель B запущен, доказательства перехода ещё нет: "
                        "ожидается waiting/MISSING_PREDECESSOR у сборщика"
                    ),
                    "next_writer": None
                    if held.writer is None
                    else {
                        "writer_id": held.writer.writer_id,
                        "container_id": held.writer.container_id,
                        "journal_seq": held.writer.first_journal.journal_seq,
                    },
                }
            if stage == "finish":
                return self._boundary_report(await producer.finish(self._binding))
            if stage == "restart":
                return self._boundary_report(
                    await producer.restart_writer(self._binding, deployment)
                )
            if stage == "reconcile":
                result = await producer.reconcile(self._binding, deployment)
                if result is None:
                    return {"stage": "reconcile", "state": "нет незавершённого перехода"}
                return {"stage": "reconcile", "state": result.state}
            raise ValueError("неизвестный этап стенда")

    def _status(self, registry: BoundaryRegistry) -> dict[str, object]:
        """Состояние привязки и перечень опубликованных файлов без изменений."""
        state = registry.binding_state(self._binding)
        intent = registry.outstanding_intent(self._binding)
        directory = self.control_directory()
        return {
            "stage": "status",
            "anchor_id": None if state is None else state.anchor_id,
            "last_boundary_seq": 0 if state is None else state.last_boundary_seq,
            "supported": True if state is None else state.supported,
            "blocked_reason": "" if state is None else state.blocked_reason,
            "outstanding": None if intent is None else {"state": intent.state},
            "control_directory": str(directory),
            "published": sorted(item.name for item in directory.iterdir())
            if directory.is_dir()
            else [],
        }

    def _anchor_report(self, result: ProducerResult) -> dict[str, object]:
        """Описание выданного anchor вместе с путём опубликованного файла."""
        assert result.document is not None
        anchor = WriterAnchor.parse(result.document.canonical)
        return {
            "stage": "anchor",
            "anchor_id": anchor.anchor_id,
            "writer_id": anchor.writer_id,
            "container_id": anchor.container_id,
            "journal_seq": anchor.first_journal.journal_seq,
            "published": str(self.control_directory() / ANCHOR_NAME),
            "note": (
                "Аварийное завершение writer A выполняет участник: "
                f"docker kill --signal KILL {anchor.container_id}"
            ),
        }

    def _boundary_report(self, result: ProducerResult) -> dict[str, object]:
        """Описание выданного proof вместе с путями документа и копий."""
        assert result.document is not None
        proof = ProcessBoundary.parse(result.document.canonical)
        name = boundary_name(proof.boundary_seq, proof.boundary_id)
        return {
            "stage": "boundary",
            "boundary_id": proof.boundary_id,
            "boundary_seq": proof.boundary_seq,
            "previous_writer": proof.previous_writer.writer_id,
            "next_writer": proof.next_writer.writer_id,
            "next_journal_seq": proof.next_writer.first_journal.journal_seq,
            "published": str(self.control_directory() / name),
            "snapshots": [
                str(self.snapshot_directory(proof.boundary_id) / item.snapshot_ref)
                for item in proof.previous_journals
            ],
            "note": (
                "Принятие перехода не разрешает cleanup: исходные журналы остаются "
                "на месте, capture_completeness остаётся unknown"
            ),
        }


def build_parser() -> argparse.ArgumentParser:
    """Параметры стенда задаёт оператор; профиль центра здесь не читается."""
    parser = argparse.ArgumentParser(
        description="Стенд совместного теста границы процесса Low (только Linux)"
    )
    parser.add_argument(
        "--stage",
        required=True,
        choices=["anchor", "hold", "finish", "restart", "reconcile", "status"],
    )
    parser.add_argument("--control-root", required=True, type=Path)
    parser.add_argument("--snapshot-root", required=True, type=Path)
    parser.add_argument("--journal-root", required=True, type=Path)
    parser.add_argument("--state", required=True, type=Path, help="SQLite registry границы")
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--deployment-id", required=True)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--revision", type=int, default=1)
    parser.add_argument("--collector-gid", type=int, help="Группа чтения сборщика (G)")
    parser.add_argument(
        "--snapshot-quota-bytes",
        type=int,
        help="Отдельная квота оператора на snapshot_root с учётом подготовки (§8.3)",
    )
    parser.add_argument("--image", default="honeyforge-low:local")
    parser.add_argument("--bind-address", default="127.0.0.1")
    parser.add_argument("--network")
    parser.add_argument("--tcp-port", type=int, default=13222)
    parser.add_argument("--banner", default="SSH-2.0-HoneyForge")
    parser.add_argument("--journal-max-bytes", type=int)
    parser.add_argument("--journal-quota-bytes", type=int)
    return parser


def main() -> None:
    """Печатает результат этапа в JSON; отказ producer печатается кодом без трассировки."""
    parser = build_parser()
    args = parser.parse_args()
    layout = StandLayout(
        control_root=args.control_root,
        snapshot_root=args.snapshot_root,
        journal_root=args.journal_root,
        state=args.state,
        group_gid=args.collector_gid,
        snapshot_quota_bytes=args.snapshot_quota_bytes,
    )
    try:
        binding = WriterBinding(args.agent_id, args.deployment_id, args.instance_id, args.revision)
        settings = LowSettings(
            image=args.image,
            bind_address=args.bind_address,
            network=args.network,
            journal_root=args.journal_root,
            journal_max_bytes=args.journal_max_bytes,
            journal_quota_bytes=args.journal_quota_bytes,
        )
        stand = BoundaryStand(layout, binding, settings)
        report = asyncio.run(stand.run(args.stage, args.tcp_port, args.banner))
    except BoundaryError as error:
        parser.exit(1, f"Стенд остановлен: {error.code.value}: {error.detail}\n")
    except (ValueError, OSError) as error:
        parser.exit(1, f"Стенд остановлен: локальная конфигурация ({type(error).__name__})\n")
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
