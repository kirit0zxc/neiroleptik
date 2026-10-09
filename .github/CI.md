# CI и pull request

CI запускается для каждого PR в `main`, push в `main` и вручную из Actions.
Backend обязателен: отсутствие файлов или пропуск job не дают зелёный `CI result`.

## Проверки репозитория

Проверяются workflow, пробельные ошибки и наличие компонентов/lock-файлов.
actionlint 1.7.12 скачивается из [официального выпуска](https://github.com/rhysd/actionlint/releases/tag/v1.7.12)
как Linux amd64 binary; SHA-256 архива закреплён в workflow и проверяется до
извлечения и запуска. Так проверка workflow не зависит от Docker Hub pull limit.
Ошибка скачивания или checksum блокирует job; проверка не пропускается.

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

## Frontend

В текущем проекте frontend присутствует: CI выполняет `npm ci`, `typecheck`,
`test:ci` и `build` на Node.js 24. При отсутствии frontend job явно пропускается;
при добавлении нужны package.json, package-lock.json и эти npm-скрипты. Исходники без
package.json и manifest без lock-файла считаются ошибкой.
Зелёный backend CI не доказывает интеграцию с ещё не подключённым frontend.
Stream/events/replay в этом PR имеют только проект контрактов, без реализации.
Playwright E2E пока запускается локально; обязательной браузерной job в CI нет.

## Ревью

Обязательный итоговый check: `CI result`. Он требует успешных repository/backend
и frontend, когда последний присутствует. Ошибка, отмена или неожиданный пропуск
проверок блокируют итоговый check. Проверка ветки самим GitHub зависит от настроенных
rulesets/branch protection; этот файл не устанавливает защиту ветки.

Процесс задаёт `AGENTS.md`: три субагента перед push, проверка с актуальным main,
финальное одобрение человеком. Агент не одобряет и не сливает PR. Для PR владельца
нужен другой человек, назначенный владельцем для независимого одобрения.

## Agent telemetry checks

Обязательная job телеметрии устанавливает корневой uv.lock на Python 3.12,
запускает Ruff, mypy, pytest для tests/telemetry, сверку входных JSON Schema
и синтетическое offline-демо. PostgreSQL этим набором не проверяется.
Итоговый CI result также требует успешной telemetry job.
