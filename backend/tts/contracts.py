"""Валидация данных из specs/openapi.json."""
from dataclasses import dataclass


@dataclass(frozen=True)
class SpeechRequest:
    """Нормализованная короткая реплика; клиент не задаёт пути модели."""
    text: str
    rate: float = 1.0
    MAX_TEXT = 400

    @classmethod
    def parse(cls, data):
        if not isinstance(data, dict) or set(data) - {"text", "rate"}:
            raise ValueError("Нужен объект с text и необязательным rate.")
        text = data.get("text")
        if not isinstance(text, str) or not 1 <= len(text) <= cls.MAX_TEXT or not text.strip():
            raise ValueError("Текст должен содержать от 1 до 400 символов.")
        rate = data.get("rate", 1.0)
        # Сначала проверяем диапазон: огромный JSON integer нельзя переводить во float.
        if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not 0.5 <= rate <= 2:
            raise ValueError("Темп должен быть числом от 0.5 до 2.")
        return cls(text.strip(), float(rate))
