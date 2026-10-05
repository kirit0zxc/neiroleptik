"""Проверяем размер и формат TTS до разбора форм в CSRF middleware."""
from django.conf import settings
from django.core.exceptions import RequestDataTooBig
from .errors import ApiError


class SpeechRequestMiddleware:
    """Неподдерживаемые форматы не попадают в парсеры форм Django."""
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method == "POST" and request.path_info == "/api/tts/":
            try:
                # Django кеширует тело; CSRF и view используют уже ограниченные данные.
                if len(request.body) > settings.DATA_UPLOAD_MAX_MEMORY_SIZE:
                    raise RequestDataTooBig()
            except RequestDataTooBig:
                return ApiError("payload_too_large", "Тело запроса превышает 16 КиБ.", 413)
            if request.content_type != "application/json":
                return ApiError("unsupported_media_type", "Нужен application/json.", 415)
        return self.get_response(request)
