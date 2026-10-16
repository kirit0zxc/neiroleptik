# HoneyForge

Центр управления сетевыми ловушками: создайте профиль, назначьте его узлу, запустите ловушку и наблюдайте обращения в браузере. React + TypeScript, FastAPI + PostgreSQL, Python-агент и Docker; Medium использует Cowrie 3.1.1.

## Что работает

| Возможность | Реализовано |
| --- | --- |
| Low | TCP-баннер и HTTP-страница, start/stop, события |
| Medium | SSH с заданными hostname, banner и учётными данными; start/stop/reset |
| Управление | Роли, версии профилей, регистрация узлов, heartbeat, команды, ACK и аудит |
| Телеметрия | Постоянный сбор, SQLite-очередь агента, повторы доставки, PostgreSQL, live-лента |
| Расследование | События сессии, текстовый replay после выхода из SSH, алерты, honeytokens, метаданные файлов, экспорт IoC |
| Удаление назначения | После подтверждённого stop; история сохраняется, порт освобождается |

High/KVM, публичная изоляция runtime, внешнее хранилище raw и серверная сводка `/analytics/summary` ещё не завершены. Medium эмулирует SSH-окружение, а не даёт атакующему настоящую ОС. Replay показывает записанные команды и ответы; геометрия терминала не воспроизводится. Linux/server deployment требует отдельной проверки прав и сетевой изоляции; основная приёмка — Windows + Docker Desktop.

## Быстрый запуск Windows

Установите Git, Python 3.12, Docker Desktop (Linux containers), uv 0.11.28. Для собранного интерфейса Node.js на хосте не нужен.

```powershell
git clone https://github.com/kirit0zxc/neiroleptik.git
cd neiroleptik
powershell -ExecutionPolicy Bypass -File scripts/start-demo.ps1 -CreateAdmin admin
```

Введите новый пароль администратора (12–256 символов). Откройте **http://localhost:5173**. Скрипт создаёт `.env` только при первом запуске, собирает API/UI, применяет миграции и ждёт готовности сервисов. Повторный запуск: `powershell -ExecutionPolicy Bypass -File scripts/start-demo.ps1`. Не используйте `-CreateAdmin` повторно для существующего пользователя.

Это центр. Чтобы кнопка «Запустить» создавала настоящий контейнер, отдельно зарегистрируйте и запустите исполнитель: **[полная инструкция: центр → агент → SSH → события → replay](docs/deployment.md)**. Там же описаны Linux, TLS/удалённый агент, обновление и сохранение данных.

Порты: UI `127.0.0.1:5173`, API `127.0.0.1:8000`, PostgreSQL `127.0.0.1:55432`; тестовый SSH выбирается в профиле, например `2224`. [API-документация](http://localhost:8000/docs), [готовность](http://localhost:8000/readyz). Frontend в Docker собран **без MSW**; live-события и replay включены.

## Сценарий демонстрации

1. Зарегистрируйте узел с поддержкой Medium и запустите executor с `--telemetry`.
2. В «Профилях» создайте Medium: SSH 2224, hostname `accounting-server`, banner `SSH-2.0-OpenSSH_9.6`, тестовый пользователь `root` и собственный пароль.
3. Назначьте профиль узлу. В «Парке» нажмите «Запустить», дождитесь состояния «работает».
4. С того же компьютера: `ssh -p 2224 root@127.0.0.1`, выполните `whoami`, `pwd`, `ls`, затем `exit`.
5. В «Событиях» найдите обращения SSH и откройте сессию: доступны команды и replay.
6. «Сбросить» создаёт новый экземпляр. «Остановить» закрывает порт; история остаётся.

Для демо используйте отдельные тестовые пароли. Входной SSH-пароль не показывается в replay, но секрет, введённый как shell-команда, становится частью записи. Локальный Medium слушает loopback; доступ из интернета этим стендом не настраивается.

## Разработка и проверки

```sh
# Агент/runtime: из корня
uv sync --frozen
uv run --frozen pytest
uv run --frozen ruff check agent honeypots infra tests
uv run --frozen mypy

# Backend: отдельное окружение
cd backend
uv sync --frozen
uv run --frozen pytest
uv run --frozen python -m honeyforge.cli export-openapi --check

# Frontend: Node.js 24
cd ../frontend
npm ci
npm run typecheck
npm run test:ci
npx playwright install chromium
PW_CHANNEL=chromium npm run e2e
```

Backend-тестам нужна отдельная БД `honeyforge_test` и `HF_TEST_DATABASE_URL` из корневого `.env`. Создать один раз: `docker compose --env-file .env -f infra/compose.center.yml exec db createdb -U honeyforge honeyforge_test`.
E2E Playwright используют MSW; это отдельная проверка UI, не доказательство настоящего SSH. Живая приёмка описана в [Medium](specs/medium-cowrie-executor.md).

## Документация

- [Установка и развёртывание](docs/deployment.md), [демо-сценарий](specs/demo-runbook.md).
- [Архитектура](specs/architecture.md), [Control API](specs/control-plane.md), [OpenAPI](contracts/openapi.json).
- [Medium/Cowrie](specs/medium-cowrie-executor.md), [исполнитель](infra/runtime/executor.md).
- [События](specs/events.md), [контракт UI](specs/frontend-protocol-v1.md).
- [Серверные требования](specs/server-checklist.md), [CI](.github/CI.md), [правила проекта](AGENTS.md).

Секреты `.env`, agent token, SQLite агента и raw/TTY журналы не коммитятся. Сохраните `HF_CREDENTIAL_KEY`: без него нельзя расшифровать credentials профилей.
