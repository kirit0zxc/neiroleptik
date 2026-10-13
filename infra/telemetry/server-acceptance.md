# Независимая серверная приёмка телеметрии

## Дополнение 13.10.2026: согласованный mapper

Кирито подтвердил MVP; нормативный контракт и DTO находятся в
[low-telemetry-handoff.md](../../specs/low-telemetry-handoff.md).
Ветка обновлена fast-forward до main `01f8272ba37ef16af7b905042b00a7a1d36808ca`.
Ранее подготовленные проверки сохранены. Новые/итоговые проверки **не запускались**.
Исторические фразы ниже об отсутствии решения/mapper относятся к 12.10.

В отдельном checkout сервера после установки закреплённых зависимостей:

```sh
uv run --frozen pytest tests/telemetry -q
uv run --frozen python -m agent.telemetry.export_contracts --check
uv run --frozen mypy
uv run --frozen ruff check agent/telemetry tests/telemetry
uv run --frozen ruff format --check agent/telemetry tests/telemetry
```

Backend: выполнить штатные PostgreSQL/contract/типовые проверки из CI,
включая `tests/test_investigation.py` и `tests/telemetry/test_request_policy.py`.
Frontend: `npm ci`, `npm run typecheck`, `npm run test:ci`, `npm run build`.
Результаты сверки генерации OpenAPI обязательны: JSON в этом пакете обновлён
точечным редактированием enum, локальный генератор по запрету не запускался.

Для настоящих журналов Low, доверенного binding и отдельного writable spool:

```sh
uv run --frozen python -m agent.telemetry.collect_low \
  --spool "$HF_SPOOL" --agent-id "$HF_AGENT_ID" \
  --deployment-id "$HF_DEPLOYMENT_ID" --instance-id "$HF_INSTANCE_ID" \
  --profile-revision "$HF_PROFILE_REVISION" /readonly/low/*.jsonl
uv run --frozen python -m agent.telemetry.deliver \
  --spool "$HF_SPOOL" --agent-id "$HF_AGENT_ID" --endpoint "$HF_INGEST_ENDPOINT" --once
```

collect_low делает один ограниченный проход, не демон: оператор повторяет его
после новых строк/ротации и передаёт полную доступную цепочку explicit paths.
Код 0 — проход завершён, 1 — ожидание/лимит/повтор, 2 — ошибка.
JSON stdout содержит отдельно collection/conversion и безопасный code.
Ожидание позднего null не удаляет файлы и не блокирует конверсию прежнего raw.
Sender запускается в единственном экземпляре; Bearer только HF_AGENT_TOKEN.
TLS проверяется существующим HttpBatchTransport. Перед запуском нужен реальный
зарегистрированный deployment/instance, а не придуманные UUID.
Проверить REST=WS=lookup, TCP фильтр, отсутствие секретов и HTTP 200, count/IDs
при повторе и после ACK, затем браузер без MSW. Эти команды не заменяют
полную матрицу Linux/ACK/crash/PostgreSQL ниже и не являются отчётом об успехе.

Дата подготовки: 12.10.2026. Статус: команды подготовлены чтением кода,
**не выполнены**. Пользователь разрешает исполнения только на будущем сервере.
Документ не развёртывает сервер и не запускает Actions. Требования reader:
[low-journal-reader.md](../../specs/low-journal-reader.md); mapping остаётся
[предложением](../../specs/low-telemetry-handoff.md).

## Версии и границы

Подготовка выполнена в `codex/low-production-mapper`, HEAD и последнее известное
origin/main: `391d292a92c54180dc76ab08ce2417b0afbad027`. Git fetch 12.10.2026
не смог записать FETCH_HEAD в ограниченной среде. Актуальность remote не доказана.
Файлы подготовки незакоммичены: одного HEAD недостаточно для воспроизведения.
Оператор переносит итоговые изменения отдельным пакетом в выделенный checkout;
не переносит чужой frontend/public/mockServiceWorker.js, work/, .env, БД и venv.
До запуска фиксирует SHA HEAD/main и SHA-256 каждого файла пакета.

После появления сервера сначала обновить сведения main, вручную проверить diff
контрактов/миграций/зависимостей и совместимость итогового пакета с новым main
в отдельной ветке. Ни основной checkout, ни чужие ветки не переключать.
Конфликты разрешить до исполнения. После изменения файлов или main проверки
повторяются. До будущего push обязательны три субагента по AGENTS.md, серверные
проверки и запись обоих SHA в PR. Здесь цикл не проводился, PR не готов к merge.

Production factory `backend/src/honeyforge/main.py:create_app` уже включает
TelemetryRuntime: Bearer ingest `/agent/v1/events`, PostgreSQL event/outbox,
publisher, историю, catch-up и WS. Старые фразы в README/backend test README
«не подключён» описывают прежние этапы. Production Low mapper отсутствует:
`mapping.py` содержит Protocol, старый LowAdapter принимает другой JSONL.
Нельзя подавать журнал v1 в старый adapter или вставлять SyntheticMapper
в рабочий сборщик ради сквозной демонстрации.

## Изолированный Linux-каталог и зависимости

Нужны выделенный Linux сервер, Python 3.12, uv 0.11.28, PostgreSQL 17,
util-linux (flock, unshare, mount), sudo только для привилегированных родителей
двух файловых сценариев. Mount-сценарию требуется CAP_SYS_ADMIN и разрешённые
mount namespaces. Нет таких прав — обязательная проверка не выполнена.
Не выдавать CAP_SYS_ADMIN рабочему collector. UID/GID 65533 и группа 65532
используются только в изолированных фикстурах; на стенде исключить процессы
других пользователей с этими идентификаторами.

Команды ниже — Bash, только на сервере; `$HF_REPO` указывает на выделенный
checkout с итоговым пакетом, не на рабочий deployment. Его исходники и Python
должны быть доступны для чтения, а тестовый родитель имеет право создавать
фикстуры. `mktemp` создаёт отдельный каталог: не подставлять путь журналов Low.

```sh
set -euo pipefail
set +x
: "${HF_REPO:?Укажите выделенный checkout}"
cd "$HF_REPO"
HF_ACCEPTANCE_RUN=$(mktemp -d /tmp/hf-telemetry-acceptance.XXXXXXXX)
export HF_REPO HF_ACCEPTANCE_RUN
chmod 700 "$HF_ACCEPTANCE_RUN"
mkdir "$HF_ACCEPTANCE_RUN/tmp" "$HF_ACCEPTANCE_RUN/results"
export TMPDIR="$HF_ACCEPTANCE_RUN/tmp"
git rev-parse HEAD origin/main > "$HF_ACCEPTANCE_RUN/results/revisions.txt"
git status --short > "$HF_ACCEPTANCE_RUN/results/status.txt"
uv sync --frozen
(cd backend && uv sync --frozen)
```

`uv.lock` корня и `backend/uv.lock` независимы; backend dev-зависимость agent
указывает на `..`. Не использовать старое окружение основного checkout.
Версии Python/uv записать отдельно без env dump. Не менять lock-файлы.
Пример фиксации пакета (до исполнения):

```sh
sha256sum tests/telemetry/test_conversion_ack_crash.py \
  tests/telemetry/test_low_journal_linux_access.py \
  tests/telemetry/test_low_journal_readonly_mount.py \
  tests/telemetry/test_low_journal_durable.py \
  specs/low-journal-reader.md specs/low-telemetry-handoff.md \
  specs/architecture.md .github/workflows/ci.yml \
  infra/telemetry/server-acceptance.md \
  > "$HF_ACCEPTANCE_RUN/results/package.sha256"
```

## Независимые agent/Low проверки

Из корня выделенного checkout, обычный пользователь, без рабочих журналов:

```sh
uv run --frozen pytest tests/telemetry/test_low_journal_reader_contract.py \
  tests/telemetry/test_low_journal_reader_validation.py \
  tests/telemetry/test_low_journal_reader_files.py \
  tests/telemetry/test_low_journal_reader_rotation.py \
  tests/telemetry/test_low_journal_durable.py \
  tests/telemetry/test_raw_conversion.py \
  tests/telemetry/test_conversion_ack_crash.py \
  --basetemp "$HF_ACCEPTANCE_RUN/reader" -q --tb=no --disable-warnings
uv run --frozen pytest tests/telemetry/test_spool.py \
  tests/telemetry/test_batch_snapshot.py tests/telemetry/test_ack_contracts.py \
  tests/telemetry/test_ack_quarantine.py tests/telemetry/test_sender.py \
  tests/telemetry/test_sender_deadline.py tests/telemetry/test_sender_durable_http.py \
  tests/telemetry/test_nullable_source.py \
  --basetemp "$HF_ACCEPTANCE_RUN/delivery" -q --tb=no --disable-warnings
uv run --frozen pytest tests/low tests/infra/test_sample_low_journal.py \
  --basetemp "$HF_ACCEPTANCE_RUN/low" -q --tb=no --disable-warnings
```

Каждый basetemp уникален и отсутствует до запуска: pytest может очистить этот
каталог. При повторе создать новый HF_ACCEPTANCE_RUN. Loopback TCP/HTTP разрешить
только в этом стенде. `test_sender_durable_http` использует SQLite получатель;
это не PostgreSQL/production ingress. SyntheticMapper в конверсии — тестовый
Event runtime.state с собственной UUID-формулой, без production session.
Low-тесты используют реальный сервис/журнал, но не полный production маршрут.

Отдельный обязательный DAC запуск (не может стать зелёным через skip):

```sh
sudo env HF_REQUIRE_LINUX_ACCESS=1 TMPDIR="$TMPDIR" \
  "$HF_REPO/.venv/bin/python" -m pytest \
  tests/telemetry/test_low_journal_linux_access.py \
  --basetemp "$HF_ACCEPTANCE_RUN/dac" -q --tb=no --disable-warnings
```

Root родителя создаёт лишь TemporaryDirectory: Low root:65532 0640/0750,
фикстура находится непосредственно в `/tmp`, независимо от закрытого TMPDIR;
collector 65533:65533 со списком supplementary groups `[65532]` либо `[]`,
spool собственная writable. CapEff/CapPrm/CapAmb после setuid должны быть нулевыми
в обоих Linux-сценариях. Проверяются bytes, checkpoint без группы, реальные
операции append/truncate/unlink/rename/chmod, повторное открытие spool и её UID.
Режим обязательного CI шага в `.github/workflows/ci.yml` аналогичен.

Отдельный обязательный mount запуск, тоже fail при отсутствии предпосылок:

```sh
sudo env HF_REQUIRE_READONLY_MOUNT=1 TMPDIR="$TMPDIR" \
  "$HF_REPO/.venv/bin/python" -m pytest \
  tests/telemetry/test_low_journal_readonly_mount.py \
  --basetemp "$HF_ACCEPTANCE_RUN/mount" -q --tb=no --disable-warnings
```

Тест запускает `unshare --mount --propagation private`, bind/remount ro только
собственного временного source на временный mounted. Затем необратимо сбрасывает
группы/GID/UID. DAC намеренно разрешает запись владельцу; через mounted пять
операций должны дать именно EROFS. Spool находится вне mount и работает повторно.
Ожидания subprocess ограничены (25s целиком, mount 5s), namespace освобождается
при завершении процесса. Исходный source существует в тестовом namespace:
это доказательство запрета через mount-путь, не изоляция всех путей runtime.
CI сейчас требует DAC, mount остаётся отдельным серверным gate. Обычный pytest
без HF_REQUIRE_READONLY_MOUNT пропускает mount-тест: такой skip не засчитывается.

## PostgreSQL, секреты и штатные миграции

Следовать [README](../../README.md), разделам backend запуска/миграций, и
[серверному checklist](../../specs/server-checklist.md). Здесь нужен отдельный
PostgreSQL 17 cluster/endpoint с отдельным пользователем и **honeyforge_test**.
Совпадение имени БД недостаточно: оператор подтверждает отдельный host/port/роль.
`backend/tests/conftest.py` очищает таблицы, test_investigation очищает hf_telemetry;
запуск на рабочей БД недопустим. API стенд для будущего браузера — другая БД,
на ней эти pytest-команды не выполнять.

Администратор выделенного cluster создаёт ограниченную тестовую роль и БД.
Не использовать production superuser/пароль. PG credentials получить через
защищённый PGPASSFILE/secret manager, не URL в командной строке и не psql history.
После проверки отдельного endpoint пример штатного создания БД:

```sh
: "${HF_PG_MAINTENANCE_DB:?Выделенный endpoint без пароля в URI}"
: "${HF_PG_TEST_OWNER:?Созданная роль стенда}"
createdb --maintenance-db="$HF_PG_MAINTENANCE_DB" \
  --owner="$HF_PG_TEST_OWNER" honeyforge_test
```

Создать один раз, без автоматического удаления/пересоздания существующей БД.
`HF_ACCEPTANCE_ENV_FILE` — доверенный Bash-файл вне репозитория, 0600, содержит
export HF_TEST_DATABASE_URL, HF_DATABASE_URL (один выделенный honeyforge_test),
HF_SESSION_SECRET (>=32 символов), HF_CREDENTIAL_KEY (валидный Fernet),
HF_ENVIRONMENT=test, HF_COOKIE_SECURE=false,
HF_ALLOWED_ORIGINS='["http://localhost:5173"]'. Только тестовые ключи.
Для live acceptance использовать отдельный файл, HTTPS origins и Secure cookie.
Не печатать файл, токены/credentials, `env`, `set`, аргументы с паролем.

```sh
: "${HF_ACCEPTANCE_ENV_FILE:?Доверенный файл тестового окружения}"
set +x
. "$HF_ACCEPTANCE_ENV_FILE"
: "${HF_TEST_DATABASE_URL:?Только отдельная honeyforge_test}"
: "${HF_DATABASE_URL:?Только отдельная honeyforge_test}"
test "$HF_DATABASE_URL" = "$HF_TEST_DATABASE_URL"
: "${HF_ACCEPTANCE_LOCK:?Общий абсолютный lock-файл выделенного PostgreSQL стенда}"
export HF_ACCEPTANCE_LOCK
```

Использовать один согласованный абсолютный lock для всех участников стенда,
вне индивидуальных run-каталогов; родительский каталог заранее готовит оператор.
Ещё надёжнее выделить cluster одному участнику. PowerShell FileShare.None wrapper
из прежних запусков не является Linux flock: их нельзя считать общей блокировкой.
Ни API, ни второй pytest не должны писать в тестовую БД во время suite.
Открыть оболочку, удерживающую lock на всё время миграций и тестов:

```sh
flock --exclusive --nonblock "$HF_ACCEPTANCE_LOCK" bash
```

В этой оболочке, из `backend/`, сначала guard без вывода URL, затем штатные команды:

```sh
set -euo pipefail
set +x
cd "$HF_REPO/backend"
uv run --frozen python - <<'PY'
import os
from sqlalchemy.engine import make_url
for key in ("HF_DATABASE_URL", "HF_TEST_DATABASE_URL"):
    assert make_url(os.environ[key]).database == "honeyforge_test"
PY
uv run --frozen alembic upgrade head
uv run --frozen alembic check
uv run --frozen alembic downgrade base
uv run --frozen alembic upgrade head
uv run --frozen pytest tests/telemetry tests/test_investigation.py \
  --basetemp "$HF_ACCEPTANCE_RUN/postgres" -q --tb=no --disable-warnings
uv run --frozen pytest \
  --basetemp "$HF_ACCEPTANCE_RUN/backend-all" -q --tb=no --disable-warnings
uv run --frozen python -m honeyforge.cli export-openapi --check
exit
```

HF_REPO и HF_ACCEPTANCE_RUN экспортированы на этапе подготовки. Ошибка любого этапа блокирует приёмку;
не продолжать вслепую после failed guard/миграции. Downgrade уничтожает тестовые
таблицы: допустим только на описанном disposable cluster. Миграции не создавать
через metadata.create_all как замену Alembic. Test fixtures сами применяют upgrade.

Сетевой `tests/telemetry/test_ingress_end_to_end.py` проверяет реальный HTTP,
PostgreSQL и WS в integration_app с `/test/ingest`, фиксированной identity и
тестовой маской. `tests/test_investigation.py` проверяет factory, штатный ingest,
реальные auth/маршруты/PublicPolicy и lifespan publisher. Это разные доказательства.

ACK/commit gate: до успешного store commit нет accepted ACK, нет частичной пары
event/outbox; после потерянного ответа повтор принимается без второго event/outbox
и изменения оригинала/received_at. Проверять отдельным SQL connection.
Publication gate: ACK не означает публикацию. До отдельного publisher commit
нет видимого stream_seq; после commit одна publication и processed outbox.
Падения до/после publication commit, конкурирующие publishers и catch-up
проверяются существующими storage/ingress/stream тестами.

## Остальные проверки итоговой версии — также только сервер

После исправлений серверного ревью повторить соответствующие suites. Для передачи
PR человеку дополнительно исполнить команды актуального CI:

```sh
cd "$HF_REPO"
uv run --frozen ruff check agent honeypots infra tests
uv run --frozen ruff format --check agent honeypots infra tests
uv run --frozen mypy
uv run --frozen mypy --platform win32
uv run --frozen python -m agent.telemetry.export_contracts --check
uv build --wheel
cd backend
uv run --frozen ruff check . ../scripts
uv run --frozen ruff format --check . ../scripts
uv run --frozen mypy
cd ../frontend
npm ci
npm run typecheck
npm run test:ci
npm run build
```

Frontend Node 24, package-lock.json; CI также проверяет workflows/whitespace,
runtime tests/Compose/image. Полный список: `.github/workflows/ci.yml` и
`.github/CI.md`; здесь не объявлять пропущенные jobs успешными.
`mypy --platform win32` проверяет типы, не Linux DAC/mount.

## Production Low → основной backend → REST/WS → браузер: заблокировано

До согласования точных UUID/session, tcp.request, ограниченного HTTP mapping,
публичных полей и позднего null нет исполняемой полной команды collector.
Ответ Кирито ещё не получен; предложения не являются одобрением интеграций.
После решения владельцев реализовать и отдельно принять production mapper/wiring,
согласованные fixtures UUID/канонических bytes, HTTP budgets/ошибки/полноту.
Затем на отдельной live acceptance БД выполнить следующий сценарий:

1. Создать пользователя штатным `honeyforge.cli create-user --username ... --role admin`
   (пароль интерактивно), enrollment/узел/deployment/profile и instance штатным
   control/runtime потоком. Не подменять identity тестовой таблицей.
2. Запустить основной backend штатным `uvicorn honeyforge.main:create_app --factory`
   с конфигурацией отдельной БД, миграциями, TLS proxy и publisher lifespan.
   Внутренний loopback пример из корневого README:
   `uv run --frozen uvicorn honeyforge.main:create_app --factory --host 127.0.0.1 --port 8000 --no-proxy-headers`.
3. Запустить настоящий Low согласно `infra/runtime/executor.md`, читать только
   явно заданные журналы доверенного binding; collector непривилегированный,
   Low mount ro, собственная spool writable. Рабочие журналы не менять/не удалять.
4. Создать TCP/HTTP connection/request, два соединения одного IP, ротацию,
   source=null в согласованной фикстуре; проверить утверждённые UUID/session.
   Неполный/invalid/oversized HTTP, бинарные bytes, query/path секреты не дают
   выдуманного response/status/success или утверждения полноты захвата.
5. Для **существующей** spool доставку выполняет уже имеющийся entrypoint:
   `uv run --frozen python -m agent.telemetry.deliver --spool "$HF_SPOOL" --agent-id "$HF_AGENT_ID" --endpoint "$HF_INGEST_ENDPOINT" --once`.
   Bearer только HF_AGENT_TOKEN, endpoint основной `/agent/v1/events`, HTTPS
   вне loopback. Эта команда не собирает/конвертирует Low; до mapper шаг заблокирован.
6. Проверить production исторический instance, unknown retryable и foreign
   permanent, commit/ACK/publication независимо; отключить центр, накопить backlog,
   перезапустить collector/sender/центр и продолжить новыми событиями.
7. Собрать UI с `VITE_API_MOCK=0 VITE_EVENTS_ENABLED=1` (`npm run build`),
   проксировать `/api`, auth и WS на этот backend. Проверить null source и текущие
   DTO. Не включать analytics/replay без их реализации. В чистом браузерном профиле
   проверить отсутствие MSW/service worker, login и настоящие Network ответы.
   Разорвать WS, доставить события во время разрыва, восстановить: точные пары
   agent/event и stream_seq совпадают в REST/catch-up/live/UI без дублей/пропусков.
   Существующий `live-feed.spec.ts` ожидает MSW operator/operator и демо-IP;
   E2E_BASE_URL сам по себе не превращает его в production acceptance.

Не публиковать готовность этой последовательности, пока каждый этап не выполнен
на итоговом SHA с доказательствами. Реальный deployment/SELinux/network/TLS
остаются отдельными предпосылками; mount-фикстура их не заменяет.

## Матрица покрытия по чтению кода

В колонке запуска «не запущен» означает итоговую версию этого пакета, включая
старые suites. Исторические результаты не переносятся на неё.

| Требование | Существующий код/тест | Что фактически проверяется | Недостающая проверка | Согласование/стенд | Статус запуска |
|---|---|---|---|---|---|
| Реальные connection/HTTP/TCP, production mapping | LowService; tests/low/test_service.py, test_service_events.py; sample_low_journal; mapping.RawEventMapper | Реальный loopback сервис, bytes журнала; mapper только Protocol | Low v1 → production Event → основной backend | Решение mapping/tcp.request; Linux | Код Low реализован по чтению; mapping не реализован; тесты не запущены |
| Точные UUID, rename, разные позиции | LowAdapter; test_spool; reader_files; raw_conversion; ACK crash | Старый UUIDv5 и synthetic ID, rename raw, позиции ledger | Golden UUID утверждённого mapper (synthetic формула другая) | Владелец UUID | Требуется согласование; тесты не запущены |
| Session через ротацию, два соединения одного IP | tests/low/test_service_events.py; reader_rotation; durable actual_sink_connection_request_cross_files | Стабильность connection_id; raw не склеивается | Production session formula, ротация + два IP-equal connection | Runtime/telemetry/backend | Требуется согласование; тесты не запущены |
| source=null | nullable_source; test_investigation null_source_ingest_db_rest_ws_lookup_timeline; обе Event schema | Nullable контракты и основной backend, отсутствие IP IoC | Peer null → production mapper → UI | Mapper и frontend, сервер | Реализовано по чтению кода; тесты не запущены |
| Ограниченный HTTP-разбор, incomplete/invalid/oversized | LowService _read_headers/_parse_headers; tests/low/test_service.py | Ограниченное чтение/разбор самой ловушки | Независимый mapper budgets 4096/1024/32, serialized data limit | Proposal mapping | Mapper не реализован; Low тесты не запущены |
| Нет выдуманного HTTP response/полноты | low-journal-v1; LowService._emit | В журнале нет поля ответа/полноты | Mapper/UI unknown и отсутствие status/success | Публичные поля | Требуется согласование; сквозного теста нет |
| Секреты REST/WS/диагностика | PublicPolicy; test_investigation nested_public_payload_hidden_rest_ws_lookup; durable secret_payload_safe_error | Allowlist, query stripping, безопасные коды | Секрет в разрешённом path/username/terminal text, production payload; proposal строгой path | Backend/frontend политика | Реализовано текущее поведение по коду; согласование усиления; не запущено |
| Raw crash | durable process_crash_recovery | Crash отдельного процесса внутри raw транзакции/после commit | Питание/FS durability реального диска | Сервер/volume | Тест существует, не запущен |
| Ledger/pending crash | raw_conversion process_crash_before_and_after_commit | Реальные отдельные процессы, SQLite rollback/commit | Конверсия production Event | Mapper; сервер | Тест существует, не запущен |
| ACK loss и повтор после ACK | sender_durable_http; raw_conversion repeat_after_ack_and_restart; conversion_ack_crash | Потеря HTTP ACK у SQLite receiver; crash DELETE до commit/после ACK commit; raw/checkpoint/ledger/snapshot сохранены, старая позиция не requeue, следующая отличается | Основной ingest + production Low при потере ACK | Сервер/mapper | ACK тест уточнён, не запущен; прежние 2 passed исторические |
| Mapper/version конфликт | conversion.convert; raw_conversion raw_result_version_and_id_conflicts; ACK crash | RESULT_CONFLICT сохраняет evidence/pending | Утверждённые версии и план миграции | Владелец mapping | Реализовано по чтению кода; тесты не запущены |
| Новая граница процесса Low после аварии | collector топология; durable late_null_process_boundary_keeps_previous_checkpoint; raw_conversion new_process_same_instance | Поздний null блокируется; direct raw конверсия не склеивает journal_id | Trusted provenance/receipt, реальная авария Low | Runtime/telemetry решение | Два новых параметризованных сценария написаны, не запущены; переход не реализован |
| Независимые источники, storage exhaustion | durable independent_source_continues_after_missing_file, quota_lock_and_step_limits; raw_conversion sqlite_full_no_false_ledger | Другой binding продолжает; quota/SQLITE_FULL не продвигают ложный ledger | Ограниченный отдельный volume, справедливый scheduler реальных источников | Linux volume/runtime | Тесты не запущены; deployment требует сервера |
| Linux DAC | low_journal_linux_access | Другой UID, supplementary groups, own spool, отсутствие write/progress без группы | Runtime владельцы/предки/SELinux | Linux root родителя | Тест уточнён, не запущен; старые Windows 2 skipped не Linux результат |
| Read-only mount | low_journal_readonly_mount | EROFS через private bind ro при writable DAC, точные raw, reopen spool | Реальный collector mount без обходных путей | CAP_SYS_ADMIN только тестового родителя | Тест написан, не запущен; required flag исключает skip |
| Historical/unknown/foreign instance production ingress | IngressService.binding; main factory; test_investigation | Код сверяет историческую запись instance/revision, не current pointer; unknown INSTANCE_NOT_REGISTERED retryable; foreign DEPLOYMENT_MISMATCH permanent | Специальный production HTTP matrix после смены current instance, неизменная БД при отказах; test integration identity её не заменяет | PostgreSQL/control/runtime | Реализовано по чтению; отдельная приёмка требует сервера |
| Commit перед ACK | EventStorage.store; runtime.install.ingest; ingress_end_to_end storage_failure_never_acknowledges, lost_commit_response... | store возвращается после commit; тестовая HTTP сборка проверяет границы/повтор | Аналогичные fault injection через основной route | PostgreSQL сервер | Реализовано по чтению; тесты не запущены |
| Отдельная последующая публикация | OutboxPublisher; storage; ingress publisher_process_crash_and_restart; investigation production_lifespan_publishes_without_manual_call | Publication/processed commit, crash/повтор; production lifespan poller | Полный Low → UI и рестарт deployment центра | PostgreSQL/mapper | Реализовано по чтению; тесты не запущены |
| Рестарт collector/центра, backlog и новые события | durable real_low_rotation_restart; sender_durable_http; ingress lost_commit_response_restart... | Collector reopen, pending recovery, тестовый receiver/центр | Production Low + основной backend, новые события после восстановления | Сервер/mapper | Существуют частичные тесты; полный путь не реализован |
| Браузер без MSW, WS reconnect/catch-up | StreamClient.test.ts; stream_postgres; ingress_end_to_end; live-feed.spec.ts | Unit catch-up и сетевой WS; браузер E2E использует MSW | Браузер с реальным route, сверка точных IDs/seq и null source | Frontend/backend/mapper/сервер | Тесты существуют, не запущены; живая приёмка отсутствует |

## Завершение и доказательства

Вести таблицу SHA/команда/время/exit code/counts passed, failed, skipped и причины
skip; mandatory DAC/mount skip = незавершённая приёмка. Проверки можно считать
успешными только после реального завершения на сервере. Не сохранять payload,
spool/raw SQL dumps, HTTP bodies, cookies, Authorization, пароли или секретные
URL в общий отчёт. Подробные логи, если нужны для ошибки, держать в 0700 каталоге
на сервере, сначала редактировать секреты; --tb=no уменьшает риск вывода fixtures.
Playwright traces/screenshots/HAR тоже могут содержать секреты: не выгружать их
автоматически. Публиковать лишь проверенные сводки/безопасные коды/версии.

После каждого сценария subprocess/SQLite закрываются; namespace временного mount
исчезает с дочерним процессом. При отмене operator проверяет отсутствие тестовых
процессов/mount, завершает только процессы собственного run. Live sender/API
завершать SIGTERM штатно, ждать завершения и сохранять неподтверждённую spool.
Тестовые TemporaryDirectory убирают лишь собственные фикстуры. Здесь нет команды
автоматического rm исходных журналов, drop рабочей БД или Docker volume prune.
Отдельную БД и каталог результатов сохранять до разбора владельцем стенда;
cleanup/retention/receipt исходных журналов требуют отдельной договорённости.
