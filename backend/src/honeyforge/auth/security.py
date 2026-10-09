"""Криптографические операции на стандартных библиотеках без собственных шифров."""

import hashlib
import hmac
import json
import secrets
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet

from honeyforge.config import Settings


class Security:
    """Секреты разделены по назначению: сессии, агенты и credentials профилей."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.hasher = PasswordHasher()
        self.cipher = Fernet(settings.credential_key.get_secret_value().encode())

    def hash_password(self, password: str) -> str:
        return self.hasher.hash(password)

    def verify_password(self, password_hash: str, password: str) -> bool:
        try:
            return self.hasher.verify(password_hash, password)
        except (VerificationError, InvalidHashError):
            return False

    @staticmethod
    def token(prefix: str) -> str:
        return prefix + secrets.token_urlsafe(32)

    @staticmethod
    def digest(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def csrf_token(self, session_token: str) -> str:
        """CSRF привязан к недоступному JavaScript session token."""
        return hmac.new(
            self.settings.session_secret.get_secret_value().encode(),
            ("csrf:" + session_token).encode(),
            hashlib.sha256,
        ).hexdigest()

    def ip_key(self, ip: str) -> str:
        """Сырые IP для ограничения входа не хранятся в таблице счётчиков."""
        return hmac.new(
            self.settings.session_secret.get_secret_value().encode(),
            ("login:" + ip).encode(),
            hashlib.sha256,
        ).hexdigest()

    def encrypt_credentials(self, credentials: list[dict[str, str]]) -> str:
        return self.cipher.encrypt(json.dumps(credentials).encode()).decode()

    def decrypt_credentials(self, ciphertext: str) -> list[dict[str, Any]]:
        value: list[dict[str, Any]] = json.loads(self.cipher.decrypt(ciphertext.encode()))
        return value
