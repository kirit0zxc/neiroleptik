"""Чистое отображение Cowrie JSONL; прежний collector доступен по старому импорту."""

import json
from dataclasses import dataclass
from uuid import NAMESPACE_URL, UUID, uuid5

from agent.telemetry.adapters.cowrie_collector import CowrieCollector as CowrieCollector
from agent.telemetry.schemas import MAX_DATA_BYTES, Event, Source, compact_json


class CowrieMappingError(ValueError):
    """Стабильный код без исходных полей и сообщений внешних валидаторов."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class CowrieBinding:
    """Доверенная привязка; подтверждение start выполняет будущий runtime wiring."""

    agent_id: UUID
    deployment_id: UUID
    instance_id: UUID
    profile_revision: int
    service_port: int

    def __post_init__(self) -> None:
        if not all(
            isinstance(v, UUID) for v in (self.agent_id, self.deployment_id, self.instance_id)
        ):
            raise ValueError("Нужны UUID доверенной привязки")
        if type(self.profile_revision) is not int or self.profile_revision < 1:
            raise ValueError("Нужна положительная целая revision")
        if type(self.service_port) is not int or not 1 <= self.service_port <= 65535:
            raise ValueError("Нужен целый порт 1..65535")


class CowrieAdapter:
    """Не читает файлы и не меняет storage; идентичность задаётся физической позицией."""

    _TYPES = {
        "cowrie.session.connect": "connection.opened",
        "cowrie.login.failed": "auth.attempt",
        "cowrie.login.success": "auth.attempt",
        "cowrie.command.input": "session.command",
        "cowrie.session.closed": "session.closed",
    }

    @staticmethod
    def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Повтор ключа")
            result[key] = value
        return result

    @staticmethod
    def _constant(value: str) -> None:
        raise ValueError("Нечисловая константа JSON")

    @staticmethod
    def _name(value: object) -> str:
        if not isinstance(value, str) or not 1 <= len(value.encode("utf-8")) <= 128:
            raise ValueError("Неверное имя")
        return value

    def map_record(
        self, raw: bytes, *, binding: CowrieBinding, journal_id: UUID, offset: int
    ) -> Event | None:
        """Полная неверная строка даёт безопасную ошибку, неизвестный тип — None."""
        if (
            not isinstance(journal_id, UUID)
            or type(offset) is not int
            or not 0 <= offset <= 9007199254740991
        ):
            raise CowrieMappingError("INVALID_POSITION")
        if not isinstance(raw, bytes):
            raise CowrieMappingError("INVALID_RECORD")
        if len(raw) > 65536:
            raise CowrieMappingError("RECORD_LIMIT")
        # Исключение поднимается за пределами except: traceback не хранит цепочку
        # JSONDecodeError/ValidationError, способную раскрыть пароль и исходник.
        code = "INVALID_RECORD"
        try:
            if not isinstance(binding, CowrieBinding):
                raise ValueError("Неверная привязка")
            if not raw.endswith(b"\n") or b"\n" in raw[:-1]:
                raise ValueError("Нужна одна строка")
            record = json.loads(
                raw.decode("utf-8"), object_pairs_hook=self._object, parse_constant=self._constant
            )
            if not isinstance(record, dict):
                raise ValueError("Нужен объект")
            # Проверяет суррогаты также в неизвестных полях и именах ключей.
            compact_json(record).encode("utf-8")
            eventid = self._name(record.get("eventid"))
            session = self._name(record.get("session"))
            ip, port = record.get("src_ip"), record.get("src_port")
            if ("src_ip" in record) != ("src_port" in record):
                raise ValueError("Неполная пара источника")
            source = (
                None
                if ip is None and port is None
                else Source.model_validate({"ip": ip, "port": port})
            )
            # Сначала общий конверт через тот же Event validator, даже для ignored.
            identity = [str(binding.agent_id), str(binding.deployment_id), str(binding.instance_id)]
            event = Event.model_validate(
                {
                    "event_id": uuid5(
                        NAMESPACE_URL,
                        compact_json(identity + ["cowrie-jsonl/1", str(journal_id), offset]),
                    ),
                    "agent_id": binding.agent_id,
                    "deployment_id": binding.deployment_id,
                    "instance_id": binding.instance_id,
                    "profile_revision": binding.profile_revision,
                    "session_id": uuid5(
                        NAMESPACE_URL, compact_json(identity + ["cowrie-session/1", session])
                    ),
                    "source_seq": offset,
                    "occurred_at": record.get("timestamp"),
                    "source": source,
                    "service": {"protocol": "ssh", "port": binding.service_port},
                    "type": self._TYPES.get(eventid, "runtime.state"),
                    "data": {},
                    "artifact_ids": [],
                }
            )
            if eventid not in self._TYPES:
                return None
            data: dict[str, str | bool] = {}
            fields = (
                ("username", "password")
                if event.type == "auth.attempt"
                else ("input",)
                if event.type == "session.command"
                else ()
            )
            for field in fields:
                value = record.get(field)
                if not isinstance(value, str):
                    raise ValueError("Нужна строка")
                data[field] = value
            if event.type == "auth.attempt":
                data["success"] = eventid == "cowrie.login.success"
            if len(compact_json(data).encode("utf-8")) > MAX_DATA_BYTES:
                code = "DATA_LIMIT"
            else:
                return Event.model_validate({**event.model_dump(), "data": data})
        except (ValueError, TypeError, RecursionError, OverflowError):
            pass
        raise CowrieMappingError(code)
