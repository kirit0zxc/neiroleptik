"""Доверенная позиция поколения и ограниченные результаты фоновых шагов."""

from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from agent.telemetry.adapters.cowrie import CowrieBinding
from agent.telemetry.schemas import compact_json


class CowrieJournalError(ValueError):
    """Категория отказа без пути, сырья и текста внешнего исключения."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class CowrieJournalSource:
    """Не доказывает ACK: worker отдельно сверяет привязку с историей исполнителя."""

    binding: CowrieBinding
    journal_id: UUID
    root: Path = field(repr=False)
    relative_path: str = field(repr=False)

    def __post_init__(self) -> None:
        path = self.relative_path
        try:
            length = len(path.encode("utf-8")) if isinstance(path, str) else 0
        except UnicodeError:
            raise CowrieJournalError("INVALID_BINDING") from None
        if (
            not isinstance(self.binding, CowrieBinding)
            or not isinstance(self.journal_id, UUID)
            or not isinstance(self.root, Path)
            or not self.root.is_absolute()
            or ".." in self.root.parts
            or "\x00" in str(self.root)
            or self.root.drive.startswith("\\\\")
            or not isinstance(path, str)
            or not path
            or length > 4096
            or any(c in path for c in ("\\", ":", "\x00"))
            or any(p in ("", ".", "..") for p in path.split("/"))
        ):
            raise CowrieJournalError("INVALID_BINDING")

    @property
    def scope(self) -> str:
        return binding_scope(self.binding)


def binding_scope(binding: CowrieBinding) -> str:
    """Имя файла, revision и порт не создают новую историю того же instance."""
    return compact_json(
        [str(binding.agent_id), str(binding.deployment_id), str(binding.instance_id)]
    )


@dataclass(frozen=True)
class CowrieCollectionLimits:
    """Scan включает повторную проверку сохранённых bytes и непродвинутого хвоста."""

    max_records: int = 100
    max_bytes: int = 4 * 1024 * 1024
    max_scan_bytes: int = 64 * 1024 * 1024

    def __post_init__(self) -> None:
        if any(
            type(v) is not int or v < 1
            for v in (self.max_records, self.max_bytes, self.max_scan_bytes)
        ):
            raise ValueError("Нужны положительные целые лимиты")


@dataclass(frozen=True)
class CowrieProgress:
    """Прогресс одного поколения без чувствительных данных."""

    state: str
    records: int = 0
    raw_bytes: int = 0
    code: str | None = None
