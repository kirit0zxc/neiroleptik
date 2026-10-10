# Проверка истории и stream телеметрии

Дата: 10.10.2026, Москва. Статус: локальная реализация и SQL интеграция тестового
приложения, без production rollout, push или PR.

- Ветка: codex/telemetry-stream.
- Worktree: C:/Users/dimvo/.codex/worktrees/telemetry-stream/neiroleptik.
- Проверенная база main: 6d8eb2eea7bb67fddd0819801ebe812771196835.
- Включённый storage SHA: 50b4f3383b1aa27d896f466cb55ce29c75fc6028.
- Независимая часть сохранена коммитом 8296f1a перед обычным merge bff702d.
- Итоговый stream SHA: git rev-parse HEAD для коммита с этим отчётом.

Эта точная пара main/storage проверена совместно. Локальный origin/main оставался
равным указанной базе; последний fetch зафиксирован первым агентом в handoff
10.10.2026 20:30:30 Москва. Перед будущим push нужен новый актуальный main.

## Правила и merge

Корневой AGENTS.md полностью прочитан до работы и после merge; отслеживается,
существует в HEAD, не изменён локально. Blob HEAD и базы:
921e7165b8f1c6a4013196d9a83d65b98aba7a66. Локальных AGENTS.md в затронутых
каталогах нет. Основной checkout и worktree первого агента не переключались
и не изменялись. Чужие файлы не переносились. Конфликты specs разрешены
с сохранением требований обеих веток; fixtures storage сохранены.
Push/PR/Approve/merge PR и сообщения участникам не выполнялись.
Цикл трёх субагентов обязателен перед будущим push; этот этап его не заменяет.

## Реализация

До кода обновлены specs/events.md, frontend-protocol-v1.md и architecture.md.
Реализованы подписанный ограниченный HMAC cursor (версия, W, порядок, keyset,
нормализованные фильтры); история с limit+1; bigint catch-up с фиксированным
until; немутирующая публичная копия с явно внедрённой политикой; WS polling
committed журнала с ограниченной страницей, send timeout и освобождением задач.
AuthService проверяет настоящую cookie/Origin в короткой Session вне event loop
перед данными и при простое. Ключ HMAC внедряется, production настройка не добавлена.

CommittedReadAdapter подключает настоящий PublicationReader включённого storage.
UUID/keyset/фильтры преобразуются без сериализации PK, canonical_bytes и digest.
Нижняя retention граница None: cleanup отсутствует, SQL gaps не считаются 410.
Внедрённая политика retention проверена отдельно только на независимом уровне.
Синтетический TestReader существует только в tests; fallback в памяти нет.

Router подключён только к явно собранным тестовым приложениям. Рабочий
main.create_app, общая Base/Alembic и публичный OpenAPI не изменены.
Production проекция, миграции, запуск publisher и публичные endpoints требуют
backend согласования. В изолированном transport стандартный 1013 используется
для временного отказа/slow send; production применение ещё согласуется.
4401/4403 применяются только для auth/Origin. SQL содержит синтетические Event
оригиналы; это настоящая PostgreSQL интеграция, но не живой Low backend.

## Воспроизводимые проверки

PostgreSQL 17.11: отдельная honeyforge_test на loopback 55439, UTC. Секретный URL
без вывода скопирован из игнорируемого .env storage в игнорируемый .env stream.
Все pytest удерживали общий FileShare.None POSTGRES_TEST.lock, ожидание до 30s.
SQL fixtures storage создают случайные telemetry_test схемы, используют штатный
защищённый Engine и удаляют только тестовые схемы. Auth дополнительно проверена
на отдельной изолированной схеме пользователей. Production DB не затрагивалась.

```powershell
./scripts/test_backend_locked.ps1 -LockPath C:/DTSU/vsprojects/neiroleptik/work/telemetry-two-branches/POSTGRES_TEST.lock
./scripts/test_backend_locked.ps1 -LockPath C:/DTSU/vsprojects/neiroleptik/work/telemetry-two-branches/POSTGRES_TEST.lock -TestArguments @('-q', 'backend/tests')
```

| Проверка | Итог |
| --- | --- |
| uv sync --frozen --project backend | passed; CPython 3.12.13 |
| pytest backend/tests/telemetry | 97 passed, без skipped; storage и stream совместно |
| pytest backend/tests | 187 passed, без skipped, 60.70s |
| ruff check backend/src backend/tests | passed |
| ruff format --check backend/src backend/tests | passed, 60 файлов |
| mypy с backend/pyproject.toml | passed, 43 source files |
| honeyforge.cli export-openapi --check | passed; production telemetry routes отсутствуют |
| uv build --wheel --project backend | passed, honeyforge_control-0.1.0 |
| git diff --check / staged check | passed для итоговой версии |
| npm ci / npm run typecheck | passed |
| Frontend vitest run на Node 24.19 | 44 файла, 401 passed |
| Frontend tsc -b + vite build на Node 24.19 | passed; предупреждение о крупных chunks |
| node scripts/check_stream_wire.mjs | passed на настоящем текущем StreamClient |

Сначала без доступного стенда независимый прогон дал 36 passed/2 skipped, полный
suite остановился на отсутствии HF_TEST_DATABASE_URL. После появления ready и
настройки стенда эти ограничения сняты. В первом новом SQL прогоне исправлены
повторные index в нашей batch fixture; storage проверки не ослаблялись.
Оставшееся backend предупреждение: Starlette/httpx deprecation.

Системные Node 22.11/npm 8 дали engine warnings и не установили optional binding.
Для frontend использован bundled Node 24.19 и точный
@rolldown/binding-win32-x64-msvc@1.2.13, полученный npm pack и распакованный в
игнорируемые node_modules. package.json/lockfile и frontend исходники не менялись.
Воспроизводить проверки на поддерживаемом Node; исправление среды не является
изменением зависимости приложения. Генерируемое npm ci изменение MSW отменено.

## Покрытие сценариев 06_STREAM_TESTS.md

| Сценарии | Доказательство / границы |
| --- | --- |
| R01–R09 | Unit страницы/подпись/фильтры/лимиты; SQL tie agent/event, snapshot и late received, точные границы occurred_at |
| R10 | Настоящий store commit pending outbox не появляется до publisher commit |
| R11–R18 | Unit и SQL bigint >2^53, fixed until, gaps, exact last page; R16 только unit retention политика |
| R19–R21 | SQL Barrier/Event между чтением W и polling, отдельный commit; publisher pause до commit/rollback невидим reader |
| R22–R24 | Текущий StreamClient: live буфер, страницы, bigint, pair identity, дубли, reconnect; SQL output побайтно в JSON-структуре совпадает с тем же wire fixture |
| R25/R34/R35 | Существующий frontend suite зелёный; сервер хранит HTML/ANSI/OSC оригинал; браузер без MSW не проверен |
| R26 | Bounded page_size/send timeout; медленная отправка не блокирует настоящий SQL publisher, catch-up остаётся читаемым |
| R27 | Отмена задач, короткие Session/connection, disconnect TestClient; loopback/reverse proxy ещё не проверены |
| R28 | Два клиента/экземпляра SQL reader/live service используют один committed журнал, без локального списка |
| R29–R31 | Viewer cookie REST/WS; отсутствие cookie, Origin; PostgreSQL отзыв/истечение при простое и после live закрывают 4401, дальнейшие данные не отправляются |
| R32–R33 | Тестовая маска во всех трёх SQL каналах, оригинальные bytes неизменны, internal поля не выходят, input/output/method/path/truncated сохраняются |
| R36 | Флаг принадлежит Диане; изменения не дублировались и новый флаг не создавался |
| R37 | Найдено расхождение YumiEvents.attackPayload: читает data.command вместо внешнего data.input; исправление frontend ожидает согласования ответственности |

## Передача владельцу и оставшиеся условия

Точные wire fixtures: contracts/fixtures/telemetry-stream-wire.json. REST items
содержат stream_seq/event без type; WS type сохраняется. SQL integration test
сверяет фактический output с fixture, используемым текущим StreamClient harness.
Юми требует адаптера data.input → внутренний payload.command;
управляющие start/stop/reset не переименовывать. HTTP optional поля не выдумываются.

Ожидаются согласованные production проекция и миграции, включение публичных
маршрутов, frontend флаг Дианы. Нет браузерного прогона history/live/disconnect
без MSW, loopback WS клиента (websockets не установлен) и reverse proxy проверки.
Нет ingest HTTP, Low адаптера или replay. Этот этап не доказывает живой Low → UI.
Перед push повторить полный цикл трёх субагентов и совместимость с новым main;
для merge PR нужны CI и явное одобрение человека актуальной версии.
