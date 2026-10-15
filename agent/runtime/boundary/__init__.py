"""Доверенная граница процесса Low: runtime producer anchor и proof.

Состав пакета по спецификации `specs/journal-process-boundary-proposal.md`:

- `documents` — канонические anchor и proof (§4, §8.1);
- `journals` — наблюдение каталога слота и цепочки поколений (§8.1, §8.3);
- `registry` — долговечные intents, состояния и выданные байты документов (§8.4);
- `snapshots` — закрепление байтов журналов до выдачи нового RW (§8.3);
- `publication` — локальная публикация write-once на Linux (§8.2);
- `launcher` — одна жизнь контейнера-писателя и привязка заголовка (§3 шаг 4);
- `producer` — порядок шагов перехода и восстановление (§3, §8.4).

Пакет не меняет формат журнала Low, не определяет Event DTO и не вводит сетевого
канала: anchor и proof публикуются только локально на runtime-хосте.
"""

from agent.runtime.boundary.documents import (
    ANCHOR_SCHEMA,
    BOUNDARY_SCHEMA,
    CanonicalJson,
    FirstJournal,
    NextWriter,
    PreviousWriter,
    ProcessBoundary,
    SnapshotEntry,
    WriterAnchor,
    WriterBinding,
)
from agent.runtime.boundary.errors import BoundaryError, BoundaryErrorCode
from agent.runtime.boundary.journals import JournalReader, JournalSlot
from agent.runtime.boundary.launcher import WriterAllocation, WriterBackend, WriterLauncher
from agent.runtime.boundary.producer import (
    ANCHOR_NAME,
    CONTROL_PREFIX,
    BoundaryProducer,
    ProducerResult,
    boundary_name,
    build_producer,
)
from agent.runtime.boundary.publication import (
    LinuxWriteOnceRename,
    ProtectedTree,
    PublicationRoots,
)
from agent.runtime.boundary.registry import BoundaryRegistry
from agent.runtime.boundary.snapshots import SnapshotLimits, SnapshotStore

__all__ = [
    "ANCHOR_NAME",
    "ANCHOR_SCHEMA",
    "BOUNDARY_SCHEMA",
    "CONTROL_PREFIX",
    "BoundaryError",
    "BoundaryErrorCode",
    "BoundaryProducer",
    "BoundaryRegistry",
    "CanonicalJson",
    "FirstJournal",
    "JournalReader",
    "JournalSlot",
    "LinuxWriteOnceRename",
    "NextWriter",
    "PreviousWriter",
    "ProcessBoundary",
    "ProducerResult",
    "ProtectedTree",
    "PublicationRoots",
    "SnapshotEntry",
    "SnapshotLimits",
    "SnapshotStore",
    "WriterAllocation",
    "WriterAnchor",
    "WriterBackend",
    "WriterBinding",
    "WriterLauncher",
    "boundary_name",
    "build_producer",
]
