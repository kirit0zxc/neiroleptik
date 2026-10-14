"""Один ограниченный проход Low → durable raw → production Event/spool без отправки."""

import argparse
import json
import sqlite3
import sys
from contextlib import ExitStack, closing
from dataclasses import asdict
from pathlib import Path
from uuid import UUID

from agent.telemetry.low_journal.collector import CollectionLimits, LowJournalCollector
from agent.telemetry.low_journal.conversion import ConversionLimits, LowRawConverter
from agent.telemetry.low_journal.durable import RawJournalError, RawJournalRepository
from agent.telemetry.low_journal.models import JournalBinding
from agent.telemetry.low_journal.production import LowProductionMapper
from agent.telemetry.spool import SpoolRepository


def main(argv: list[str] | None = None) -> int:
    """Binding задаёт оператор; header не может зарегистрировать чужой instance."""
    parser = argparse.ArgumentParser(description="Один проход сбора Low в долговечную очередь")
    parser.add_argument("--spool", required=True, type=Path)
    parser.add_argument("--agent-id", required=True, type=UUID)
    parser.add_argument("--deployment-id", required=True, type=UUID)
    parser.add_argument("--instance-id", required=True, type=UUID)
    parser.add_argument("--profile-revision", required=True, type=int)
    parser.add_argument("--max-raw-bytes", type=int, default=256 * 1024 * 1024)
    parser.add_argument("--max-records", type=int, default=1000)
    parser.add_argument("--max-bytes", type=int, default=4 * 1024 * 1024)
    parser.add_argument("--max-scan-bytes", type=int, default=64 * 1024 * 1024)
    parser.add_argument("--max-line-bytes", type=int, default=65536)
    parser.add_argument("--max-files", type=int, default=64)
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args(argv)
    try:
        binding = JournalBinding(
            str(args.deployment_id), str(args.instance_id), args.profile_revision
        )
        limits = CollectionLimits(
            max_files=args.max_files,
            max_records=args.max_records,
            max_bytes=args.max_bytes,
            max_scan_bytes=args.max_scan_bytes,
            max_line_bytes=args.max_line_bytes,
        )
        conversion_limits = ConversionLimits(args.max_records, args.max_bytes)
        with ExitStack() as stack:
            raw = stack.enter_context(
                closing(
                    RawJournalRepository(
                        args.spool, str(args.agent_id), max_raw_bytes=args.max_raw_bytes
                    )
                )
            )
            spool = stack.enter_context(closing(SpoolRepository(args.spool, str(args.agent_id))))
            collection = LowJournalCollector(raw, binding).step(args.paths, limits)
            mapper = LowProductionMapper(args.agent_id)
            # Даже при ожидании новой цепочки ранее проверенное сырьё не блокируется.
            conversion = LowRawConverter(spool, mapper, mapper.version).step(
                binding, conversion_limits
            )
            print(
                json.dumps(
                    {"collection": asdict(collection), "conversion": asdict(conversion)},
                    ensure_ascii=False,
                )
            )
            if collection.state == "error" or conversion.state == "error":
                return 2
            if collection.state in {"waiting", "limited"} or conversion.state in {
                "retry",
                "limited",
            }:
                return 1
            return 0
    except (OSError, ValueError, sqlite3.Error, RawJournalError):
        print("Ошибка конфигурации или долговечного хранилища Low", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
