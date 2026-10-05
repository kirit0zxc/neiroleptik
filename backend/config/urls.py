"""Маршруты согласованного API голоса."""
from django.urls import path
from tts.views import SpeechView, CsrfView

urlpatterns = [
    path("api/tts/", SpeechView.as_view(), name="tts-synthesize"),
    path("api/tts/csrf/", CsrfView.as_view(), name="tts-csrf"),
]
