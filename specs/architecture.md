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
