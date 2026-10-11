# Следующий этап телеметрии: проверенная внутренняя реализация

Дата: 11.10.2026. Ветка: `codex/telemetry-next`.
Рабочая копия: `C:/DTSU/vsprojects/neiroleptik/work/telemetry-next-implementation`.
Исходная база PR26: `aeaad8c614a0318bce47264237be8f8d3c6d6840`, исходный main
`69a061a16a8ebd3e8b3a2094106510c5c6c8137c`. Во время работы PR26 был слит:
финальный HEAD `2e7376ab5d90c04c7b949aa44a5b07c1dfe8d36b`, 13:35:57 Москва.
Рабочая ветка обновлена fast-forward до актуального main
`fc473ff02ac71d5c3e86ae38f20ee92584258360`; конфликты отсутствовали.
Проверки ниже относятся к сочетанию этой базы и внесённых файлов. Итоговый локальный
HEAD получают `git rev-parse HEAD`; после commit сохранён artifacts/revision.json.
Отслеживаемые файлы общего checkout и PR26 не изменялись; исходный пакет
work/telemetry-next обновлён
по переданным ответам участников и скопирован в эту ветку.

## Результат и границы

LowRawConverter создаёт pending и устойчивый ledger одним SQLite commit; повтор
после accepted/permanent ACK или рестарта не восстанавливает pending. Raw/checkpoint
не меняются. Версия/оригинал/результат/занятый Event ID дают безопасный конфликт.
Проверены две одновременные конверсии, writer-lock, лимиты, настоящий SQLITE_FULL,
реальные INSERT/COMMIT отказы и аварийные процессы. Непустая старая spool без ledger
открывается с сохранением pending/quarantine/snapshots/checkpoints/metadata/raw.
Синтетический mapper не определяет production UUID namespace или Low mapping.

Тестовое приложение соединяет настоящие EventStorage, OutboxPublisher,
PublicationReader, CommittedReadAdapter, History/CatchUp/Presenter/LiveStream/Router.
HttpBatchTransport/TelemetrySender отправляют настоящий HTTP на временный loopback
порт; WS также сетевой. PostgreSQL хранит реальные оригинал/outbox/publication.
User/UserSession и telemetry используют отдельную telemetry_test_<uuid> схему;
общий backend conftest выполняет штатные тестовые миграции под прежним lock.
Production routes/Alembic/agent auth/runtime wiring не добавлены.

ACK приходит после commit event+outbox при остановленном publisher. Потеря тела
ответа после commit, timeout, 503 и invalid ACK сохраняют неподтверждённую очередь.
Проверены partial ACK с конфликтным/невалидным соседом, конкурентные POST,
publisher rollback и настоящий os._exit отдельного publisher процесса до/после
commit, стабильный REST snapshot, WS разрыв→REST догрузка→live, пары ID разных
агентов и десятичные stream_seq >2^53. Cookie/Origin/expiry/revoke реальные;
тестовый фиксированный Bearer→identity не доказывает production authorization.
REST/WS используют одну синтетическую password-маску; скрытый секрет всё равно
участвует в сравнении canonical оригинала. Полная политика REST/WS/export отложена.

## Выполненные проверки

Окружение: Windows 11 (10.0.26200), Python 3.12.13, SQLite 3.53.1,
PostgreSQL 17.11 на loopback:55439, отдельная honeyforge_test, Pydantic 2.14.0.
Локальный PostgreSQL был остановлен; для проверок запущен прежний тестовый кластер
pgdata2 и оставлен доступным. Секрет URL читался только из игнорируемого .env,
в команды/отчёты/коммит не включён. Все SQL тесты удерживали FileShare.None lock
`C:/DTSU/vsprojects/neiroleptik/work/telemetry-two-branches/POSTGRES_TEST.lock`.

| Проверка | Фактический результат / доказательство |
| --- | --- |
| Полный корневой pytest на актуальном main | 1356 passed, 7 skipped, 37 subtests, 107.87 s; artifacts/agent-combined.xml/log |
| Полный backend pytest на актуальном main | 234 passed, 0 skipped, 143.78 s; artifacts/backend-combined.xml/log |
| Окончательные новые независимые тесты после добавления C11 upgrade | 50 passed: conversion 30, benchmark 8, tty 12; artifacts/independent-final.xml |
| Окончательная HTTP/PostgreSQL/REST/WS матрица после добавления subprocess publisher crash | 15 passed; artifacts/ingress-process.xml |
| Ruff check / format root agent/tests/telemetry и backend/scripts | passed, 53/70 файлов formatted |
| Mypy root, root --platform win32, backend | passed, 49/49/44 source files |
| agent.telemetry.export_contracts --check | passed, Event/Batch/ACK схемы unchanged |
| honeyforge.cli export-openapi --check | passed, production OpenAPI unchanged |
| Agent/backend wheel | построены uv 0.11.28; новые модули импортированы из установленного wheel вне checkout |
| backend --frozen --no-dev в аналоге Docker context без parent agent | passed; runtime dependency honeyforge-agent отсутствует; uv 0.11.28 совместим с lock |
| Автоматическая приёмка, два запуска | 25 точных пар/оригиналов, (event,outbox,publication)=(25,25,25), pending=0, duplicate publications=0; новая схема/порт/spool каждый раз |
| Приёмка с недоступным тестовым PostgreSQL URL | ожидаемый exit=1 и JSON status=failed / TEST_OR_INFRASTRUCTURE_FAILURE; artifacts/acceptance-unavailable |
| git diff --check | passed |

Полные suite были выполнены до последних трёх дополнительных тестов (C11 и двух
процессных P06). Затем окончательные изменённые наборы выполнены полностью (50/15);
код остальных модулей не менялся. Не выдаём эти дополнения за новый единый полный
прогон 1357/236. Семь skips — существующие Windows ограничения POSIX прав/FIFO/
symlink, это не выполненные Linux проверки. Есть deprecation warnings FastAPI/
Starlette и legacy websockets; ошибок тестов нет. Ранние прогоны без loopback прав,
с остановленной БД/недоступным temp каталогом были ошибками среды, не успехом;
итоговые прогоны использовали правильные права, БД и basetemp внутри worktree.

## Матрица доказательств

| Требования | Исполняемое покрытие |
| --- | --- |
| C01/C02/C03 | test_atomic_service_event_and_unchanged_raw; test_mapper_failure_has_no_evidence |
| C04/C05 | real_insert_failure, real_commit_denied, fault_boundaries, process_crash_before_and_after_commit |
| C06/C07/C08 | repeat_after_ack_and_restart; raw_result_version_and_id_conflicts; existing_spool_snapshots; id_reuse_from_another_position_after_ack |
| C09/C10/C11 | two_connections_same_position; step_limits_lock_and_progress; sqlite_full; upgrade_nonempty_legacy_spool |
| P01/P02/P11 | lost_commit_response_restart_ack_before_publisher_and_live; automatic_acceptance |
| P03/P04/P05 | storage_failure_never_acknowledges; mixed_conflicting_hidden_original_and_invalid_neighbors; concurrent_http_duplicates_and_conflicts |
| P06/P07/P08/P09 | publisher_process_crash_and_restart; publisher_rollback_restart_snapshot_reconnect_and_bigint |
| P10/P12 | access_cookie_origin_expiry_and_idle_revoke; backoff_bad_ack_and_recovery; invalid_batch_test_auth_and_identity |
| L01/L02/L03/L05 | benchmark CLI и test_benchmark; настоящий max_page_count SQLITE_FULL в conversion тесте |
| L04 | Не выполнено: нет выделенного ограниченного volume; системный диск не заполнялся |
| T01–T04 | test_tty_fixture: наш hf-test-tty/1, incremental UTF-8, bounds/EOF/direction/time/controls |
| E01/E02/E03 | telemetry_acceptance.ps1, два успешных изолированных запуска и ожидаемый failure при недоступной БД |
| E04 | Отложено: production Low→Event→БД→браузер не подключён |

Tty sample — физически заданные test bytes из собственного versioned формата,
не Cowrie; реальный Cowrie/SFTP требует версию и настоящие журналы. Crash процесса
не доказывает физическую устойчивость при отключении питания. В нагрузке измеряется
Event→SQLite→синтетический ACK, а не PostgreSQL throughput или потери сетевых
запросов до durable append. Cleanup/retention отсутствуют.

## SQLite/WAL замер

Воспроизводимый запуск: 200 Event, 1024 ASCII bytes padding на Event,
requested ingress=1000/s, outage=0.2 s, receiver=10000/s, lose_once, seed=20261011,
max_seconds=30. Фактический elapsed=1.657 s; backlog max=200, delivered unique=200,
pending=0. Измеренный ingress=148.81/s, drain=137.27/s; это результат этого малого
стенда со стоимостью мониторинга, не целевые throughput. Drain после настроенного
outage до пустого pending=1.457 s (включает продолжающийся ingress).
Append commit→локальное применение accepted ACK: max=1.546 s, p95=1.484 s.
DB/WAL/SHM peak=471040/4124152/32768 bytes; pending canonical TEXT peak=298290 bytes,
snapshot BLOB=298733 bytes, raw BLOB=0 (в этом Event-стенде raw не генерируется).
Минимально свободно 181716692992 bytes. Машинный отчёт artifacts/benchmark.json.
Это отдельные логические payload и физические файлы; реальный ENOSPC не проведён.

## Воспроизводимые команды

Из корня этой рабочей копии; нужен Python 3.12 и uv 0.11.28. Backend содержит
agent/websockets только как dev зависимости. HF_TEST_DATABASE_URL берётся из
отдельной тестовой конфигурации, не из отчёта.

```powershell
uv sync --frozen
uv sync --frozen --directory backend
uv run --frozen pytest -q --basetemp=artifacts/tmp-agent --junitxml=artifacts/agent.xml
uv run --frozen ruff check agent tests/telemetry
uv run --frozen ruff format --check agent tests/telemetry
uv run --frozen mypy
uv run --frozen mypy --platform win32
uv run --frozen python -m agent.telemetry.export_contracts --check
uv run --frozen --directory backend ruff check . ../scripts
uv run --frozen --directory backend ruff format --check . ../scripts
uv run --frozen --directory backend mypy

# Этот wrapper включает штатные test миграции и cleanup, удерживая общий lock.
$testLock = 'C:/DTSU/vsprojects/neiroleptik/work/telemetry-two-branches/POSTGRES_TEST.lock'
$testPython = (Resolve-Path 'backend/.venv/Scripts/python.exe').Path
Push-Location backend
try {
    ../scripts/test_backend_locked.ps1 -Python $testPython -LockPath $testLock `
        -TestArguments @('-q','--basetemp=../artifacts/tmp-backend','--junitxml=../artifacts/backend.xml')
} finally { Pop-Location }
scripts/telemetry_acceptance.ps1 -Python $testPython -LockPath $testLock `
    -ReportDirectory artifacts/acceptance
uv run --frozen python -m agent.telemetry.benchmark --directory artifacts/benchmark `
    --report artifacts/benchmark.json --count 200 --payload-bytes 1024 `
    --ingress-rate 1000 --outage-seconds 0.2 --receive-rate 10000 `
    --receiver-mode lose_once --seed 20261011
uv build --wheel --out-dir artifacts/wheels
uv build --wheel --directory backend --out-dir ../artifacts/wheels
```

Linux использует тот же pytest и изолированную test DB; принятый Windows lock-wrapper
нельзя запускать параллельно с другим владельцем общей БД. Backend OpenAPI export
требует обычные test Settings (secret/credential key/database/environment), не
ослабление production Settings. Docker image здесь не строился: CLI Docker отсутствует;
проверена изолированная --no-dev установка и wheel. Браузерный прогон не проводился.

## Оставшиеся согласования и статус передачи

Ответы backend/runtime и варианты с примерами внесены в low-telemetry-handoff.md,
events.md, low-journal-reader.md и work/telemetry-next/{01_PROMPT,02_SPEC,03_TESTS}.md.
Не запрашиваем повторно описанные runtime факты. Требуются только доверенная граница
crash/процесса и provenance order; UUID golden examples; nullable source/tcp.request/
session/capture/полная проекция с Дианой; cleanup receipt+replay; Linux UID/mounts;
disk-pressure admission/recovery; внешний TLS endpoint. Production wiring/Alembic/
авторизацию/worker делает backend. Оборванный конец runtime ответа не достроен.

Результат локальный, push/новый PR не выполнялись. Цикл трёх субагентов требуется
перед push по AGENTS.md и пока не выполнялся; это не отказ из-за недоступности.
Для слияния также нужны итоговый CI и явное независимое человеческое одобрение
актуальной версии. ИИ не выставляет Approve и не выполняет merge.
