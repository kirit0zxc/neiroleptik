"""Стенд совместного теста границы процесса: разбор параметров и безопасный status.

Живой прогон стенда требует Linux и настоящего Docker Engine, поэтому здесь
проверяются только локальные части: состав параметров, раскладка корней §8.2 и
этап `status`, который ничего не меняет.
"""

import asyncio
from pathlib import Path

import pytest

from agent.runtime.boundary.documents import WriterBinding
from agent.runtime.boundary.producer import ANCHOR_NAME, CONTROL_PREFIX
from agent.runtime.boundary.snapshots import SNAPSHOT_PREFIX
from agent.runtime.drivers.low import LowSettings
from infra.runtime.boundary_stand import BoundaryStand, StandLayout, build_parser

AGENT = "00000000-0000-4000-8000-000000000001"
DEPLOYMENT = "e0d692f0-fb4c-4a37-920e-0c8683471639"
INSTANCE = "a2df557e-785f-48f3-ba6e-348b76730f41"
BOUNDARY = "d6e623c9-425a-4ce4-9b31-24a8ad509283"


def layout(tmp_path: Path) -> StandLayout:
    return StandLayout(
        control_root=tmp_path / "control",
        snapshot_root=tmp_path / "snapshots",
        journal_root=tmp_path / "journals",
        state=tmp_path / "state" / "boundary.db",
        group_gid=65532,
    )


def stand(tmp_path: Path) -> BoundaryStand:
    return BoundaryStand(
        layout(tmp_path),
        WriterBinding(AGENT, DEPLOYMENT, INSTANCE, 1),
        LowSettings(journal_root=tmp_path / "journals"),
    )


def test_parser_requires_roots_state_and_binding() -> None:
    """Корни, каталог состояния и привязку задаёт оператор, а не профиль центра."""
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--stage=status"])
    arguments = parser.parse_args(
        [
            "--stage=hold",
            "--control-root=/srv/hf/control",
            "--snapshot-root=/srv/hf/snapshots",
            "--journal-root=/srv/hf/journals",
            "--state=/var/lib/hf/boundary.db",
            f"--agent-id={AGENT}",
            f"--deployment-id={DEPLOYMENT}",
            f"--instance-id={INSTANCE}",
            "--collector-gid=65532",
        ]
    )
    assert arguments.stage == "hold"
    assert arguments.revision == 1
    assert arguments.collector_gid == 65532
    assert arguments.image == "honeyforge-low:local"
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "--stage=cleanup",
                "--control-root=/srv/hf/control",
                "--snapshot-root=/srv/hf/snapshots",
                "--journal-root=/srv/hf/journals",
                "--state=/var/lib/hf/boundary.db",
                f"--agent-id={AGENT}",
                f"--deployment-id={DEPLOYMENT}",
                f"--instance-id={INSTANCE}",
            ]
        )


def test_layout_follows_the_published_paths(tmp_path: Path) -> None:
    """Раскладка control-root и snapshot-root совпадает с §8.2."""
    prepared = stand(tmp_path)
    control = prepared.control_directory()
    assert control == tmp_path / "control" / CONTROL_PREFIX / AGENT / DEPLOYMENT / INSTANCE
    assert (control / ANCHOR_NAME).name == "anchor.json"
    snapshots = prepared.snapshot_directory(BOUNDARY)
    assert snapshots == (
        tmp_path / "snapshots" / SNAPSHOT_PREFIX / AGENT / DEPLOYMENT / INSTANCE / BOUNDARY
    )


def test_deployment_is_a_low_profile_of_the_binding(tmp_path: Path) -> None:
    """Профиль стенда — один tcp-сервис уровня low с привязкой перехода."""
    deployment = stand(tmp_path).deployment(13222, "SSH-2.0-HoneyForge")
    assert deployment.deployment_id == DEPLOYMENT
    assert deployment.instance_id == INSTANCE
    assert deployment.revision == 1
    assert deployment.config.interaction_level == "low"
    assert [(item.protocol, item.port) for item in deployment.config.services] == [("tcp", 13222)]
    assert deployment.config.credentials == ()


def test_status_reports_an_empty_binding_without_changes(tmp_path: Path) -> None:
    """Этап status не создаёт документов и сообщает отсутствие anchor."""
    prepared = stand(tmp_path)
    report = asyncio.run(prepared.run("status", 13222, "SSH-2.0-HoneyForge"))
    assert report["stage"] == "status"
    assert report["anchor_id"] is None
    assert report["last_boundary_seq"] == 0
    assert report["supported"] is True
    assert report["published"] == []
    assert not (tmp_path / "control").exists()


def test_unknown_stage_is_refused(tmp_path: Path) -> None:
    """Неизвестный этап не выполняет операций producer."""
    with pytest.raises(ValueError):
        asyncio.run(stand(tmp_path).run("cleanup", 13222, "SSH-2.0-HoneyForge"))
