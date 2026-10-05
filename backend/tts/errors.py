"""Единые ошибки HTTP для middleware и представлений."""
from django.http import JsonResponse


class ApiError(JsonResponse):
    """JSON-ошибка без внутренних исключений и путей."""
    def __init__(self, code, message, status):
        super().__init__({"error": {"code": code, "message": message}}, status=status)
        self["Cache-Control"] = "no-store"
