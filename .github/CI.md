# CI и pull request

CI запускается для каждого PR в `main`, push в `main` и вручную из Actions.
Backend обязателен: отсутствие файлов или пропуск job не дают зелёный `CI result`.

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
pytest для `tests/low`, `tests/runtime`, `tests/preflight` и `tests/infra`, сборка
wheel (`uv build --wheel`) и `docker compose -f compose.runtime.yml config --quiet`.
Последняя команда проверяет только синтаксис Compose-конфигурации: контейнеры не
запускаются. Root-конфигурация Ruff исключает `backend`, `frontend` и `scripts`,
чтобы они проверялись только своими конфигурациями.

Зелёный `Runtime checks` не доказывает работу KVM, Cowrie, Docker, сетевую изоляцию и
интеграцию с центром: такие проверки требуют отдельного живого стенда.

## Frontend

Пока `frontend/` отсутствует, job явно пропускается. При добавлении приложения
нужны `package.json`, `package-lock.json` и npm-скрипты `typecheck`, `test:ci`, `build`.
Тогда CI выполняет `npm ci` и эти три команды на Node.js 24. Исходники без
package.json и manifest без lock-файла считаются ошибкой.
Зелёный backend CI не доказывает интеграцию с ещё не подключённым frontend.
Stream/events/replay в этом PR имеют только проект контрактов, без реализации.

## Ревью

Обязательный итоговый check: `CI result`. Он требует успешных repository, backend,
telemetry и runtime, а также frontend, когда последний присутствует. Ошибка, отмена или неожиданный пропуск
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
