"""Проверяемая конфигурация центра; секреты задаются окружением."""

from functools import lru_cache
from typing import Literal

from cryptography.fernet import Fernet
from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Параметры локального и серверного запуска без небезопасных секретов по умолчанию."""

    model_config = SettingsConfigDict(env_prefix="HF_", extra="ignore")
    database_url: SecretStr
    session_secret: SecretStr
    credential_key: SecretStr
    environment: Literal["development", "production", "test"] = "production"
    allowed_origins: list[str] = ["http://localhost:5173"]
    cookie_secure: bool = True
    session_hours: int = 8

    @field_validator("database_url")
    @classmethod
    def validate_database(cls, value: SecretStr) -> SecretStr:
        """SQLite не подменяет PostgreSQL в приложении и интеграционных тестах."""
        if not value.get_secret_value().startswith("postgresql+psycopg://"):
            raise ValueError("Требуется PostgreSQL URL с драйвером psycopg")
        return value

    @field_validator("session_secret")
    @classmethod
    def validate_secret(cls, value: SecretStr) -> SecretStr:
        """Короткий ключ не допускается даже для dev-окружения."""
        if len(value.get_secret_value()) < 32:
            raise ValueError("Ключ сессий должен содержать не менее 32 символов")
        return value

    @field_validator("credential_key")
    @classmethod
    def validate_credential_key(cls, value: SecretStr) -> SecretStr:
        """Fernet проверяет длину и кодировку ключа до старта сервера."""
        Fernet(value.get_secret_value().encode())
        return value

    @field_validator("session_hours")
    @classmethod
    def validate_ttl(cls, value: int) -> int:
        if not 1 <= value <= 24:
            raise ValueError("Срок сессии: от 1 до 24 часов")
        return value

    @model_validator(mode="after")
    def validate_production(self) -> "Settings":
        """Production не допускает HTTP origins или незащищённую cookie."""
        if not self.allowed_origins or any(
            origin == "*" or origin.endswith("/") for origin in self.allowed_origins
        ):
            raise ValueError("Нужны точные origins без завершающего слэша")
        if self.environment == "production" and (
            not self.cookie_secure
            or any(not origin.startswith("https://") for origin in self.allowed_origins)
        ):
            raise ValueError("Production требует HTTPS origins и Secure cookie")
        return self


@lru_cache
def get_settings() -> Settings:
    """Загружает одну проверенную конфигурацию на процесс."""
    return Settings()
