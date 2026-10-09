# HoneyForge / neiroleptik

Центр управления ловушками на FastAPI, Python 3.12 и PostgreSQL 17.
Frontend планируется на React + TypeScript. Первый этап реализует cookie-сессии,
CSRF и роли, версии профилей, регистрацию узлов, heartbeat, назначения и аудит.
Назначение имеет статус `planned`: выполнение ловушек, real-time события и replay
будут подключены следующими компонентами. High-профиль описывает настройки;
реальная VM этим API не запускается.

## Локальный запуск

Нужны Python 3.12, uv 0.11.28 и Docker Compose. Команды выполняются из корня репозитория:

```sh
python scripts/init_local_env.py
docker compose --env-file .env -f infra/compose.center.yml up -d db
docker compose --env-file .env -f infra/compose.center.yml build api
docker compose --env-file .env -f infra/compose.center.yml run --rm api /app/.venv/bin/alembic upgrade head
docker compose --env-file .env -f infra/compose.center.yml run --rm api /app/.venv/bin/python -m honeyforge.cli create-user --username admin --role admin
docker compose --env-file .env -f infra/compose.center.yml up -d api
```

Пароль первого администратора вводится интерактивно (12–256 символов).
`.env` создаётся один раз со случайными секретами и не коммитится. Сохраняйте
`HF_CREDENTIAL_KEY`: без него невозможно расшифровать credentials профилей.
API: http://localhost:8000/docs; готовность: http://localhost:8000/readyz.
Dev-порты доступны только локально. PostgreSQL — `127.0.0.1:55432`.
Это dev-стенд; требования к отдельному серверу и TLS — в `specs/server-checklist.md`.

## Разработка и тесты

```sh
docker compose --env-file .env -f infra/compose.center.yml exec db createdb -U honeyforge honeyforge_test
cd backend
uv sync --frozen
uv run --frozen ruff check . ../scripts
uv run --frozen ruff format --check . ../scripts
uv run --frozen mypy
uv run --frozen pytest
uv run --frozen python -m honeyforge.cli export-openapi --check
```

Создание `honeyforge_test` нужно один раз. Тесты читают `HF_TEST_DATABASE_URL`
из окружения или корневого `.env`; БД с иным именем отвергается до очистки таблиц.
Для запуска API вне контейнера из `backend/`:

```sh
uv run --env-file ../.env --frozen alembic upgrade head
uv run --env-file ../.env --frozen uvicorn honeyforge.main:create_app --factory --host 127.0.0.1 --port 8000 --no-proxy-headers
```

Остановите контейнер `api`, если его порт уже занят. После изменения API обновляйте
контракт командой `uv run --frozen python -m honeyforge.cli export-openapi`.

## Runtime ловушек (участник 3)

Реализуется локальный блок D1: ограниченная Low-ловушка, журнал команд, неразрушающий
preflight Linux/KVM, разбор конфигурации центра и драйвер Low (контейнер по профилю).
Клиент центра, Reconciler, Medium и High в runtime пока не реализованы; локальные тесты
не подтверждают работу с настоящим Docker, сквозной сценарий или изоляцию реальной VM.
Код runtime и образ Low не имеют зависимостей времени выполнения (pydantic нужен только
телеметрии).

Runtime использует тот же корневой проект `honeyforge-agent`, что и телеметрия
([агент](agent/README.md)): Python 3.12, uv 0.11.28, инструменты из `uv.lock`.
Команды выполняются из корня репозитория:

```sh
uv sync --frozen
uv run --frozen pytest
uv run --frozen ruff check agent honeypots infra tests
uv run --frozen ruff format --check agent honeypots infra tests
uv run --frozen mypy
uv build --wheel
docker compose -f compose.runtime.yml config --quiet
```

Backend проверяется отдельно командами из раздела выше.

### Low на локальном компьютере

```sh
uv run --frozen python -m honeypots.low
```

По умолчанию сервис доступен только на loopback: TCP 2222 и HTTP 8080.
Журнал JSONL выводится в stdout; это диагностический формат, не согласованный
Event центра и не устойчивый spool. Ctrl-C завершает сервис.

При работающем Docker Engine:

```text
docker compose -f compose.runtime.yml config --quiet
docker compose -f compose.runtime.yml up --build
```

Compose настроен публиковать TCP 13222 и HTTP 18080 только на 127.0.0.1; контейнер
ограничен по ресурсам и подключён к сети с `internal: true`. Запуск этой
конфигурации в Docker не проверялся: неизвестно, публикует ли Docker порты для
контейнера, подключённого только к внутренней сети. Это локальная конфигурация
разработки; изоляцию хоста и будущего runtime-сервера необходимо отдельно проверить.

### Проверка будущего runtime-хоста

```sh
uv run --frozen python -m infra.vm.preflight
```

Команда ничего не создаёт и не изменяет. Linux/KVM/libvirt проверяются статически;
создание VM, права на изменение сети и матрица изоляции требуют отдельного smoke.
На Windows ожидается отчёт о неподдерживаемой платформе и ненулевой код возврата.

Сроки сохраняются: D1–D5, 30 человеко-часов (10 / 12 / 8).

## Документация и интеграция

- [Архитектура](specs/architecture.md), [Control API](specs/control-plane.md).
- [OpenAPI](contracts/openapi.json), [событие v1](contracts/event-v1.schema.json).
- [Frontend: сессии, stream и replay](specs/frontend-protocol-v1.md).
- [Передача интерфейсов участникам](specs/integration-handoff.md).
- [Серверный стенд](specs/server-checklist.md), [CI](.github/CI.md).
- Runtime: [требования D1](specs/runtime-d1.md), [драйверы](specs/runtime-drivers.md),
  [предложения общих контрактов](specs/runtime-contract-proposal.md) (ожидают
  согласования участников 2 и 4), [подготовка runtime и Cowrie spike](infra/runtime/README.md).
- Правила изменений, ревью и человеческого одобрения: [AGENTS.md](AGENTS.md).

OpenAPI описывает реализованный control API. События, stream и replay — проект
контракта для согласования с участником телеметрии. Для этих экранов frontend
временно использует MSW; auth/profiles/hosts/deployments подключаются к настоящему
API. Финальная демонстрация событий требует реального telemetry backend.

В разработке frontend использует origin `http://localhost:5173`, cookie
`hf_session`, `credentials: "include"` и CSRF из login или `/api/v1/auth/me`.
Другой origin согласуйте и добавьте в `HF_ALLOWED_ORIGINS`.

## Телеметрия участника 4: старт D1

Локальная очередь SQLite WAL, адаптер Low, схемы и синтетические fixtures:
[запуск и проверки агента](agent/README.md), [контракт](specs/events.md).
Этот шаг проверяет сохранность событий; живые ingest/WS подключаются следующим этапом.
