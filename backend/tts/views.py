"""HTTP-адаптер голоса: CSRF, ограничение размера, единый формат ошибок."""
import json
from django.core.exceptions import RequestDataTooBig
from django.http import HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.views import View
from .contracts import SpeechRequest
from .errors import ApiError
from .service import speech_service, SpeechUnavailable


class ApiView(View):
    """Ошибки метода тоже соответствуют OpenAPI."""
    def http_method_not_allowed(self, request, *args, **kwargs):
        response = ApiError("method_not_allowed", "Метод не поддерживается.", 405)
        response["Allow"] = ", ".join(self._allowed_methods())
        return response


class CsrfView(ApiView):
    http_method_names = ["get", "options"]

    def get(self, request):
        response = JsonResponse({"csrfToken": get_token(request)})
        response["Cache-Control"] = "no-store"
        return response


class SpeechView(ApiView):
    http_method_names = ["post", "options"]

    def post(self, request):
        if request.content_type != "application/json":
            return ApiError("unsupported_media_type", "Нужен application/json.", 415)
        try:
            raw = request.body
            if len(raw) > 16 * 1024:
                raise RequestDataTooBig()
            data = SpeechRequest.parse(json.loads(raw))
        except RequestDataTooBig:
            return ApiError("payload_too_large", "Тело запроса превышает 16 КиБ.", 413)
        except (ValueError, UnicodeError, RecursionError):
            return ApiError("invalid_request", "Проверьте текст и темп реплики.", 400)
        try:
            audio = speech_service.synthesize(data)
        except SpeechUnavailable:
            return ApiError("tts_unavailable", "Голос временно недоступен.", 503)
        response = HttpResponse(audio, content_type="audio/wav")
        response["Cache-Control"] = "no-store"
        response["Content-Disposition"] = 'inline; filename="yumi.wav"'
        return response


def csrf_failure(request, reason=""):
    """Сигнатура точки расширения Django; причина не публикуется."""
    return ApiError("csrf_failed", "Обновите CSRF-токен и повторите запрос.", 403)
