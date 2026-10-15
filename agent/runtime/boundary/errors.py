"""Коды отказа producer доверенной границы процесса Low.

Коды предложены спецификацией `specs/journal-process-boundary-proposal.md` (§5, §8)
как внутренние: их размещение в wire/API согласует владелец collector. Сообщение
не содержит сырых байт журнала, секретов профиля и вывода Docker.
"""

from __future__ import annotations

from enum import StrEnum


class BoundaryErrorCode(StrEnum):
    """Причина, по которой переход не доказан; неизвестный исход не выдаётся за отказ."""

    # Старый writer не ограждён: Engine не подтвердил удаление либо остались
    # другие ресурсы с RW доступом к каталогу слота (§3 шаг 2).
    PREVIOUS_WRITER_NOT_FENCED = "PREVIOUS_WRITER_NOT_FENCED"
    # Новый writer запущен, но его идентичность или первый заголовок не привязаны (§3 шаг 4).
    NEXT_WRITER_UNBOUND = "NEXT_WRITER_UNBOUND"
    # Исход операции Engine неизвестен: ошибка, тайм-аут, другой daemon, утрата истории (§2).
    RUNTIME_OBSERVATION_UNKNOWN = "RUNTIME_OBSERVATION_UNKNOWN"
    # Привязка агента/назначения/экземпляра/ревизии не совпала с ожидаемой (§5).
    BOUNDARY_BINDING_MISMATCH = "BOUNDARY_BINDING_MISMATCH"
    # Набор журналов закрываемого writer недоступен или не образует целую цепочку (§8.3).
    BOUNDARY_SNAPSHOT_UNAVAILABLE = "BOUNDARY_SNAPSHOT_UNAVAILABLE"
    # Счётчик переходов, предыдущий boundary или anchor binding конфликтуют (§5, §8.1).
    BOUNDARY_SEQUENCE_CONFLICT = "BOUNDARY_SEQUENCE_CONFLICT"
    # Опубликованное имя уже существует с другими байтами; перезаписи нет (§8.2).
    BOUNDARY_PUBLICATION_CONFLICT = "BOUNDARY_PUBLICATION_CONFLICT"
    # Превышен согласованный предел документа, числа журналов, хвоста или квоты (§8.5).
    LIMIT_EXCEEDED = "LIMIT_EXCEEDED"
    # Исходный журнал изменился во время копирования: ограждение неполно (§8.3).
    SOURCE_CHANGED = "SOURCE_CHANGED"
    # Producer выключен: не Linux, нет RENAME_NOREPLACE или корни настроены небезопасно (§8.2).
    PRODUCER_DISABLED = "PRODUCER_DISABLED"
    # Документ не прошёл строгую проверку формы или канонизации (§4).
    INVALID_DOCUMENT = "INVALID_DOCUMENT"
    # Registry недоступна или повреждена: новые документы binding не выдаются (§8.4).
    REGISTRY_UNAVAILABLE = "REGISTRY_UNAVAILABLE"


class BoundaryError(Exception):
    """Отказ producer с безопасным кодом; текст пригоден только для локальной диагностики."""

    def __init__(self, code: BoundaryErrorCode, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(detail or code.value)
