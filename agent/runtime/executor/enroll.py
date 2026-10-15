"""Однократная регистрация агента и сохранение Bearer в новый закрытый файл.

Узел объявляет центру уровни, которые на нём действительно можно запускать: по
умолчанию только Low, а Medium — явным `--level medium`, потому что Cowrie требует
заранее доставленного образа и каталога состояния на этом хосте.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from agent.runtime.drivers.registry import SUPPORTED_LEVELS
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
    """Регистрация использует существующий API без Authorization и объявленные уровни."""

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
    def capabilities(levels: Sequence[str]) -> list[str]:
        """Уровни узла в каноническом порядке; неизвестный уровень не объявляется.

        Центр принимает только известные ему значения, а агент — только уровни,
        для которых у него есть драйвер (`SUPPORTED_LEVELS`).
        """
        unique = set(levels)
        if not unique or not unique <= set(SUPPORTED_LEVELS):
            raise ClientError("INVALID_REQUEST")
        return [level for level in SUPPORTED_LEVELS if level in unique]

    @classmethod
    def validate_input(cls, enrollment_token: str, name: str, levels: Sequence[str]) -> None:
        """Некорректный локальный ввод отклоняется до сетевого запроса."""
        cls.capabilities(levels)
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
        self, enrollment_token: str, name: str, levels: Sequence[str] = ("low",)
    ) -> EnrollmentReply:
        """Ответ не повторяется автоматически: одноразовый token мог быть уже израсходован."""
        self.validate_input(enrollment_token, name, levels)
        value = await self._send(
            "POST",
            "/agent/v1/enroll",
            {
                "enrollment_token": enrollment_token,
                "name": name,
                "capabilities": self.capabilities(levels),
            },
        )
        return EnrollmentReply.parse(value)


async def run(args: argparse.Namespace, enrollment_token: str) -> EnrollmentReply:
    """Резервирование файла предшествует регистрации; готовый Bearer не выводится."""
    client = EnrollmentClient(args.center, allow_http=args.allow_http_loopback)
    client.validate_input(enrollment_token, args.name, args.level)
    with TokenFile(args.token_file).reserve() as destination:
        reply = await client.enroll(enrollment_token, args.name, args.level)
        destination.write(reply.agent_token)
        return reply


def main() -> None:
    """Секрет принимается только из окружения и не наследуется дочерними процессами."""
    parser = argparse.ArgumentParser(description="Регистрация агента HoneyForge")
    parser.add_argument("--center", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument(
        "--level",
        action="append",
        choices=list(SUPPORTED_LEVELS),
        help="Уровень ловушки этого узла; можно указать несколько раз (по умолчанию low)",
    )
    parser.add_argument("--allow-http-loopback", action="store_true")
    args = parser.parse_args()
    if not args.level:
        args.level = ["low"]
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
    levels = ", ".join(EnrollmentClient.capabilities(args.level))
    print(
        f"Узел зарегистрирован: {reply.host_id}; уровни: {levels}; agent token сохранён в файл",
        flush=True,
    )


if __name__ == "__main__":
    main()
