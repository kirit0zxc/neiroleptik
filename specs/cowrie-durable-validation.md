# Durable Cowrie: проверка второго этапа, 16.10.2026

Ветка `codex/cowrie-durable-worker`, зависимость — чистые интерфейсы из PR #53.
База main: `6183092dc9234dd35f25c94834dc539b48f078ea`, base PR #53:
`a90bf70eb98963989fb2bbfa44e0c83928dab89f`. Итоговый SHA, результаты трёх
рецензентов и GitHub CI фиксируются в PR.
Новый HTTP API, replay upload/read и изменения legacy Event replay в этот PR
не входят. Это локальный SQLite collector/worker, не PostgreSQL-приёмка.

## Состав изменения

`agent/telemetry/cowrie_journal` разделяет доверенные модели, безопасное открытие
файлов, raw/checkpoint/ledger, bounded collector и атомарную конверсию.
Worker использует прежний SpoolRepository/TelemetrySender в том же потоке.
Опциональный внутренний provider передаёт реестр UUID поколений; worker сверяет
его с историческими ACK start/reset. Отсутствие provider не означает разрешение
угадать UUID по имени файла или прочитать путь из JSON. Автоматическая активация
в CLI не вводится: существующий runtime пока не экспортирует этот реестр.

Подключённые durable scope закрепляются в SQLite. Старый collector с replay
сохранён для остальных; после legacy checkpoint автоматическое переключение
запрещено. Для нового instance нужен явный доверенный source. Исторические
ACK после stop остаются допустимыми для дочитывания. Ошибка одного поколения,
неправильный snapshot provider или неизвестная Cowrie storage version не
отменяют sender и независимый Low. Неизвестная версия не разрешает fallback
к legacy collector с другой системой UUID.

## Автотесты

`tests/telemetry/test_cowrie_durable.py` проверяет настоящую SQLite:

- Raw сохраняется до mapper; Event/ledger фиксируются одной транзакцией.
  Семь точек crash проверяются дочерним процессом с `os._exit(77)` и reopen.
- Повтор после ACK/quarantine не создаёт pending, исходник и ledger остаются.
  Подмена raw и версии/владельца отвергается; квота откатывает транзакцию.
- Неизвестный валидный тип получает ignored, полная неверная строка остаётся
  в raw и блокирует поколение; другой source продолжает работу.
- Неполный LF-tail переживает restart; rename сохраняет поколение, новый файл
  требует UUID; замена inode, prefix/tail mutation и truncate отвергаются.
- Отклоняются traversal/ADS/backslash/NUL/surrogate, hardlink, symlink и FIFO.
  Три POSIX-проверки на Windows пропускаются и выполняются в Linux CI.
- Worker требует настоящий ACK из ExecutionStore; после stop читает исторический
  binding; Low и Cowrie работают с одним sender. Ошибка provider не мешает
  восстановлению backlog и корректному async shutdown.
- Отдельный HTTP-стенд использует настоящий HttpBatchTransport и тестовый
  долговечный receiver: потеря ACK, restart, точное повторение wire bytes Event,
  один принятый ID и отсутствие новых POST после ACK. Новый batch_id после
  restart допустим существующим sender-контрактом. Этот стенд не выдаётся за
  backend PostgreSQL или живой Cowrie.

## Команды и ограничения

Python 3.12.13, зависимости корневого lock. Выполняются Ruff check/format,
mypy Linux/Win32, корневой pytest, export_contracts --check и uv build --wheel.
Точные итоги финального прогона публикуются в PR, а не подменяются ожидаемыми
числами. Данные и SQLite находятся в игнорируемых временных каталогах.

Итоговый полный локальный прогон: **1825 passed, 14 skipped, 37 subtests passed**.
Новый durable-набор содержит 60 тестов (57 выполняются на Windows, 3 POSIX
проверяются в Linux CI). Ruff check/format прошли по 126 файлам; mypy Linux и
Win32 — по 67 исходным файлам. export_contracts --check и wheel build прошли.
Изменений HTTP/OpenAPI, Event/ACK/replay schemas, зависимостей и миграций
относительно актуального main нет. Новый этап не меняет legacy TTY путь.

Первый полный Windows-прогон упёрся в MAX_PATH при создании существующего
64-byte имени legacy TTY. Использован короткий basetemp под корнем репозитория,
без изменения теста. Последующие общие прогоны выявляли отказы существующих
сетевых disconnect/timeout assertions. Их исходники не менялись. Целевой
контроль двух этих тестов на чистом main и PR #53 прошёл; первоначальные отказы
не считаются PASS, причина нестабильности не утверждается. Итоговый обязательный
CI должен выполнить все исходные проверки без ослабления.

Реальный Cowrie/container/SSH, runtime UUID registry/image digest, PostgreSQL
event/outbox и новая sealed/replay интеграция этим PR не доказаны. Provider —
внутренний доверенный стык, а не реализация экспорта от runtime. Неполный файл,
session.closed или EOF не считаются seal; этот этап TTY не читает. Необходимы
финальный CI и явное человеческое одобрение актуального PR; агент не сливает его.
