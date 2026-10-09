"""Локальная проверка JSONL → SQLite → рестарт → batch, без сервера и сети."""

import tempfile
from pathlib import Path

from agent.telemetry.adapters.low import LowAdapter
from agent.telemetry.export_contracts import AGENT_ID, DEPLOYMENT_ID, INSTANCE_ID, example_line
from agent.telemetry.spool import SpoolRepository


def main() -> None:
    adapter = LowAdapter(AGENT_ID, DEPLOYMENT_ID, INSTANCE_ID, 1)
    with tempfile.TemporaryDirectory(prefix="honeyforge-demo-") as directory:
        path = Path(directory) / "spool.sqlite3"
        spool = SpoolRepository(path, AGENT_ID)
        ids = []
        try:
            for _ in range(100):
                record = adapter.parse_line(
                    example_line(),
                    source_id="demo:g1",
                    offset=spool.checkpoint("demo:g1"),
                )
                spool.append(record)
                ids.append(record.event.event_id)
        finally:
            spool.close()
        restored = SpoolRepository(path, AGENT_ID)
        try:
            batch = restored.next_batch()
            if batch is None or [event.event_id for event in batch.events] != ids:
                raise RuntimeError("После рестарта не совпал набор событий")
            print(f"Синтетических событий после рестарта: {restored.count()}")
            print(f"Размер batch: {len(batch.to_bytes())} байт; все 100 ID совпали")
            print("Сеть и центр не использовались. Без серверного ACK записи остаются в spool.")
        finally:
            restored.close()


if __name__ == "__main__":
    main()
