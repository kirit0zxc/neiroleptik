# Cowrie: результаты первого этапа, 15.10.2026

Ветка: `codex/cowrie-implementation`. Историческая база первого запуска:
`origin/main` `16791d4da8e658fcf7ef0143d3d80bca48db65c3`.
SHA итогового коммита и результаты трёх рецензентов фиксируются в PR, чтобы
отчёт не изменял уже проверенное дерево. Требуется человеческое одобрение
актуальной версии после CI; агент не выставляет Approve и не выполняет merge.

## Красный и зелёный набор

Исходный пакет готовился на `9c47c0a`: 4 passed, 89 errors из-за отсутствия
модулей. Это исторический результат подготовительного задания.
На обновлённом main collector уже существует, но новых интерфейсов нет:
`python -m pytest acceptance/cowrie -q --tb=no` → **4 passed, 5 failed,
84 errors**, exit 1. После реализации тот же исходный набор → **93 passed**.
Утверждения не ослаблялись. Оба файла перенесены в `tests/telemetry` и дополнены;
финальные `test_cowrie_adapter_contract.py` и `test_cowrie_tty_contract.py` →
**195 passed**. Pytest автоматически включает их в штатный CI.

Дополнения проверяют строгие UUID и границы offset, неизменяемость binding,
независимость идентичности от revision/порта, частичную null-пару source,
timestamp неизвестного типа, суррогаты/нечисловые значения/повторы в лишних
полях, безопасную цепочку исключений, точные byte/frame/payload budgets,
все частичные headers, повтор CLOSE, сохранение префикса при IO_ERROR и uint32
время. Сгенерированные Event/chunks проверяются существующими JSON Schema.

Первый цикл ревью выявил пересечение frame budget с неполным snapshot:
FRAME_LIMIT перекрывал обещанные INCOMPLETE_HEADER/INCOMPLETE_PAYLOAD.
Исправление проверяет полноту bounded кадра до frame guard; byte/payload
лимиты по-прежнему действуют до чтения/выделения памяти. Дополнительный кадр
не попадает в chunks/consumed. Регрессии покрывают все 23 частичных header,
частичный payload и отказ полного кадра на frame budget. Итоговая версия
проходит новый цикл двух независимых рецензентов и третьего аудитора.

## Локальные проверки

Python 3.12.13; pytest 9.1.1, Pydantic 2.14.0 из существующей корневой `.venv`.
Ruff и mypy также из этого окружения, соответствуют корневому lock.

| Команда | Фактический результат |
| --- | --- |
| `pytest tests -q --tb=short` | 1738 passed, 11 skipped, 37 subtests passed; до исправления приоритета frame budget; результат финального полного прогона фиксируется в PR |
| `pytest tests/telemetry/test_cowrie_adapter_contract.py tests/telemetry/test_cowrie_tty_contract.py -q` | 195 passed после исправления frame budget, включая OSError/ValueError/RuntimeError |
| `ruff check agent honeypots infra tests` | PASS |
| `ruff format --check agent honeypots infra tests` | PASS, 116 файлов |
| `mypy` и `mypy --platform win32` | PASS, 59 исходных файлов |
| `python -m agent.telemetry.export_contracts --check` | PASS; Event/ACK/replay schemas не изменились |
| `python -m agent.telemetry.demo` | PASS, синтетические 100 событий/ACK |
| `uv build --wheel --out-dir artifacts/cowrie-validation/dist` | PASS, wheel honeyforge_agent-0.1.0 |
| `git diff --check` | PASS |

Дополнительная проверка без БД: CowrieAdapter → Event → существующий backend
PublicPolicy для пустого и непустого пароля прошла. Публичный data содержит
username/success/password_present, исходный пароль остаётся только в private Event.
Это не проверка REST/WS/lookup или PostgreSQL.

Первоначальные sandbox-запуски полной регрессии получили отказ доступа к
временным каталогам/SQLite и были остановлены. Полный успешный запуск выполнен
с расширенным доступом, TEMP/TMP и pytest basetemp внутри игнорируемого
`artifacts/cowrie-validation/`. Первоначальная sandbox-сборка не разрешила DNS
PyPI; повторная сборка с доступом к hatchling завершилась успешно.
Артефакты и временные файлы не входят в PR.

## Совместимость и ограничения

Прежний CowrieCollector перенесён в отдельный файл **без изменения содержимого**,
публичный импорт из `adapters.cowrie` сохранён. Его UUID, редактирование пароля,
checkpoint и wiring runtime сохраняют прежнее поведение; тесты main включены
в полную регрессию. Новый mapper не подключён к этому demo collector, поэтому
два различных алгоритма UUID не смешиваются в одном источнике.
Low, Event/ACK, HTTP/OpenAPI, зависимости, миграции и PublicPolicy не менялись.
Новый mapper не читает JSON-пути, не обогащает source из соседних записей,
не утверждает наличие output или скачанного файла по command.input.

Одиннадцать skips — существующие платформенные POSIX/Linux/FIFO/UID/mount
проверки; Windows ими не проверяет Linux-доступ или изоляцию контейнера.
Backend/PostgreSQL, frontend/browser и новый сквозной Cowrie smoke локально
не запускались. Полные обязательные jobs должны проверяться в GitHub CI.
Pure TTY bytes созданы struct.pack по upstream `<iLiiLL>`; это не захват
живого Cowrie. Digest/версия установленного runtime и подтверждение Данила
не получены. Новый durable collector/worker, seal/digest storage, replay upload,
read API для новых чистых chunks и модель доверенного seal остаются следующими PR по
[cowrie-telemetry.md](cowrie-telemetry.md). Сообщение Данилу не отправлялось.

## Совместимость с main после Medium replay, 16.10.2026

Актуальная база: `6183092dc9234dd35f25c94834dc539b48f078ea`. Исторические SHA,
числа тестов и ограничения выше описывают предыдущий запуск, а не новую базу.
Конфликт cowrie.py разрешён сохранением pure mapper и переносом **актуального**
legacy collector из main в cowrie_collector.py. Он совпадает с main целиком,
включая profile port, TTY collector, возобновление и manifest. В main уже
существует legacy replay через Event и GET read API; они сохранены. Будущими
остаются durable/sealed wiring и transport новых чистых интерфейсов.

Независимые проверки переноса: 195 pure tests и 8 legacy tests прошли;
один POSIX FIFO skip на Windows. Сценарии service.port=2200/2323 сохранены.
Ruff/format, mypy Linux/Win32, export_contracts и wheel build прошли на новой базе.
Первый полный агентский прогон на базе `e61f6ef` (agent/tests не менялись
в последующих коммитах main): 1767 passed, 11 skipped, 37 subtests passed и один отказ
существующего test_executor_client slow-response-body (сервер не увидел закрытие
соединения за 3 секунды на Windows). Все Medium tests в нём прошли.
Целевой повтор дал тот же отказ и 5 passed Medium tests. Client, deadline и
этот тест не изменены относительно main; причина отказа пока не установлена.
Этот отказ не считается успешным тестом; результаты дальнейших проверок,
повторного цикла рецензентов и итоговый SHA — в PR. Обязательный CI на Linux
должен выполнить исходный тест без ослабления или пропуска.
Гипотеза влияния живого порта не подтверждена и не используется как доказательство.
