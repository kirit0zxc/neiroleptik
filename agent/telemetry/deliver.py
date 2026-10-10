"""Запуск доставки существующего spool; сбор журнала Low подключается отдельно."""

import argparse
import json
import os
import sqlite3
import sys
import time
from dataclasses import asdict
from pathlib import Path
from uuid import UUID

from pydantic import SecretStr

from agent.telemetry.sender import HttpBatchTransport, TelemetrySender
from agent.telemetry.spool import SpoolRepository


def main(argv: list[str] | None = None) -> int:
    """Токен приходит из окружения, чтобы не попадать в аргументы процессов."""
    parser = argparse.ArgumentParser(description="Доставка существующей очереди телеметрии")
    parser.add_argument("--spool", required=True, type=Path)
    parser.add_argument("--agent-id", required=True, type=UUID)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument(
        "--once", action="store_true", help="Один шаг доставки без ожидания повторов"
    )
    args = parser.parse_args(argv)
    token = os.environ.get("HF_AGENT_TOKEN")
    if not token:
        parser.error("Задайте HF_AGENT_TOKEN в окружении")
    if not args.spool.is_file():
        parser.error("Нужен существующий файл spool")
    try:
        transport = HttpBatchTransport(args.endpoint, SecretStr(token), args.timeout)
    except ValueError:
        parser.error("Проверьте endpoint, формат HF_AGENT_TOKEN и timeout")
    spool: SpoolRepository | None = None
    try:
        spool = SpoolRepository(args.spool, str(args.agent_id))
        sender = TelemetrySender(spool, transport)
        while True:
            result = sender.step()
            if result.state not in {"idle", "waiting"} or args.once:
                print(json.dumps(asdict(result), ensure_ascii=False), flush=True)
            if result.state == "paused":
                return 2
            if args.once:
                return 0 if result.state in {"idle", "delivered"} else 1
            if result.state in {"idle", "waiting", "retry"}:
                time.sleep(max(0.1, result.retry_in if result.state != "idle" else 1.0))
    except KeyboardInterrupt:
        return 130
    except (sqlite3.Error, ValueError, OSError):
        # Исключения БД/файлов могут содержать пути или исходные данные; в CLI
        # возвращаем только категорию. Неподтверждённые события не удаляем.
        print("Ошибка локальной очереди или конфигурации доставки", file=sys.stderr)
        return 2
    finally:
        if spool is not None:
            spool.close()


if __name__ == "__main__":
    raise SystemExit(main())
