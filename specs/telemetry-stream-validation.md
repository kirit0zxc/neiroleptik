# Проверка stream: независимый этап

Дата: 10.10.2026, Москва. Статус: partial, без production rollout.
Ветка: codex/telemetry-stream.
Worktree: C:/Users/dimvo/.codex/worktrees/telemetry-stream/neiroleptik.
Проверенная база main: 6d8eb2eea7bb67fddd0819801ebe812771196835 (fetch из handoff).
Storage SHA: не включён; STORAGE_READY.md отсутствовал при проверке.
Точная пара main/storage для PostgreSQL отсутствует; проверку SQL не заявляем.
Stream SHA: локальный коммит, содержащий этот отчёт (git rev-parse HEAD).

## Правила

Корневой AGENTS.md полностью прочитан. Он отслеживается, существует в HEAD,
не изменён локально. Blob HEAD и базы: 921e7165b8f1c6a4013196d9a83d65b98aba7a66.
Локальных AGENTS.md в backend/frontend/specs нет. Основной checkout и storage
worktree не переключались и не изменялись. Исключений из правил не применялось.
Push/PR/Approve/merge PR и сообщения участникам не выполнялись.
Цикл трёх субагентов нужен перед будущим push, этот этап его не заменяет.

## Реализация и границы

Сначала обновлены events/frontend-protocol-v1/architecture. Реализованы:
подписанный ограниченный HMAC cursor (версия, snapshot W, порядок, keyset,
нормализованные фильтры); история с limit+1; bigint catch-up с фиксированным
until и доказуемой retention границей; немутирующая публичная копия с явно
внедрённой политикой; WS polling committed журнала с ограниченной страницей,
таймаутом отправки и освобождением задач; короткие настоящие AuthService Session
в worker thread перед данными и при простое. Нет production ключа по умолчанию.

PublicationReader — внутренняя read-only граница. Реальный repository подключится
после готового storage SHA. Синтетический TestReader существует только в tests.
Нет fallback на память в рабочем приложении. Router подключается исключительно
явной сборкой тестового приложения; main.create_app и общий OpenAPI не изменены.
Production политика проекции, миграции, запуск publisher и подключение публичных
endpoints по-прежнему требуют backend согласования. В изолированном transport
временный отказ/slow send использует стандартный 1013; production применение
этой политики ещё требует согласования. 4401/4403 сохранены для auth/Origin.

## Исполняемые проверки

Команды выполнялись из этого worktree (backend команды также из backend).
Backend pytest удерживал FileShare.None на общем POSTGRES_TEST.lock, ожидание
ограничено 30 сек. Повторяемый запуск:

```powershell
./scripts/test_backend_locked.ps1 -LockPath C:/DTSU/vsprojects/neiroleptik/work/telemetry-two-branches/POSTGRES_TEST.lock
```

- uv sync --frozen --project backend: успешно, CPython 3.12.13.
- pytest -q backend/tests/telemetry: 36 passed, 2 skipped; предупреждение Starlette
  об httpx2. Skip: отсутствует HF_TEST_DATABASE_URL, два PostgreSQL auth сценария.
- pytest -q backend/tests --maxfail=1: 36 passed, 2 skipped, 1 setup error:
  не задан безопасный PostgreSQL URL honeyforge_test. Полный suite не завершён.
- ruff check backend/src backend/tests: успешно.
- ruff format --check backend/src backend/tests: 49 файлов соответствуют.
- mypy backend/src/honeyforge (backend/pyproject.toml): 37 файлов, успешно.
- python -m honeyforge.cli export-openapi --check --path contracts/openapi.json:
  успешно. Дополнительно проверено отсутствие telemetry routes в рабочем OpenAPI.
- uv build --wheel --project backend: успешно, honeyforge_control-0.1.0.
- git diff --check: успешно после удаления лишних пустых строк.
- node scripts/check_stream_wire.mjs, Node 24.19: успешно; настоящий StreamClient,
  реальные сериализованные ответы синтетических backend сервисов: live во время
  страниц, bigint >2^53, next_cursor, agent/event identity, дубли, reconnect.
  Фикстура contracts/fixtures/telemetry-stream-wire.json проверяется pytest против
  текущего вывода сервисов. Это wire contract тест, не сетевой SQL backend.
- npm ci: успешно; исходная среда Node 22.11 выдала engine warnings.
- npm run typecheck: успешно.
- Первый npm run test:ci и build: ошибка запуска rolldown native binding.
  Повтор с Node 24 и локальной установкой optional binding описан ниже по итогам.

## Сценарии 06_STREAM_TESTS.md

| Сценарии | Доказательство / ограничение |
| --- | --- |
| R01–R09 | Независимые pytest: страницы, одинаковые event_id, поздняя синтетическая публикация вне W, фильтры/границы, подпись/версия/размер cursor, лимиты |
| R10 | Pending publication в настоящей SQL БД: ожидает storage |
| R11–R18 | Независимые pytest: bigint, gaps, fixed until, exact last page, ошибки; R16 только внедрённая retention политика, cleanup не реализован |
| R19–R20 | Hello и добавление после W проверены на синтетическом read source; PostgreSQL Barrier/commit гонка ещё не проверена |
| R21 | SQL rollback/pending видимость ожидает storage |
| R22–R24 | Wire harness использует текущий StreamClient, проверяет live буфер/догрузку/reconnect/дедупликацию |
| R25/R34/R35 | Существующие frontend tests требуют успешно запущенного полного frontend suite; браузерный прогон не выполнен |
| R26 | Ограниченный page_size и send timeout unit; publisher/backpressure на PostgreSQL ещё не проверены |
| R27 | Отмена задач unit; auth Session структурно закрывается через with; SQL ресурсы/reconnect loopback ещё не проверены |
| R28 | Два клиента независимого уровня; несколько SQL экземпляров ожидают storage |
| R29–R31 | Реальные auth отказы без cookie/с чужим Origin проходят; PostgreSQL viewer/revoke/expire тесты написаны, пропущены; отзыв callback unit не объявляется SQL тестом |
| R32–R33 | Синтетическая маска во всех трёх каналах, оригинал неизменён, whitelist, input/output/method/path/truncated сохраняются |
| R36 | Флаг принадлежит Диане; изменения не дублировались |
| R37 | Подтверждено расхождение: YumiEvents.attackPayload читает data.command, внешний Event требует data.input. Frontend не менялся |

## Передача владельцу

Точный fixture: contracts/fixtures/telemetry-stream-wire.json. REST items содержат
stream_seq/event без type; WS type сохраняется. Юми: требуется согласованное
изменение frontend адаптера data.input → внутренний payload.command;
управляющие команды start/stop/reset не переименовывать. HTML/ANSI/OSC сохраняются
как недоверенный текст, optional HTTP поля сервер не выдумывает.

Ожидается готовый storage SHA и безопасный PostgreSQL стенд; затем обычный merge,
проверка AGENTS.md и совместный transaction/publisher → history/catch-up/WS прогон.
Согласование production проекции и миграции отдельно. Нет проверки браузера без
MSW, сетевого reverse proxy или loopback WS reconnect/shutdown.
Текущий этап не является replay или живым Low → UI.
