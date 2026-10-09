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

## Документация и интеграция

- [Архитектура](specs/architecture.md), [Control API](specs/control-plane.md).
- [OpenAPI](contracts/openapi.json), [событие v1](contracts/event-v1.schema.json).
- [Frontend: сессии, stream и replay](specs/frontend-protocol-v1.md).
- [Передача интерфейсов участникам](specs/integration-handoff.md).
- [Серверный стенд](specs/server-checklist.md), [CI](.github/CI.md).

OpenAPI описывает реализованный control API. События, stream и replay — проект
контракта для согласования с участником телеметрии. Для этих экранов frontend
временно использует MSW; auth/profiles/hosts/deployments подключаются к настоящему
API. Финальная демонстрация событий требует реального telemetry backend.

В разработке frontend использует origin `http://localhost:5173`, cookie
`hf_session`, `credentials: "include"` и CSRF из login или `/api/v1/auth/me`.
Другой origin согласуйте и добавьте в `HF_ALLOWED_ORIGINS`.
