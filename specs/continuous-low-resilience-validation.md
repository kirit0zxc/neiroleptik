# Отказоустойчивость непрерывного Low: исполненные проверки

Дата: 15.10.2026. Среда: Windows, CPython 3.12.13, uv 0.11.28, frozen root
`uv.lock`. Проверены актуальный `origin/main`
`65fc136f44c4eb4f8bd260f61eebccf31e20e1e3` и реализация
`1925c52360f7903520b85ae994522e91b22eab6b` в ветке
`codex/continuous-low-resilience`. Реализация основана непосредственно на этом
main, конфликтов нет. Итоговый SHA ветки с данным отчётом и результаты трёх
независимых ревью фиксируются в описании PR; данный документ не заменяет ревью.

## Изменения и результат

Добавлен 21 исполняемый сценарий (с учётом параметризации). Стенд использует
настоящие worker, collector, production mapper, SQLite WAL/FULL, HttpBatchTransport
и HTTP receiver с собственной долговечной SQLite. Ожидаемые UUIDv5 и canonical
DTO вычисляются из входных bytes независимо от mapper и ACK. Состояние spool
читается отдельным read-only SQL connection без вызова миграций.

Подтверждённых дефектов production-поведения в проверенной матрице не обнаружено.
Единственное изменение production-кода — keyword-only clock/jitter в constructor
LowTelemetryWorker, передаваемые существующему sender. Defaults, последовательный
порядок collector/converter/sender и рабочие лимиты сохранены. Внешние API,
Event/ACK, UUID, source_seq, доверие и restart Low не изменены. Новые соглашения
runtime/backend, миграции, retention и boundary acceptance не добавлены.

## Результаты по матрице

Все строки исполнены на Windows, обязательных пропусков нет. Имена ниже —
pytest-функции в `tests/telemetry/`; их запускает полный root pytest.

| ID | Файл / тест | Фактический результат |
| --- | --- | --- |
| C01 | test_continuous_low_executor.py::test_c01_c02_failed_unacked_start_then_ack_stop_reopen_and_footer | PASS: файл failed/prepared/completed-unacked start не разрешает сбор; только успешный ACK экспортирует tuple |
| C02 | тот же тест | PASS: stop/reopen сохраняет исторический binding; footer есть в raw/service ledger, без нового POST/ID |
| C03 | test_continuous_low_resilience.py::test_c03_503_collects_during_backoff_and_delivers_exact_union | PASS: два новых наблюдения собираются без POST во время backoff, старый batch повторяется буквально; затем точное объединение ID/bytes |
| C04 | test_continuous_low_resilience.py::test_c04_refused_reserved_socket_then_receiver_on_same_endpoint | PASS: bound socket без listen даёт реальный отказ и удерживает порт; тот же socket передан receiver; retry доставляет исходное событие |
| C05 | test_continuous_low_process.py::test_c05_c13_new_process_recovers_exact_saved_events[lost_ack] | PASS: commit до разрыва ACK, новый процесс и batch_id, прежние Event bytes; одна запись на agent/event |
| C06 | test_continuous_low_resilience.py::test_c06_bad_ack_is_atomic_then_valid_ack_accounts_for_all | PASS ×3: чужой batch_id, некорректный JSON и неполный ACK сохраняют всю pending без частичного quarantine; хороший ACK доставляет оба Event |
| C07 | test_continuous_low_resilience.py::test_c07_mixed_ack_preserves_raw_ledger_and_does_not_resend_permanent | PASS: ровно один accepted, permanent в quarantine, retryable в pending; повтор содержит только retryable, raw/ledger сохранены |
| C08 | test_continuous_low_resilience.py::test_c08_auth_pause_collects_until_restart | PASS ×2: отдельно 401/403, один POST, ещё три строки конвертируются; restart доставляет четыре точных Event |
| C09 | test_continuous_low_resilience.py::test_c09_c10_round_robin_keeps_independent_progress_and_saved_delivery[corrupt] | PASS: INVALID_RECORD не продвигает checkpoint через ошибку; старый pending доставлен, второй binding обслужен на втором step |
| C10 | тот же тест [late_null] | PASS: прежний код ожидания; сохранённый до конверсии raw обработан; независимый binding обслужен, boundary binding/writer не создан |
| C11 | test_continuous_low_resilience.py::test_c11_partial_line_restart_completion_footer_and_replay | PASS: частичный хвост не двигает checkpoint до/после restart; дописывание создаёт один точный Event, replay без нового POST |
| C12 | test_continuous_low_process.py::test_c12_real_low_tcp_http_child_production_mapping_and_transport | PASS: дочерний настоящий Low с портами 0 и IPC readiness, TCP binary/Unicode и HTTP Unicode; четыре connection/request Event, шесть raw/service+event позиций |
| C13 | test_continuous_low_process.py::test_c05_c13_new_process_recovers_exact_saved_events[pending_crash] | PASS: transport выдаёт flush IPC после pending/ledger commit и вызывает os._exit(79); новый процесс сохраняет raw/checkpoint/ledger и доставляет точные Event |
| C14 | test_continuous_low_executor.py::test_c14_real_http_deadline_allows_control_coroutine_and_retains_queue | PASS: receiver удерживает настоящий HTTP, контрольная coroutine завершается до worker; transport deadline сохраняет pending; последующий retry доставляет |
| C15 | test_continuous_low_executor.py::test_c15_cancel_waits_before_store_release_and_closes_on_worker_thread | PASS ×2: constructor/step; close в том же единственном потоке; независимый процесс получает STORE_LOCKED до release, OPENED после закрытия store |
| C16 | test_continuous_low_executor.py::test_c16_executor_prints_codes_without_private_input; test_continuous_low_resilience.py::test_c16_safe_worker_diagnostics_for_payload_ack_token_and_transport | PASS ×2: реальные stdout/stderr ExecutorTelemetry и безопасные результаты не содержат token/payload/query/ACK/exception/path/Authorization, коды видимы |
| C17 | test_continuous_low_resilience.py::test_c17_raw_budget_does_not_remove_saved_pending_or_advance_checkpoint | PASS: STORAGE_LIMIT видим, raw/checkpoint/ledger/usage не изменены, старый pending доставлен; новая строка остаётся на диске |

Старые unit-тесты sender/durable/conversion и ExecutionStore сохранены. Новые
проверки добавляют уровень взаимодействия worker с настоящим HTTP и процессами.
C15 использует управляемый worker с Event; настоящий HTTP отдельно сохранён C14.

## Команды и фактические итоги

Команды выполняются из корня worktree. Локальный `uv.exe` установлен в
игнорируемый `artifacts/tools/bin`; этот каталог добавлен в PATH, UV_CACHE_DIR
направлен в `artifacts/uv-cache`. Для demo TEMP/TMP направлены в `artifacts/tmp`.
Это подготовка окружения, не изменение зависимостей проекта.

| Команда | Exit | Результат |
| --- | --- | --- |
| uv sync --frozen --python C:/DTSU/vsprojects/neiroleptik/.venv/Scripts/python.exe | 0 | Изолированная .venv Python 3.12.13, 22 frozen packages |
| uv run --frozen pytest tests/telemetry tests/runtime -q --basetemp=artifacts/pytest-focused --junitxml=artifacts/focused.xml | 0 | 1396 passed, 0 failed, 9 skipped; 37 subtests passed, 122.58 s |
| uv run --frozen pytest tests -q --basetemp=artifacts/pytest-root --junitxml=artifacts/root.xml | 0 | 1560 passed, 0 failed, 10 skipped; 37 subtests passed, 117.49 s |
| uv run --frozen ruff check agent honeypots infra tests | 0 | All checks passed |
| uv run --frozen ruff format --check agent honeypots infra tests | 0 | 107 files already formatted |
| uv run --frozen mypy | 0 | 54 source files, no issues |
| uv run --frozen mypy --platform win32 | 0 | 54 source files, no issues |
| uv run --frozen python -m agent.telemetry.export_contracts --check | 0 | Контракты и fixtures совпали |
| uv run --frozen python -m agent.telemetry.demo | 0 | 100 Event, batch 50128 bytes, все ID различны, offline без сети |
| uv build --wheel | 0 | dist/honeyforge_agent-0.1.0-py3-none-any.whl |
| git diff --check | 0 | Пробельных ошибок нет |

Локальные полные логи и JUnit находятся в игнорируемом `artifacts/`, в Git их нет.
Focused-прогон предшествует последней правке timeout cleanup в тестовом Low reader;
полный root-прогон выполнен уже на итоговой реализации `1925c523…`.

Все десять root skips существовали до этого этапа: POSIX journal (1), POSIX
ExecutionStore permissions (4), POSIX token/FIFO/symlink (2), отдельный Linux
collector UID (2), привилегированный read-only mount (1). Ни один C01–C17 не skipped.
CI `telemetry` запускает весь root pytest, `runtime` — tests без telemetry;
новые сценарии включены автоматически, workflows не изменены.

## Ограничения среды и доказательства

Sandbox запрещал loopback connect (первые HTTP-попытки давали TRANSPORT_ERROR
без запроса receiver), запись в системный uv cache/TEMP и обновление PE resources
при сборке. Реальные сетевые/process-прогоны и build повторены с разрешённым
окружением и завершились успешно. Эти отказы окружения не объявлены дефектами
продукта. Ошибки разработки фикстур (удаление ledger при уже сохранённом snapshot,
HTTP/1.1 без Host) исправлены в тестах без ослабления production-защит.

| Уровень | Проверено локально |
| --- | --- |
| Synthetic fixture | Да: независимые expected UUID/DTO/offsets по исходным bytes |
| Настоящий Low | Да: TCP и HTTP, binary/Unicode, файловый JSONL и footer |
| SQLite | Да: настоящие WAL-файлы, read-only второй connection, restart/crash |
| Настоящий HTTP | Да: HttpBatchTransport и loopback receiver, ошибки/разрыв/deadline |
| Дочерний процесс | Да: Low, crash worker, новый worker, независимый владелец store |
| Linux | Нет: WSL не установлен; POSIX permissions/UID/mount требуют отдельного Linux CI |

Не проверены production Bearer ownership, PostgreSQL dedup, outbox/publication,
REST/WebSocket, UI/MSW, Docker/KVM и runtime fencing. Crash os._exit не доказывает
power-loss/fsync оборудования. Boundary producer/consumer, anchor/proof/writer
bootstrap и автоматическая очистка вне этого изменения. Нового решения об API
или lifecycle не потребовалось. До успешного CI итогового SHA и явного одобрения
человеком изменение не готово к слиянию; агент Approve и merge не выполняет.
