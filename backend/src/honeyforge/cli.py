"""Служебные операции центра, недоступные через публичную регистрацию."""

import argparse
import getpass
import json
import os
import re
from pathlib import Path

from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.orm import Session

from honeyforge.auth.security import Security
from honeyforge.config import Settings, get_settings
from honeyforge.control.audit import AuditService
from honeyforge.db import get_engine
from honeyforge.main import create_app
from honeyforge.models import User


class ControlCLI:
    """Создаёт пользователей и экспортирует единый OpenAPI без соединения с БД."""

    @staticmethod
    def export_openapi(path: Path, check: bool) -> int:
        settings = Settings(
            database_url="postgresql+psycopg://schema:schema@localhost/schema",
            session_secret="schema-only-not-for-serving-000000000000",
            credential_key=Fernet.generate_key().decode(),
            environment="development",
            cookie_secure=False,
        )
        content = json.dumps(create_app(settings).openapi(), ensure_ascii=False, indent=2) + "\n"
        if check:
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                print("OpenAPI устарел: выполните export-openapi")
                return 1
            print("OpenAPI соответствует приложению")
            return 0
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        print(f"OpenAPI: {path}")
        return 0

    @staticmethod
    def create_user(username: str, role: str) -> int:
        username = username.lower()
        if not re.fullmatch(r"[a-z0-9_.-]{1,64}", username):
            raise ValueError("Имя пользователя: 1..64 ASCII-символов a-z, 0-9, _, ., -")
        password = os.environ.get("HF_BOOTSTRAP_PASSWORD") or getpass.getpass("Пароль: ")
        if not 12 <= len(password) <= 256:
            raise ValueError("Пароль должен содержать 12..256 символов")
        security = Security(get_settings())
        with Session(get_engine()) as session:
            if session.scalar(select(User).where(User.username == username)):
                raise ValueError("Пользователь уже существует; пароль не изменён")
            user = User(
                username=username, role=role, password_hash=security.hash_password(password)
            )
            session.add(user)
            session.flush()
            AuditService.add(session, "user.created_cli", target=user.id, details={"role": role})
            session.commit()
        print(f"Пользователь {username} создан с ролью {role}")
        return 0

    @classmethod
    def run(cls) -> int:
        parser = argparse.ArgumentParser(description="Служебные операции HoneyForge")
        commands = parser.add_subparsers(dest="command", required=True)
        export = commands.add_parser("export-openapi")
        export.add_argument(
            "--path",
            type=Path,
            default=Path(__file__).resolve().parents[3] / "contracts/openapi.json",
        )
        export.add_argument("--check", action="store_true")
        create = commands.add_parser("create-user")
        create.add_argument("--username", required=True)
        create.add_argument("--role", choices=["admin", "operator", "viewer"], required=True)
        args = parser.parse_args()
        if args.command == "export-openapi":
            return cls.export_openapi(args.path, args.check)
        return cls.create_user(args.username, args.role)


if __name__ == "__main__":
    raise SystemExit(ControlCLI.run())
