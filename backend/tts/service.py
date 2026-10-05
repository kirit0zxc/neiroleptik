"""Piper без внешних запросов: одна модель и один синтез на процесс."""
import io
import threading
import wave
from pathlib import Path
from django.conf import settings


class SpeechUnavailable(Exception):
    """Синтез временно недоступен; наружу не раскрываются детали модели."""


class PiperSpeechService:
    """Ленивая загрузка ONNX и WAV в памяти, без хранения реплик."""
    def __init__(self):
        self._voice = None
        self._path = None
        self._lock = threading.Lock()

    def synthesize(self, request):
        if not self._lock.acquire(blocking=False):
            raise SpeechUnavailable("Синтез занят. Повторите запрос позже.")
        try:
            model = Path(settings.TTS_MODEL_PATH).resolve()
            if not model.is_file() or not Path(str(model) + ".json").is_file():
                raise SpeechUnavailable("Модель голоса не установлена.")
            from piper import PiperVoice, SynthesisConfig
            if self._voice is None or self._path != model:
                self._voice = PiperVoice.load(str(model))
                self._path = model
            output = io.BytesIO()
            with wave.open(output, "wb") as wav:
                self._voice.synthesize_wav(request.text, wav,
                    syn_config=SynthesisConfig(length_scale=1 / request.rate))
            return output.getvalue()
        except SpeechUnavailable:
            raise
        except Exception as error:
            # Текст и пути не включаем в сообщение HTTP или логи.
            raise SpeechUnavailable("Не удалось синтезировать речь.") from error
        finally:
            self._lock.release()


speech_service = PiperSpeechService()
