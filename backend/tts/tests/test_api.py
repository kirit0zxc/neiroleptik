"""API-контракт: настоящий CSRF и WAV, ошибки до вызова синтезатора."""
import io
import json
import wave
from pathlib import Path
from unittest.mock import patch
from django.test import Client, SimpleTestCase
from tts.contracts import SpeechRequest
from tts.service import SpeechUnavailable


class SpeechApiTests(SimpleTestCase):
    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.token = self.client.get("/api/tts/csrf/").json()["csrfToken"]
        self.synth = patch("tts.views.speech_service.synthesize")
        self.mock_synth = self.synth.start()
        self.addCleanup(self.synth.stop)

    def post(self, body, **kwargs):
        return self.client.post("/api/tts/", data=json.dumps(body),
            content_type="application/json", HTTP_X_CSRFTOKEN=self.token, **kwargs)

    def test_valid_request_returns_playable_wav_and_no_store(self):
        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setparams((1, 2, 22050, 0, "NONE", "not compressed"))
            wav.writeframes(b"\0\0" * 32)
        self.mock_synth.return_value = output.getvalue()
        response = self.post({"text": "  Привет  ", "rate": 1.25})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "audio/wav")
        self.assertEqual(response["Cache-Control"], "no-store")
        with wave.open(io.BytesIO(response.content)) as wav:
            self.assertEqual(wav.getnframes(), 32)
        self.mock_synth.assert_called_once_with(SpeechRequest("Привет", 1.25))

    def test_bad_data_does_not_call_synthesis(self):
        bad = [None, [], {}, {"text": ""}, {"text": " "}, {"text": 7},
               {"text": "я" * 401}, {"text": "да", "rate": True},
               {"text": "да", "rate": "1"}, {"text": "да", "rate": 0.4},
               {"text": "да", "rate": 2.1}, {"text": "да", "rate": float("nan")},
               {"text": "да", "model": "/private/file"}]
        for data in bad:
            with self.subTest(data=data):
                response = self.post(data)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error"]["code"], "invalid_request")
        self.mock_synth.assert_not_called()

    def test_csrf_is_required(self):
        response = Client(enforce_csrf_checks=True).post(
            "/api/tts/", data='{"text":"Привет"}', content_type="application/json")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "csrf_failed")
        self.mock_synth.assert_not_called()

    def test_huge_rate_returns_json_error_without_synthesis(self):
        for rate in (10 ** 400, -(10 ** 400)):
            with self.subTest(sign=rate > 0):
                response = self.post({"text": "Привет", "rate": rate})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error"]["code"], "invalid_request")
        self.mock_synth.assert_not_called()

    def test_deep_json_returns_json_error_without_synthesis(self):
        # Ниже лимита тела, но превышает безопасную глубину JSON-парсера.
        response = self.client.post("/api/tts/", data="[" * 7000 + "0" + "]" * 7000,
            content_type="application/json", HTTP_X_CSRFTOKEN=self.token)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_request")
        self.mock_synth.assert_not_called()

    def test_oversized_form_is_rejected_before_csrf_parses_it(self):
        for headers in ({}, {"HTTP_X_CSRFTOKEN": self.token}):
            with self.subTest(has_token=bool(headers)):
                response = self.client.post("/api/tts/", data="text=" + "x" * 17000,
                    content_type="application/x-www-form-urlencoded", **headers)
                self.assertEqual(response.status_code, 413)
                self.assertEqual(response.json()["error"]["code"], "payload_too_large")
        self.mock_synth.assert_not_called()

    def test_raw_text_length_is_checked_before_trimming(self):
        response = self.post({"text": " " + "я" * 400 + " "})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_request")
        self.mock_synth.assert_not_called()
        self.assertEqual(SpeechRequest.parse({"text": " " + "я" * 398 + " "}).text, "я" * 398)

    def test_unsupported_forms_are_rejected_before_csrf_parsing(self):
        # Маленькие формы могут превысить лимит полей/файлов или сломать парсер.
        files = "".join(
            '--voice\r\nContent-Disposition: form-data; name="f"; filename="x"\r\n\r\n\r\n'
            for _ in range(101)) + "--voice--\r\n"
        bodies = [
            ("application/x-www-form-urlencoded", "&".join("a=1" for _ in range(1001))),
            ("application/x-www-form-urlencoded; charset=iso-8859-1", "text=hi"),
            ("multipart/form-data; boundary=", "tiny"),
            ("multipart/form-data; boundary=voice", files),
        ]
        for content_type, body in bodies:
            for headers in ({}, {"HTTP_X_CSRFTOKEN": self.token}):
                with self.subTest(content_type=content_type, has_token=bool(headers)):
                    response = self.client.post("/api/tts/", data=body,
                        content_type=content_type, **headers)
                    self.assertEqual(response.status_code, 415)
                    self.assertEqual(response.json()["error"]["code"], "unsupported_media_type")
        self.mock_synth.assert_not_called()

    def test_size_media_type_malformed_json_and_methods(self):
        headers = {"HTTP_X_CSRFTOKEN": self.token}
        response = self.client.post("/api/tts/", data='{"text":', content_type="application/json", **headers)
        self.assertEqual(response.status_code, 400)
        response = self.client.post("/api/tts/", data="x" * (16 * 1024 + 1), content_type="application/json", **headers)
        self.assertEqual(response.status_code, 413)
        response = self.client.post("/api/tts/", data="hello", content_type="text/plain", **headers)
        self.assertEqual(response.status_code, 415)
        self.assertEqual(self.client.get("/api/tts/").status_code, 405)
        self.mock_synth.assert_not_called()

    def test_unavailable_voice_has_no_private_details(self):
        self.mock_synth.side_effect = SpeechUnavailable("/private/model/path")
        response = self.post({"text": "Привет"})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private", response.content.decode())

    def test_boundary_values_match_openapi(self):
        spec = json.loads((Path(__file__).resolve().parents[3] / "specs/openapi.json").read_text(encoding="utf-8"))
        schema = spec["components"]["schemas"]["SpeechRequest"]
        self.assertEqual(schema["properties"]["text"]["maxLength"], SpeechRequest.MAX_TEXT)
        for rate in (schema["properties"]["rate"]["minimum"], schema["properties"]["rate"]["maximum"]):
            self.assertEqual(SpeechRequest.parse({"text": "я" * SpeechRequest.MAX_TEXT, "rate": rate}).rate, rate)
        self.assertEqual(set(schema["properties"]), {"text", "rate"})
