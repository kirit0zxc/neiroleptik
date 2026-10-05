"""Piper: ленивая модель, WAV, отсутствие очереди и безопасный сбой."""
import io
import tempfile
import wave
from pathlib import Path
from unittest.mock import Mock, patch
from django.test import SimpleTestCase, override_settings
from tts.contracts import SpeechRequest
from tts.service import PiperSpeechService, SpeechUnavailable


class PiperServiceTests(SimpleTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.model = Path(self.tmp.name) / "voice.onnx"
        self.model.touch()
        Path(str(self.model) + ".json").write_text("{}")
        self.service = PiperSpeechService()
        self.settings = override_settings(TTS_MODEL_PATH=str(self.model))
        self.settings.enable()
        self.addCleanup(self.settings.disable)

    def test_model_loaded_once_rate_applied_and_audio_stays_in_memory(self):
        voice = Mock()
        def synth(text, wav, syn_config):
            self.assertEqual(text, "Привет")
            self.assertEqual(syn_config.length_scale, 0.5)
            wav.setparams((1, 2, 22050, 0, "NONE", "not compressed"))
            wav.writeframes(b"\0\0" * 8)
        voice.synthesize_wav.side_effect = synth
        with patch("piper.PiperVoice.load", return_value=voice) as load:
            for _ in range(2):
                result = self.service.synthesize(SpeechRequest("Привет", 2))
                with wave.open(io.BytesIO(result)) as wav:
                    self.assertEqual(wav.getnframes(), 8)
            load.assert_called_once_with(str(self.model))
        self.assertEqual(len(list(self.model.parent.iterdir())), 2)

    def test_missing_model_returns_unavailable_without_loading(self):
        with override_settings(TTS_MODEL_PATH=str(self.model.parent / "missing.onnx")):
            with patch("piper.PiperVoice.load") as load:
                with self.assertRaises(SpeechUnavailable):
                    self.service.synthesize(SpeechRequest("Привет"))
                load.assert_not_called()

    def test_busy_process_fails_instead_of_queuing(self):
        self.service._lock.acquire()
        try:
            with self.assertRaises(SpeechUnavailable):
                self.service.synthesize(SpeechRequest("Привет"))
        finally:
            self.service._lock.release()

    def test_synthesis_failure_releases_lock(self):
        with patch("piper.PiperVoice.load", side_effect=RuntimeError("private path")):
            with self.assertRaises(SpeechUnavailable) as error:
                self.service.synthesize(SpeechRequest("Привет"))
            self.assertNotIn("private", str(error.exception))
            self.assertTrue(self.service._lock.acquire(blocking=False))
            self.service._lock.release()
