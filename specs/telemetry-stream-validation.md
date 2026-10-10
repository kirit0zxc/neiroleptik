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

## Исправление подтверждённых дефектов stream, 10.10.2026

Этот раздел содержит актуальные результаты повторной проверки; предыдущие
разделы описывают исходный этап реализации.

- Исходный проверенный HEAD: 0bf6a766a6224e459d5d3bcb1748c3e8fb9ef6a6.
- Актуальный origin/main после повторного fetch: f1cf13632fc61c4e0da7340e5e7650942e479d12.
- Main включён обычным merge: 17562253e52809d0de57f7ac529dda15c290cf42.
- Storage: 50b4f3383b1aa27d896f466cb55ce29c75fc6028, остаётся предком HEAD.
- Ветка/worktree: codex/telemetry-stream,
  C:/Users/dimvo/.codex/worktrees/telemetry-stream/neiroleptik.
- Итоговая версия исправлений — локальный коммит, содержащий этот раздел;
  точный SHA получается через git rev-parse HEAD после его создания.

До изменения кода обновлены events.md и architecture.md. Внутренний DBOperations
устанавливает PostgreSQL SET LOCAL statement_timeout не более 1000 мс на каждый
SQL чтения/проверки сессии, с общим бюджетом вызова 3 секунды. Executor Future
остаётся учтённым до завершения worker: cancel запрещает следующий SQL и ожидает
rollback/close. Future защищён от отмены; transport ожидает обе дочерние задачи
даже при повторной отмене endpoint и ASGI cancel scope. Event loop не блокируется.
Hello/live, страницы, seq, cookie/Origin, отзыв и close codes сохранены.
На простое транзакция не удерживается. Локальная настройка не остаётся после
возврата соединения и не применяется к операциям вне контекста WS.

HistoryService.filters обрабатывает OverflowError при UTC-нормализации как
DomainError(422), с общим безопасным detail. Проверены обе границы для from/to
в сервисе и HTTP, нормализация допустимых крайних дат; существующие SQL-тесты
подтверждают from включительно / to исключительно.

Новые штатные регрессии: backend/tests/telemetry/test_stream_regressions.py.
Использовано воспроизведение из work/telemetry-review как основание; штатные
тесты не импортируют его и не зависят от его абсолютного пути. Отдельный Engine
с пулом размера 1 и случайная telemetry_test схема доказывают ожидание Lock
через pg_stat_activity. Отмена чтения, проверки сессии, повторная отмена,
обычный TestClient disconnect, отмена всех asyncio-задач transport при shutdown
и серверный deadline проверяются до снятия блокировки. Утверждения проверяют
завершение worker, checkedout=0, отсутствие активного SQL и незавершённых задач;
срок освобождения активного SQL/соединения после отмены — менее 2 секунд.
Rollback блокирующей транзакции в finally используется только для очистки.

Все PostgreSQL-прогоны выполнены последовательно через
scripts/run_backend_tests_locked.ps1 с FileShare.None на
C:/DTSU/vsprojects/neiroleptik/work/telemetry-two-branches/POSTGRES_TEST.lock.
Использована существующая honeyforge_test; миграции и cleanup штатных фикстур
входят в защищённый интервал. Пропущенных тестов нет.

| Команда / проверка итоговой реализации | Результат |
| --- | --- |
| uv sync --frozen --project backend | passed, 49 packages |
| wrapper: pytest tests/telemetry/test_stream_regressions.py -q | 19 passed, 15.89s |
| wrapper: pytest tests/telemetry -q | 116 passed, 48.62s |
| wrapper: полный pytest -q из backend | 206 passed, 73.37s |
| uv run --frozen --project backend ruff check backend/src backend/tests | passed |
| uv run --frozen --project backend ruff format --check backend/src backend/tests | passed, 62 файла |
| uv run --frozen --project backend mypy --config-file backend/pyproject.toml backend/src | passed, 44 source files |
| uv run --frozen --project backend python -m honeyforge.cli export-openapi --check | passed |
| uv build --wheel --project backend | passed, honeyforge_control-0.1.0-py3-none-any.whl |
| Node 24: scripts/check_stream_wire.mjs | passed: live buffer, bigint, pagination, pair identity, dedup, reconnect |
| git diff --check | passed |

19 регрессий включают 10 настоящих PostgreSQL проверок ресурсов/deadline,
4 HTTP-проверки дат с реальной cookie-сессией PostgreSQL и 5 проверок самой
валидации дат без SQL внутри HistoryService.filters. Полная suite включает
штатные изолированные и PostgreSQL-тесты; эти уровни не подменяют друг друга.
Первый полный прогон до усиления shutdown дал 201 passed; после окончательных
изменений выполнен полный прогон 206 passed. Остаётся одно существующее
предупреждение Starlette/httpx deprecation.

Совместимость проверена с указанным актуальным main, который входит в историю
ветки: основной OpenAPI и production FastAPI не изменены; wire harness использует
фактический текущий StreamClient. Общие модели, миграции, ingest, sender/runtime
и frontend-функциональность в исправлении не менялись. Основной checkout и storage
не переключались. AGENTS.md отслеживается, не изменён; blob совпадает с main:
921e7165b8f1c6a4013196d9a83d65b98aba7a66. Локальных AGENTS.md нет.

Срок менее 2 секунд относится к активному SQL при доступной PostgreSQL. Получение
соединения ограничивается pool_timeout/connect_timeout внедрённого Engine;
при потере сети действуют таймауты драйвера. Произвольный неограниченный
синхронный код Python этим механизмом не прерывается; внутренние адаптеры обязаны
соблюдать описанный контракт. Loopback/reverse proxy и браузер без MSW в этом
исправлении не проверялись. Production-интеграция, публичная проекция и миграции
остаются на согласовании. Исправление не доказывает Low → UI.

Push/PR/Approve/merge PR не выполнялись. Перед будущим push обязательны полный
цикл трёх субагентов по AGENTS.md и проверка с актуальным main; этот локальный
этап не заменяет CI и человеческое одобрение PR.
