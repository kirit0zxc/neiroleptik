# Проверка изолированного хранения Low boundary

## Перенос в main, 15.10.2026

PR №40 был слит в `codex/low-production-mapper`, поэтому его storage не попал
в основной проект. Коммит `e238afc54671d7d460ca778d5bb43e7fb1863bf6` перенесён
без изменения кода и без конфликтов в `fix/low-boundary-storage-main` от
`main e94579b` (включая постоянный сбор из PR №42). Новый PR направлен в `main`.

- Полный `pytest tests -q`: 1539 passed, 10 skipped, 37 subtests passed.
  Пропуски требуют POSIX/Linux/mount; их нельзя считать выполненными на Windows.
- Ruff lint/format, mypy и mypy --platform win32: успешно, 54 исходных модуля.
- Экспорт контрактов совпадает; wheel собирается успешно.
- На отдельной SQLite backup-копии реального spool из PR №42 проверено открытие
  через новые RawJournalRepository и SpoolRepository: содержимое всех 10 прежних
  таблиц данных сохранено, версия миграции равна 1, доверенные bindings не созданы.
  Оригинальная очередь открывалась только на чтение и не изменялась.
- Тесты постоянного worker из PR №42 входят в полный прогон. Production consumer
  переходов и автоматическое разрешение нового корня журнала не добавлены.

Ниже сохранён исторический отчёт исходного PR №40. Его указания о зависимой
ветке и отсутствии mapper в main относятся только к 14.10.2026 и больше
не определяют базу нового PR. Итоговые SHA, авторевью и CI фиксируются в новом PR.

## Исходная проверка PR №40, 14.10.2026

Дата: 14.10.2026, Windows, Python 3.12.13. Ветка `codex/low-boundary-storage`.
Рабочая копия `work/low-boundary-storage`; посторонние изменения в других
рабочих копиях сохранены. Проверенный main:
`01f8272ba37ef16af7b905042b00a7a1d36808ca`.
Зависимая база PR #39 (mapper):
`20f19d2ce3c1aae6ec79205a971a6d358260dfb1`.
`git fetch origin` выполнен перед итоговыми проверками;
`git merge-base --is-ancestor origin/main HEAD` — exit 0.
Main уже входит в базу; конфликтов нет. Финальный SHA рабочей ветки фиксируется
в описании PR после коммита; самоссылка на ещё не созданный commit здесь не используется.

Mapper не вошёл в проверенный main: `agent/telemetry/low_journal/production.py`
отсутствует в дереве origin/main. Для отдельного diff базой PR должна быть
`codex/low-production-mapper`; перед переносом PR на main нужно повторно проверить
актуальное сочетание. Состояние open/closed/merged PR #39 не подтверждено:
GitHub connector возвращает 404, браузер без авторизации также показывает 404.
Это не трактуется как closed или отсутствие PR. Проверенный `refs/pull/39/head` через `git ls-remote`
совпадает с `origin/codex/low-production-mapper` и содержит указанную базу; production mapper не дублируется в diff этого изменения.
Ветка `pr-39` с SHA 01599aede963b0903c9dc9b805bdae5068268ba7 — отдельная
документация runtime, не head PR #39; её commit не входит в историю этого PR.
Два небольших исходных JSONL sample из неё перенесены в тестовые fixtures
с теми же bytes для независимого воспроизведения B02/F.

## Команды и фактические результаты

Использованы Python из локального `.venv` и закреплённый uv; `python -m` ниже
эквивалентен `uv run --frozen` после успешного `uv sync --frozen`.
Для тестов TEMP/TMP и `--basetemp` направлены в игнорируемый `artifacts/` рабочей
копии, запуск с разрешением loopback и subprocess. Первые sandbox-прогоны с
системным TEMP/закрытой сетью завершались ошибками окружения и не считаются
успешной проверкой. При промежуточном полном прогоне обнаружена ошибка ожидаемого
кода в новой фикстуре (ID старого и нового header совпали); исправлена фикстура,
затем весь набор выполнен заново. Независимый рецензент выявил, что конкурентный
тест не разрешал допустимый WRITER_BUSY. Добавлены явный retry после завершения
потоков и детерминированный busy-сценарий через Event; итоговый полный прогон
повторён на точном head PR #39. Старые тесты не изменены и не ослаблены.

| Команда | Exit | Результат |
| --- | --- | --- |
| `uv sync --frozen` | 0 | Зависимости из uv.lock, Python 3.12.13 |
| `python -m pytest tests/telemetry/test_boundary_storage.py -q --basetemp=artifacts/pytest-rebased` | 0 | 94 passed, 0 failed/skipped |
| `python -m pytest tests -q --tb=short --basetemp=artifacts/pytest-rebased-final --junitxml=artifacts/rebased-final.xml` | 0 | 1531 passed, 10 skipped, 37 subtests passed; telemetry и runtime вместе |
| telemetry-поднабор этого полного прогона | 0 | 654 passed, 3 skipped, 37 subtests passed; нет failures/errors |
| `python -m ruff check agent honeypots infra tests` | 0 | All checks passed |
| `python -m ruff format --check agent honeypots infra tests` | 0 | 99 файлов соответствуют формату |
| `python -m mypy` | 0 | 53 source files, без ошибок |
| `python -m mypy --platform win32` | 0 | 53 source files, без ошибок |
| `python -m agent.telemetry.export_contracts --check` | 0 | JSON Schema не изменились |
| `python -m agent.telemetry.demo` | 0 | 100 долговечных событий, batch 50128 bytes; все ID сохранены |
| `uv build --wheel` | 0 | honeyforge_agent-0.1.0-py3-none-any.whl |
| Проверка ZIP wheel и локальных Markdown-ссылок | 0 | Оба новых модуля включены; ссылки разрешаются |
| `git diff --check` | 0 | Нет пробельных ошибок |

JUnit и wheel — игнорируемые локальные артефакты, не входят в commit.
Результаты pytest выше относятся к итоговому коду и неизменённым старым тестам.
Последующая запись этого отчёта не меняет исполняемую часть.

## Покрытие и пределы доказательства

Новая матрица хранится в [low-boundary-storage-test-contract.md](low-boundary-storage-test-contract.md),
реализация описана в [low-boundary-storage.md](low-boundary-storage.md).
SQL-оракул читает все логические таблицы и схему через отдельный connection.
Миграция проверена со старой явной схемой и заполненными raw/checkpoints/ledger/
pending/quarantine/batch snapshots, включая порядок pending и чужую metadata.
Все изменяющие шаги миграции и принятия имеют fault tests. Есть конкуренция
connections с Barrier, детерминированный busy с Event и явным retry, реальные crash-процессы с IPC, ошибка COMMIT через узкий
адаптер и инъекция SQLITE_FULL; дополнительно SQLite сама возвращает FULL из-за
лимита страниц только выделенной временной БД. Диск пользователя не заполняется.

Пропуски полного набора: 7 POSIX-only проверок (Low journal permissions, runtime
permissions/FIFO/symlink), 2 Linux UID-теста и 1 Linux read-only mount-тест.
Три последних составляют telemetry skips. Пропуски обусловлены Windows и не
объявляются успешными тестами прав или mount.

Frontend, backend, HTTP/WS API, Event DTO, OpenAPI, source_seq и ID событий не
изменены. Совместимость проверена в пределах общей agent spool: ACK, conversion,
collector/reader/rotation/replay и runtime. PostgreSQL, frontend E2E, Docker,
Linux fencing/fsync/права и producer не запускались для этой поставки.
Новый prepared storage не подключён к collect_low/collector: поздний null
продолжает ждать, старый verified raw конвертируется. Начальная привязка
задаётся только тестовой SQL-фикстурой; production bootstrap отсутствует.

Ревью трёх субагентов выполняется на этой итоговой версии до push; первоначальные
отчёты первых двух независимы, третий повторно проверяет оба. Краткие результаты
и идентификаторы рецензентов прикладываются к описанию PR, а не выдаются за Approve.
Remote CI и человеческое одобрение этим отчётом не подтверждены. Изменение не
считается готовым к слиянию до выполнения AGENTS.md.

Настоящий consumer перехода и совместный тест с producer в эту поставку не входят.
