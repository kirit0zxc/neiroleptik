# CI и pull request

CI запускается для каждого PR независимо от базовой ветки, push в `main` и вручную
из Actions. Для стопки PR проверяются и промежуточные базы (`VM_trap`,
`feat/runtime-drivers`); изменение базы PR (`edited`) и перевод из черновика
(`ready_for_review`) также запускают проверки. После смены базы дождись результата
для нового сочетания коммитов — прежняя зелёная проверка его не подтверждает.
Backend обязателен: отсутствие файлов или пропуск job не дают зелёный `CI result`.

## Проверки репозитория

Проверяются workflow, пробельные ошибки и наличие компонентов/lock-файлов.
actionlint 1.7.12 скачивается из [официального выпуска](https://github.com/rhysd/actionlint/releases/tag/v1.7.12)
как Linux amd64 binary; SHA-256 архива закреплён в workflow и проверяется до
извлечения и запуска. Так проверка workflow не зависит от Docker Hub pull limit.
Ошибка скачивания или checksum блокирует job; проверка не пропускается.
В CI PostgreSQL 17 и базовый Python 3.12 скачиваются из официального зеркала
Docker в ECR Public, чтобы не обращаться к Docker Hub со shared runner.
Для PostgreSQL закреплён digest, совпадающий с проверенным postgres:17-alpine.
Dockerfile позволяет переопределить PYTHON_IMAGE только при сборке; его обычное
значение остаётся python:3.12-slim. Локальный Compose и существующие серверы не меняются.

## Проверки backend

Python 3.12, FastAPI, PostgreSQL 17. `uv sync --frozen` устанавливает зависимости
из `backend/uv.lock`; uv закреплён на 0.11.28. Выполняются:

1. Ruff: ошибки и форматирование backend и служебных скриптов.
2. Строгая проверка типов mypy.
3. Alembic: upgrade, соответствие моделей миграциям, downgrade и повторный upgrade.
4. Сверка `contracts/openapi.json` с OpenAPI, генерируемым приложением.
5. Все pytest-тесты в `backend/tests/`, включая HTTP API, PostgreSQL и JSON Schema.
6. Сборка Docker-образа приложения.

Новые тесты с именами `test_*.py` в `backend/tests/` подхватываются автоматически.
Они должны выполняться без watch-режима, внешних платных сервисов и больших моделей.
Тесты очищают только выделенную БД с точным именем `honeyforge_test`.
CI использует одноразовые тестовые ключи и БД; production-секреты не нужны.
Файл результатов pytest: `backend/test-results.xml` в рабочей директории job;
полный вывод доступен в шаге Actions.

## Проверки runtime

Job `Runtime checks` проверяет код runtime и ловушек: `agent`, `honeypots`, `infra`
и `tests`. Python 3.12, зависимости и версии инструментов из общего `uv.lock`
(`uv sync --frozen`). Выполняются Ruff (ошибки и форматирование), строгий mypy,
pytest по каталогу `tests`, кроме `tests/telemetry` (их запускает задача телеметрии), сборка
wheel (`uv build --wheel`) и `docker compose -f compose.runtime.yml config --quiet`.
Последняя команда проверяет только синтаксис Compose-конфигурации: контейнеры не
запускаются. Root-конфигурация Ruff исключает `backend`, `frontend` и `scripts`,
чтобы они проверялись только своими конфигурациями.

Зелёный `Runtime checks` не доказывает работу KVM, Cowrie, Docker, сетевую изоляцию и
интеграцию с центром: такие проверки требуют отдельного живого стенда.

## Frontend

В текущем проекте frontend присутствует: CI выполняет `npm ci`, `typecheck`,
`test:ci` и `build` на Node.js 24. Также собирается Docker frontend без MSW с
общими contracts и nginx proxy (образы Node/nginx берутся из ECR Public).
При отсутствии frontend job явно пропускается;
при добавлении нужны package.json, package-lock.json и эти npm-скрипты. Исходники без
package.json и manifest без lock-файла считаются ошибкой.
Зелёный backend CI не доказывает интеграцию с ещё не подключённым frontend.
Stream/events/replay в этом PR имеют только проект контрактов, без реализации.
Отдельная обязательная job `Frontend E2E (Chromium, MSW)` устанавливает Chromium
версии из Playwright lock-файла и его системные зависимости, запускает весь
`frontend/e2e` на Linux и Node.js 24. Playwright собирает интерфейс с MSW
и поднимает Vite preview на 127.0.0.1:5174, без HMR и переиспользования чужого сервера.
Используются MSW, два worker, ноль повторов; `test.only` запрещён в CI.
WebGL отключён у тестового браузера: Юми использует штатный портрет и текстовую
консоль. Эта задача не проверяет 3D-рендеринг; для него нужен отдельный GPU-стенд.
HTML-отчёт и диагностика сохраняются на 7 дней, при падении — trace и screenshot.
Сбой установки браузера, сервера или тестов блокирует общий итог.
Это браузерные тесты интерфейса на моках; живые FastAPI/PostgreSQL/runtime ими
не проверяются. Для отдельного живого стенда предусмотрен `E2E_BASE_URL`.

## Ревью

Обязательный итоговый check: `CI result`. Он требует успешных repository, backend,
telemetry и runtime, а также frontend и E2E, когда frontend присутствует. Ошибка, отмена или неожиданный пропуск
проверок блокируют итоговый check. Проверка ветки самим GitHub зависит от настроенных
rulesets/branch protection; этот файл не устанавливает защиту ветки.

Процесс задаёт `AGENTS.md`: три субагента перед push, проверка с актуальным main,
финальное одобрение человеком. Агент не одобряет и не сливает PR. Для PR владельца
нужен другой человек, назначенный владельцем для независимого одобрения.

## Agent telemetry checks

Обязательная job телеметрии устанавливает корневой uv.lock на Python 3.12,
запускает Ruff, mypy, pytest (весь каталог tests), сверку входных JSON Schema
и синтетическое offline-демо. PostgreSQL этим набором не проверяется.
Итоговый CI result также требует успешных telemetry и runtime job.
