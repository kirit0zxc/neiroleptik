# Телеметрия: первый шаг D1

Правила команды находятся в корневом `AGENTS.md`.
Контекст: `HoneyForge-plan.html`, персональная зона `HoneyForge-4-Telemetry.html`.
Подробности и следующие шаги: `specs/events.md`, `specs/architecture.md`.

Из корня репозитория (Python 3.12, uv 0.11.28):

```sh
uv sync --frozen
uv run --frozen python -m agent.telemetry.demo
uv run --frozen pytest
uv run --frozen ruff check agent tests/telemetry
uv run --frozen ruff format --check agent tests/telemetry
uv run --frozen mypy
uv run --frozen python -m agent.telemetry.export_contracts --check
```

Корневой `pyproject.toml`/`uv.lock` относятся к агенту. Backend по-прежнему имеет
собственное окружение в `backend/`; его зависимости и команды не меняются.

Демо создаёт 100 **синтетических** событий, сохраняет их и позицию чтения в SQLite
WAL, закрывает БД и проверяет ID после повторного открытия. Затем временная БД
удаляется. Это проверка локального хранения, а не работающий honeypot или приём
событий сервером. Для подключения к настоящему журналу нужны source_id поколения,
полные UTF-8 JSONL-строки и доверенные UUID из runtime. Формат Low пока предложен
для согласования участнику 3. Автоматического tail/watch/rotation ещё нет.

`contracts/fixtures/telemetry/` содержит агентское событие, batch/ACK,
UI-event, `hello`/live WS и replay chunk. UI-event отличается наличием
`received_at`, назначаемого центром. `stream_seq`/`watermark` — строки, как
в `specs/frontend-protocol-v1.md`. JSON Schema не выражает все ограничения:
байтовый размер, корректный IP и уникальность ID проверяются моделью Python.

`ack.json` — успешный ответ на `batch.json`; `ack-permanent.json` и
`ack-invalid-event-id.json` — окончательный отказ с UUID либо null ID,
разрешаемым по обязательному index. `ack-mixed.json` относится к
`batch-mixed.json`: принятие, окончательный отказ и временный отказ.
`ack-unknown-event-id.json` — отрицательный пример: соответствует JSON Schema,
но не исходному batch и целиком отклоняется spool.

`SpoolRepository.acknowledge(batch, ack)` требует ровно один результат на
каждое исходное событие и неизменный payload сохранённого pending. Неполный,
противоречивый или неподдерживаемый ACK не меняет ни очередь, ни карантин.
`next_batch()` до выдачи сохраняет неизменяемые байты пачки в SQLite по batch_id.
ACK проверяется по этому снимку, а переданная модель должна ему точно
соответствовать: изменение списка events, его порядка или payload отклоняется.
Привязка сохраняется после рестарта и обработки ACK; её нельзя создать впервые
из модели, присланной в acknowledge. Ручные/старые пачки без привязки нужно
заново получить через next_batch(), сохраняя pending. Очистки снимков пока нет.
Accepted удаляется, retryable остаётся, permanent сохраняется с исходным
Event, batch_id, code, index и UTC-временем в карантин одной транзакцией.
Возвращаемое число учитывает только accepted-удаления; checkpoint не меняется.
Повтор/поздний ACK не перезаписывает доказательство и не создаёт его из batch,
если локальный pending уже отсутствует. После рестарта accepted/permanent
не попадают в следующую пачку.

Просмотр: `quarantine_count()` и `list_quarantine(limit=100)`; строгий limit
1..500, порядок первого сохранения, возвращаются независимые снимки.
Карантин хранит оригинал до UI-маскирования. Автоочистки и requeue нет.
При ограничении временного каталога Windows задайте TEMP и TMP на доступную
директорию перед pytest/demo; это не требует пропуска тестов.

Следующий этап: `/agent/v1/events` на общей авторизации `current_agent`,
проверка принадлежности deployment, PostgreSQL commit/dedup → ACK,
публикация после commit и WS по существующему frontend-контракту.
Согласовать instance/session и миграции с владельцами backend/runtime.
HTTP sender, backoff, backpressure и квоты spool относятся к следующему шагу.
Серверный план: UNIQUE (agent_id,event_id), атомарные event+outbox и
publication+processed, WS/REST/watermark по committed журналу. Эти требования
и PostgreSQL-тесты сбоев пока только описаны в specs/events.md. Идентичность
instance_id/restart/reset и реальные Low/Cowrie/TTY остаются на согласовании.
Не объединять root manifest с VM_trap заменой одного другим: нужно сохранить
runtime, honeypots, agent и все тесты. SQLite очередь не реализует seal файлов.
Не включать этот каркас в публичный стенд как готовую доставку телеметрии.
