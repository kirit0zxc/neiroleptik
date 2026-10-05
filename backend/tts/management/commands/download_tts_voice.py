"""Явная загрузка фиксированного голоса, вне HTTP и обычных тестов CI."""
from pathlib import Path
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Скачать голос Piper ru_RU-irina-medium в backend/voices."

    def add_arguments(self, parser):
        parser.add_argument("--directory", default=str(Path(__file__).resolve().parents[3] / "voices"))

    def handle(self, *args, **options):
        from piper.download_voices import download_voice
        directory = Path(options["directory"]).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        try:
            download_voice("ru_RU-irina-medium", directory)
        except Exception as error:
            raise CommandError("Не удалось скачать голос. Проверьте подключение.") from error
        self.stdout.write(self.style.SUCCESS(f"Голос установлен: {directory}"))
