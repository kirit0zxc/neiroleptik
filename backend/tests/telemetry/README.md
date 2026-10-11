# Проверки внутреннего PostgreSQL-ядра

## Добавленный сетевой тестовый стенд

`integration_app.py` явно собирает существующие storage/publisher/history/catch-up/
presenter/stream/router и тестовый POST /test/ingest. `test_ingress_end_to_end.py`
использует установленный honeyforge-agent (dev), настоящий loopback HTTP sender,
PostgreSQL, REST и сетевой WS, реальные cookie/Origin/revoke и синтетическую маску.
Это тестовая identity-таблица и тестовые Event, не production authorization/Low→UI.
Команды, проверенная база, матрица C/P/L/T/E и результаты приведены в
`specs/telemetry-next-validation.md` (от корня). Все SQL-прогоны используют прежний
общий lock-wrapper. Автоматический сценарий — scripts/telemetry_acceptance.ps1.
Без HF_TEST_DATABASE_URL=…/honeyforge_test прогон завершается ошибкой, а не успехом.

## Исходное ядро и ранее выполненные проверки

Рабочее приложение, общая Alembic Base, production routes и worker не подключены.
События исходного SQL-набора уже подготовлены: этот набор не проверяет JSON/Bearer.
Нужны HF_TEST_DATABASE_URL с database=honeyforge_test и общий эксклюзивный
POSTGRES_TEST.lock из исходного work/telemetry-two-branches/BRANCH_HANDOFF.md.

Из корня своего worktree запускать scripts/run_backend_tests_locked.ps1,
передав абсолютные -UvPath, -BackendPath и -LockPath, затем tests/telemetry -q.
Для полного backend pytest не передавать testpath. Wrapper держит системную
FileShare.None блокировку до окончания процесса, миграций и cleanup; timeout 30s.
Нельзя запускать backend suites обоих worktree одновременно на одной БД.

Фикстура таблиц создаёт собственную telemetry_test_<uuid> и singleton счётчик,
затем удаляет только эту схему. Общий Engine получен из защищённого conftest.
Сессия каждого конкурентного участника отдельная. Ожидания SQL 10/15s,
управляемые Event/Barrier и future.result 10s. Все потоки завершаются до cleanup.

| Сценарии пакета | Исполняемое покрытие в test_storage.py |
| --- | --- |
| S01–S06 | insert_event_and_outbox; identical_retry; conflict; mixed_batch; two_agents; duplicate_keys |
| S07–S08 | rollback_and_commit_failure (event_inserted/before_commit); actual_commit_failure; unexpected_integrity_error |
| S09–S12 | lost_result; digest_is_not_equality; empty_input; projection_matches_original |
| S13–S15 | competing_insert_waits_for_commit_or_rollback, три параметризованных расписания |
| S16–S18 | reversed_overlapping_batches; uncommitted_event; constraints_enforced_in_database |
| S19–S22 | publish_atomically; publish_empty_and_repeat; rollback_before_commit; restart_after_commit |
| S23–S27 | two_publishers; late_ingest_commit; uncommitted_publication; rollover_limit_and_overflow; fk_prevents_orphan |
| S28–S32 | snapshot_keyset; equal_received_at; filters_and_read_only; mutable_caller; golden_bytes_preserved |

Дополнительно проверяются UNIQUE publication/outbox и запрет orphan при DELETE event.
Counts, оригинальные bytes, processed/publication и watermark читаются отдельным
connection. Проверка ожидания гонок основана на PostgreSQL pg_stat_activity Lock,
а не на sleep. Golden fixtures созданы действующим agent.telemetry.schemas.Event:
из корня запустить Python backend-окружения с
`-c "import runpy; runpy.run_path('backend/tests/telemetry/fixtures/generate.py')"`.
Pydantic 2.14.0/pydantic-core 2.50.0 закреплены одинаково в обоих uv.lock.
Ни приложение, ни pytest не импортируют agent и не меняют sys.path.

Изолированный test_prepared.py проверяет внутренние предусловия и отделённость
MetaData; модуль явно отменяет ненужный ему DB cleanup. Его зелёный результат
не заменяет S01–S32. Полный результат фиксируется отдельно в STORAGE_READY.md.
Отложены HTTP ingest/ACK, auth/lifecycle, production-миграция, общий Event-пакет,
публичная проекция, sender → HTTP → БД и Low → UI.

Проверенный стенд: отдельный локальный PostgreSQL 17.11, honeyforge_test с UTC;
Python 3.12.15. Итоговый полный backend pytest: 140 passed, 0 failed, 0 skipped;
в том числе 43 PostgreSQL-теста этого модуля и 7 изолированных тестов.
Результаты и SHA зависимости находятся в исходном STORAGE_READY.md.

## История и stream после включения storage

test_read_services/test_wire_fixture проверяют независимые read-only сервисы и
согласованные JSON fixtures; test_transport проверяет настоящие auth отказы.
test_auth_postgres и test_stream_postgres проверяют PostgreSQL, snapshot,
pending/rollback, bigint, race watermark → commit → polling, настоящие cookie
и маску всех трёх каналов. test_sql_output_matches_exact_frontend_wire связывает
SQL output с scripts/check_stream_wire.mjs, запускающим текущий StreamClient.
Матрица R01–R37 и ограничения: specs/telemetry-stream-validation.md.
Запуски выполняются только под общим FileShare.None lock. Production router
не подключён; проверка live Low и replay сюда не входит.
