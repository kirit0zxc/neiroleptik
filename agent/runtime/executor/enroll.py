"""Однократная регистрация Low-агента и сохранение Bearer в новый закрытый файл."""

from __future__ import annotations

import argparse
import asyncio
import os
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from agent.runtime.executor.client import BoundedHttpTransport, ClientError
from agent.runtime.executor.tokens import TokenFile, TokenFileError, validate_agent_token


@dataclass(frozen=True)
class EnrollmentReply:
    """Agent token доступен для записи, но исключён из представления ответа."""

    host_id: str
    agent_token: str = field(repr=False)

    @classmethod
    def parse(cls, value: object) -> EnrollmentReply:
        if not isinstance(value, dict) or set(value) != {"host_id", "agent_token"}:
            raise ClientError("INVALID_RESPONSE")
        try:
            identifier = value["host_id"]
            if not isinstance(identifier, str):
                raise ValueError
            return cls(str(UUID(identifier)), validate_agent_token(value["agent_token"]))
        except (ValueError, TypeError, TokenFileError):
            raise ClientError("INVALID_RESPONSE") from None


class EnrollmentClient(BoundedHttpTransport):
    """Регистрация использует существующий API без Authorization и только Low."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 10.0,
        max_response_bytes: int = 1_048_576,
        allow_http: bool = False,
    ) -> None:
        super().__init__(
            base_url,
            timeout=timeout,
            max_response_bytes=max_response_bytes,
            allow_http=allow_http,
            success_status=201,
        )

    @staticmethod
    def validate_input(enrollment_token: str, name: str) -> None:
        """Некорректный локальный ввод отклоняется до сетевого запроса."""
        try:
            if (
                not isinstance(enrollment_token, str)
                or not 20 <= len(enrollment_token) <= 128
                or any(ord(c) <= 32 or ord(c) >= 127 for c in enrollment_token)
                or not isinstance(name, str)
                or not 1 <= len(name) <= 80
                or "\x00" in name
            ):
                raise ValueError
            name.encode("utf-8")
        except (ValueError, UnicodeError):
            raise ClientError("INVALID_REQUEST") from None

    async def enroll(
        self, enrollment_token: str, name: str, *, medium: bool = False
    ) -> EnrollmentReply:
        """Ответ не повторяется автоматически: одноразовый token мог быть уже израсходован."""
        self.validate_input(enrollment_token, name)
        value = await self._send(
            "POST",
            "/agent/v1/enroll",
            {
                "enrollment_token": enrollment_token,
                "name": name,
                "capabilities": ["low", "medium"] if medium else ["low"],
            },
        )
        return EnrollmentReply.parse(value)


async def run(args: argparse.Namespace, enrollment_token: str) -> EnrollmentReply:
    """Резервирование файла предшествует регистрации; готовый Bearer не выводится."""
    client = EnrollmentClient(args.center, allow_http=args.allow_http_loopback)
    client.validate_input(enrollment_token, args.name)
    with TokenFile(args.token_file).reserve() as destination:
        reply = await client.enroll(
            enrollment_token, args.name, medium=getattr(args, "medium", False)
        )
        destination.write(reply.agent_token)
        return reply


def main() -> None:
    """Секрет принимается только из окружения и не наследуется дочерними процессами."""
    parser = argparse.ArgumentParser(description="Регистрация Low-агента HoneyForge")
    parser.add_argument(
        "--medium", action="store_true", help="После проверки Cowrie объявить Medium"
    )
    parser.add_argument("--center", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument("--allow-http-loopback", action="store_true")
    args = parser.parse_args()
    token = os.environ.pop("HF_ENROLLMENT_TOKEN", None)
    if not token:
        parser.exit(1, "Задайте HF_ENROLLMENT_TOKEN в окружении процесса\n")
    try:
        reply = asyncio.run(run(args, token))
    except (KeyboardInterrupt, asyncio.CancelledError):
        parser.exit(1, "Регистрация прервана; результат проверьте у администратора центра\n")
    except (ClientError, TokenFileError, ValueError, OSError) as error:
        code = getattr(error, "code", "LOCAL_CONFIGURATION_ERROR")
        parser.exit(
            1,
            f"Регистрация остановлена: {code}. "
            "После сетевого запроса одноразовый token мог быть израсходован; "
            "новый token выдаёт администратор центра.\n",
        )
    print(f"Узел зарегистрирован: {reply.host_id}; agent token сохранён в файл", flush=True)


if __name__ == "__main__":
    main()
