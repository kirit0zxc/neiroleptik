# Архитектура HoneyForge

## Согласованное решение 09.10.2026

Владелец проекта подтвердил использование существующего neiroleptik, FastAPI
вместо Django и API /api/v1/auth/*, /profiles, /hosts, /deployments.
Работа идёт от main; незавершённая ветка Юми не является зависимостью HoneyForge.

## Первый этап: control plane

- backend/src/honeyforge: модульное приложение FastAPI.
- config.py и db.py: параметры окружения и SQLAlchemy sessions.
- models/: пользователи, сессии, профили/версии, узлы/регистрация, deployments, аудит.
- auth/: сессии, роли, CSRF, Argon2, ограничение попыток входа.
- control/: профили, регистрация и назначение профиля узлу.
- api/: HTTP-контракт и проверка прав; бизнес-операции в сервисных классах.
- migrations/: Alembic; production-приложение не создаёт таблицы автоматически.
- contracts/openapi.json: сгенерированный и проверяемый общий API.
- infra/compose.center.yml: локальный центр с PostgreSQL и постоянным volume.

Пользователь -> FastAPI -> PostgreSQL. Агент -> отдельная Bearer-авторизация ->
конфигурация только своего узла. TLS и сетевое разделение устанавливаются
при развёртывании сервера. Локальный Compose публикует сервисы только на loopback.

## Границы первого этапа

Deployment создаётся в состоянии planned. Это запись намерения, а не запуск
процесса и не доказательство изоляции. Job/command API, applied_revision,
реальные Low/Medium/High, события, replay, WS и артефакты реализуются следующими
этапами/участниками. Из UI нельзя выдать planned за running.

## Ответственность команды

Участник 1: frontend. Участник 2: auth/control/db/CI. Участник 3: runtime и
применение конфигурации. Участник 4: telemetry/realtime/analysis.
Общие миграции и изменения OpenAPI согласуются через владельца backend.
Форматы событий не фиксируются молча в рамках этого PR.

## Первый шаг телеметрии D1 (участник 4)

Источник планов: HoneyForge-plan.html и HoneyForge-4-Telemetry.html, 09.10.2026.
agent/telemetry/schemas.py: входной Event/Batch/ACK на Pydantic, точная
канонизация оригинала и коды временных/окончательных отказов; адаптер Low
в adapters/low.py. spool.py отвечает за атомарные event+checkpoint и полный
ACK: удаляет accepted, сохраняет retryable, переносит permanent в долговечный
SQLite WAL карантин. QuarantineEntry — отдельный снимок доказательства;
чтение ограничено, автоматической очистки и повторной постановки нет. Python 3.12.
`next_batch()` одной SQLite-транзакцией читает pending и сохраняет неизменяемый
канонический BLOB пачки в batch_snapshots по batch_id. `acknowledge()` проверяет
состав, порядок и payload переданной модели по этому снимку, включая после
рестарта и удаления pending. Привязка не создаётся и не меняется при ACK;
снимки пока сохраняются для повторных/поздних ответов без автоочистки.
Корневой pyproject.toml/uv.lock изолируют зависимости агента от backend.
Входные JSON Schema и синтетические fixtures лежат в contracts/.
Существующий Event для UI и frontend-protocol-v1.md остаются основой интеграции.

Реализован локальный путь Low JSONL → SQLite → batch → ACK/карантин. Формат исходного
Low JSONL предложен для согласования, реальный журнал ещё не проверен.
Следующий шаг: ingest в backend/src/honeyforge/telemetry с общей DB/auth,
ACK после PostgreSQL commit, outbox/publisher, минимальный WS; затем реальный
Low → БД → UI. HTTP sender, backoff и квоты пока не реализованы.

Следующие серверные модули пока только спроектированы, endpoints и миграций нет:

- ingest: отдельная валидация каждого недоверенного входного элемента;
  идентичность устанавливает общий Bearer current_agent, проверяются agent,
  deployment/revision и согласованный runtime lifecycle instance.
- storage: неизменяемый канонический входной Event отдельно от маскированного
  UI DTO; UNIQUE (agent_id,event_id) и один outbox на внутренний event PK.
  Одинаковый повтор accepted, иной payload EVENT_ID_CONFLICT; event+outbox
  записываются атомарно, ACK выпускается после commit.
- publisher: один worker MVP; сохранённая publication с уникальной ссылкой
  на event и отметка processed outbox в одной транзакции. WS выпускает только
  committed журнал; WS, REST и watermark читают один журнал публикаций.
  stream_seq монотонен в видимом committed журнале, это не sequence таблицы events.

Серверные тесты потери ACK, конфликтов среди хороших элементов, конкурентных
повторов и падений worker до/после commit обязательны на реальном PostgreSQL;
локальные тесты SQLite их не заменяют. Точные границы — specs/events.md.
Не согласованы instance_id при restart/reset, поколение источника, область
source_seq, связь connection/session и Cowrie/TTY. В VM_trap Low имеет другой
формат; root pyproject runtime требует отдельного согласованного объединения.
seal/reset требует остановки источников и manifest файлов, которого в spool нет.

D2: Cowrie/доставка/отказы; D3: timeline/replay/артефакты/token/alert/CSV;
D4: нагрузка/изоляция/целостность; D5: freeze и воспроизводимое демо.
Подробный контракт и критерии первого шага — specs/events.md.
