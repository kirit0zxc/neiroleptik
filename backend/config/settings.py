"""Настройки Django: локальный запуск и PostgreSQL через окружение."""
import os
from pathlib import Path
import dj_database_url
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")
DEBUG = os.environ.get("DJANGO_DEBUG", "false").lower() == "true"
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    if not DEBUG:
        raise RuntimeError("Задай DJANGO_SECRET_KEY; для локального запуска также DJANGO_DEBUG=true.")
    SECRET_KEY = "local-development-only-do-not-deploy"
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,testserver").split(",")
INSTALLED_APPS = ["django.contrib.contenttypes", "tts"]
MIDDLEWARE = ["django.middleware.security.SecurityMiddleware",
              "django.middleware.common.CommonMiddleware",
              "tts.middleware.SpeechRequestMiddleware",
              "django.middleware.csrf.CsrfViewMiddleware"]
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"
DATABASES = {"default": dj_database_url.config(
    default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}", conn_max_age=60)}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LANGUAGE_CODE = "ru-ru"
TIME_ZONE = "Europe/Moscow"
USE_TZ = True
CSRF_FAILURE_VIEW = "tts.views.csrf_failure"
CSRF_COOKIE_HTTPONLY = True
CSRF_TRUSTED_ORIGINS = [v for v in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if v]
CSRF_COOKIE_SECURE = not DEBUG
SECURE_CONTENT_TYPE_NOSNIFF = True
DATA_UPLOAD_MAX_MEMORY_SIZE = 16 * 1024
TTS_MODEL_PATH = os.environ.get("TTS_MODEL_PATH", str(BASE_DIR / "voices" / "ru_RU-irina-medium.onnx"))
