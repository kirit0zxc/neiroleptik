"""Создаёт только локальный .env, не печатает секреты и не перезаписывает существующий файл."""

import base64
import secrets
from pathlib import Path


class LocalEnvironment:
    @staticmethod
    def create() -> None:
        root = Path(__file__).resolve().parents[1]
        password = secrets.token_hex(24)
        values = {
            "HF_ENVIRONMENT": "development",
            "HF_COOKIE_SECURE": "false",
            "HF_ALLOWED_ORIGINS": '["http://localhost:5173","http://localhost:8000"]',
            "HF_DB_PASSWORD": password,
            "HF_DATABASE_URL": f"postgresql+psycopg://honeyforge:{password}@127.0.0.1:55432/honeyforge",
            "HF_TEST_DATABASE_URL": f"postgresql+psycopg://honeyforge:{password}@127.0.0.1:55432/honeyforge_test",
            "HF_SESSION_SECRET": secrets.token_urlsafe(48),
            "HF_CREDENTIAL_KEY": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
        }
        with (root / ".env").open("x", encoding="utf-8") as output:
            output.write(
                "\n".join(
                    f"{key}={chr(39) + value + chr(39) if key == 'HF_ALLOWED_ORIGINS' else value}"
                    for key, value in values.items()
                )
                + "\n"
            )
        print("Локальный .env создан; секреты не выводятся. Не используйте dev-настройки публично.")


if __name__ == "__main__":
    LocalEnvironment.create()
