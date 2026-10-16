"""Ограниченный сбор JSON Cowrie с атомарным checkpoint в общей очереди."""

import json
import os
import stat
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from agent.telemetry.adapters.cowrie_tty import CowrieTtyCollector
from agent.telemetry.adapters.low import SourceRecord
from agent.telemetry.low_journal.models import JournalBinding
from agent.telemetry.schemas import Event, compact_json
from agent.telemetry.spool import SpoolRepository


class CowrieCollector:
    """Идентичность только из подтверждённого ACK; файл не задаёт deployment/instance."""

    def __init__(self, spool: SpoolRepository, root: Path) -> None:
        self._spool, self._root = spool, root

    def step(self, binding: JournalBinding) -> None:
        path = (
            self._root
            / "medium"
            / binding.deployment_id
            / binding.instance_id
            / "log"
            / "cowrie.json"
        )
        if not path.exists():
            return
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError("Cowrie: ожидается обычный файл")
        for part in (path, *path.parents):
            info = part.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("Cowrie: ссылка вместо локального журнала")
        source = f"cowrie:{binding.deployment_id}:{binding.instance_id}"
        config = (
            self._root
            / "medium-config"
            / binding.deployment_id
            / binding.instance_id
            / "profile.json"
        )
        port = 2222
        if config.exists():
            if config.stat().st_size > 32768:
                raise ValueError("Cowrie: слишком большая конфигурация")
            value = json.loads(config.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or type(value.get("port")) is not int:
                raise ValueError("Cowrie: некорректный порт конфигурации")
            port = value["port"]
        offset = self._spool.checkpoint(source)
        descriptor = os.open(
            path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        )
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size < offset:
                raise ValueError("Cowrie: журнал заменён или усечён")
            if info.st_size > 64 * 1024 * 1024:
                raise ValueError("Cowrie: превышен предел журнала")
            stream.seek(offset)
            for _ in range(100):
                line = stream.readline(65537)
                if len(line) > 65536:
                    raise ValueError("Cowrie: слишком длинная запись")
                if not line or not line.endswith(b"\n"):
                    break
                event = self._event(binding, source, offset, line, port)
                raw = json.loads(line)
                if raw.get("eventid") == "cowrie.session.closed":
                    # Manifest закрывает гонку: replay не выдаётся раньше всех его записей.
                    position = stream.tell()
                    stream.seek(0)
                    recordings = []
                    while stream.tell() < offset:
                        record_offset = stream.tell()
                        previous = json.loads(stream.readline(65537))
                        if not isinstance(previous, dict):
                            raise ValueError("Cowrie: некорректная запись manifest")
                        if previous.get("eventid") == "cowrie.log.closed" and previous.get(
                            "session"
                        ) == raw.get("session"):
                            record = self._event(
                                binding,
                                source,
                                record_offset,
                                compact_json(previous).encode(),
                                port,
                            )
                            recordings.append(str(record.event_id))
                        if len(recordings) > 128:
                            raise ValueError("Cowrie: превышен лимит записей одной сессии")
                    stream.seek(position)
                    event = event.model_copy(
                        update={"data": {**event.data, "replay_recordings": recordings}}
                    )
                if raw.get("eventid") == "cowrie.log.closed":
                    if not CowrieTtyCollector(self._spool).step(path.parent.parent, raw, event):
                        break
                self._spool.append(SourceRecord(source, offset, offset + len(line), event))
                offset += len(line)

    def _event(
        self, binding: JournalBinding, source: str, offset: int, line: bytes, port: int = 2222
    ) -> Event:
        raw = json.loads(line)
        if not isinstance(raw, dict) or not isinstance(raw.get("timestamp"), str):
            raise ValueError("Cowrie: ожидается объект события с timestamp")
        if not isinstance(raw.get("eventid"), str):
            raise ValueError("Cowrie: ожидается строковый eventid")
        kind = str(raw["eventid"])
        types = {
            "cowrie.session.connect": "connection.opened",
            "cowrie.login.success": "auth.attempt",
            "cowrie.login.failed": "auth.attempt",
            "cowrie.command.input": "session.command",
            "cowrie.session.closed": "session.closed",
        }
        event_type = types.get(kind, "runtime.state")
        data: dict[str, object] = {"capture_completeness": "unknown"}
        if event_type == "auth.attempt":
            data.update(
                username=str(raw.get("username", ""))[:128], success=kind == "cowrie.login.success"
            )
            if "password" in raw:
                data["password"] = "[REDACTED]"
        elif event_type == "session.command":
            command = str(raw.get("input", ""))
            data.update(input=command[:2048], truncated=len(command) > 2048)
        peer = None
        if raw.get("src_ip"):
            peer = {"ip": raw["src_ip"], "port": raw.get("src_port", 0)}
        identity = [str(self._spool.agent_id), binding.deployment_id, binding.instance_id]
        session = raw.get("session")
        return Event.model_validate(
            {
                "event_id": str(uuid5(NAMESPACE_URL, compact_json(identity + [source, offset]))),
                "agent_id": self._spool.agent_id,
                "deployment_id": UUID(binding.deployment_id),
                "instance_id": UUID(binding.instance_id),
                "profile_revision": binding.profile_revision,
                "session_id": str(
                    uuid5(NAMESPACE_URL, compact_json(identity + ["cowrie", session]))
                )
                if session
                else None,
                "source_seq": offset,
                "occurred_at": raw["timestamp"],
                "type": event_type,
                "source": peer,
                "service": {"protocol": "ssh", "port": port},
                "data": data,
                "artifact_ids": [],
            }
        )
