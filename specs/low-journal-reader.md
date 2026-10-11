# Читатель Low JSONL: внутренняя спецификация

## Независимая конверсия сохранённого raw

`low_journal/conversion.py` добавляет внедряемый LowRawConverter, не меняя reader,
collector, raw bytes/checkpoint или runtime. Ledger расположен в той же SQLite
spool; его схема, транзакционные границы и критерии описаны в specs/events.md.
Для повторного вычисления mapper получает неизменяемую JournalRecord и доверенную
JournalBinding; production Low→Event ещё не определён. None обозначает только
служебную строку, исключение не означает пропуск. Повтор после ACK/рестарта
сравнивает сохранённое доказательство и не создаёт pending заново.

Ответ runtime 11.10.2026 13:25 подтверждает: новый процесс прежнего instance
продолжает journal_seq, но previous_journal_id=null и исходный seq начинается
заново. Это не доверенное разрешение collector переходить через разрыв:
MISSING_PREDECESSOR/OPEN_PREDECESSOR сохраняются. Внутренняя конверсия разных
сохранённых journal_id при равных offset/seq не склеивает позиции и не сбрасывает
старый checkpoint. Протокол границы процесса, cleanup receipt и replay после
удаления predecessor ещё совместно определяются; см. low-telemetry-handoff.md.

Дата: 11.10.2026. База: `1a99647780b38dbb5ac925699ec4a8a2ad08f44d`.
Статус: внутренний контракт независимого читателя. Python 3.12.
Это внутренний интерфейс нашей телеметрии, не изменение runtime/Event/API.

## 1. Цель и граница

Прочитать явно указанный локальный файл `honeyforge-low-journal/1`, проверить
структуру и соответствие явно переданной привязке, выдать записи с точными
байтовыми позициями. Входной формат берётся из актуального
`specs/low-journal-v1.md`; его интеграционные вопросы не считаются согласованными
самим фактом появления читателя.

Не используются сеть, SQLite, Event, HTTP-парсер, credentials, PostgreSQL или UI.
Служебные строки выдаются как служебные строки; из них не создаются события.

## 2. Публичный Python-интерфейс

Модуль: `agent.telemetry.low_journal`.

```python
JournalBinding(deployment_id: str, instance_id: str, profile_revision: int)
LowJournalReader(*, max_line_bytes: int = 65536)
reader.open(path: Path, binding: JournalBinding) -> контекстный менеджер потока
```

Binding — неизменяемый объект; UUID — канонические строки, revision — строгий
integer >=1. Он задан вызывающим кодом, а не получен из имени файла/header.
Это проверка согласованности с конфигурацией, не доказательство авторизации узла.

Пример потребления:

```python
with reader.open(path, binding) as stream:
    for record in stream:
        consume(record)
    summary = stream.summary
```

Каждая неизменяемая `JournalRecord` содержит:

- `kind`: `header`, `event` или `footer`;
- `journal_id`: UUID поколения из проверенного header;
- `offset`, `next_offset`: абсолютные физические byte offsets в данном файле;
- `raw`: точные исходные bytes строки вместе с LF;
- `document()`: новая независимая JSON-копия записи при каждом вызове.

Поток одноразовый. Повторный проход — новый `open`. `summary` доступна только
после успешного исчерпания итератора; до него, после ошибки или досрочного выхода
обращение даёт `RuntimeError`. Контекст закрывает дескриптор при любом выходе.

`JournalSummary` содержит `status`, `next_offset`, `journal_id` (или None):

- `closed`: найден корректный footer и после него в снимке нет ни одного байта;
- `open`: header корректен, footer отсутствует; возможен неполный хвост;
- `incomplete`: пустой файл или незавершённый первый header; записей нет,
  `next_offset=0`, `journal_id=None`.

`next_offset` — конец последней выданной полной записи, не долговечный checkpoint
и не подтверждение её обработки потребителем. `closed` не означает fsync/seal,
ACK, доставку или безопасность удаления.

`JournalReadError(ValueError)` имеет безопасный стабильный `code` и `offset`
(int или None для открытия). Коды:
`INVALID_RECORD`, `UNSUPPORTED_SCHEMA`, `IDENTITY_MISMATCH`, `LINE_TOO_LONG`,
`INVALID_SEQUENCE`, `INVALID_STRUCTURE`, `UNSAFE_PATH`, `SOURCE_CHANGED`, `IO_ERROR`.
Неверные аргументы конструктора/Binding — обычный ValueError.
Диагностика не содержит raw, payload, содержимое HTTP, исходную строку исключения
JSON/Pydantic или секреты. Ожидаемые ошибки преобразуются без опасной цепочки
исключений; неожиданные программные ошибки не скрываются общим catch-all.

## 3. Файловое чтение

- Только binary read. Позиции считаются по bytes, включая LF, а не по символам
  или повторной JSON-сериализации.
- При входе в контекст фиксируется размер открытого обычного файла. Читается
  только этот префикс: дописывание не превращает один проход в бесконечный tail.
  Следующий `open` видит новые bytes.
- Чтение потоковое, память O(max_line_bytes), исключая данные, удержанные
  вызывающим кодом. Не использовать `read()` без размера/read_bytes всего файла.
  Для проверки лимита допустим буфер max_line_bytes+1. LF входит в лимит.
- Неполная строка без LF не выдаётся и не продвигает позицию. Как только её
  длина превысила лимит, LINE_TOO_LONG, даже если LF ещё нет.
- Полная некорректная строка — ошибка; её нельзя пропускать, заменять или
  автоматически переходить к следующей. Ранее выданный префикс остаётся префиксом,
  успешная summary не выдаётся. Это не транзакционный read-all.
- На чтении не создаются sidecar, checkpoint или логи с исходным содержимым.
  Файл не изменяется, не обрезается, не переименовывается и не удаляется.
- Открытый snapshot при преждевременном EOF/обнаруженном уменьшении файла
  завершается SOURCE_CHANGED. Новый проход читает с нуля: старые offsets не
  применяются автоматически к заменённому файлу.

Путь явно выбирает вызывающий код; имя файла не является идентичностью. Корректный
файл можно скопировать/переименовать и перечитать с прежним journal_id/offset.
Обход/поиск каталога не входит в этот этап.

Прямую symlink/reparse point и не-обычный файл отвергать до чтения содержимого.
Проверять открытый дескриптор, не полагаться лишь на exists/is_file перед open.
Использовать доступный платформенный no-follow механизм. Родительский каталог
и его предки являются явно доверенной, не заменяемой злоумышленником границей.
Если платформа не позволяет подтвердить безопасное открытие конечного компонента,
вернуть UNSAFE_PATH и указать ограничение; не объявлять lstat+open защитой от гонки.
Полную защиту от подмены родительского дерева/перезаписи открытого inode этим
читателем не обещать. POSIX-права и Docker-изоляция остаются отдельной проверкой.

Реализация POSIX использует `O_NOFOLLOW` и `O_NONBLOCK` с проверкой `fstat`;
без no-follow доступ отклоняется. Windows использует `CreateFileW` с
`FILE_FLAG_OPEN_REPARSE_POINT`, затем проверяет тип HANDLE и атрибуты
`FileAttributeTagInfo` до передачи HANDLE в read-only CRT-дескриптор.
Основание гарантий конечного компонента:
[CreateFileW](https://learn.microsoft.com/ru-ru/windows/win32/api/fileapi/nf-fileapi-createfilew)
и [GetFileInformationByHandleEx](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-getfileinformationbyhandleex).
Непроверяемые атрибуты дают UNSAFE_PATH. Открытие не использует предварительный
`lstat` как защиту; разрешены дописывание и удаление имени другим владельцем,
чтение продолжается по исходному открытому дескриптору.

## 4. Валидация исходных записей

Сохранять точный набор полей header/event/footer из исходной спецификации.
Отвергать неизвестные/отсутствующие поля, неверные типы, bool вместо int,
неизвестный record/schema, повторяющиеся JSON-ключи на любой глубине,
NaN/Infinity, не-объект JSON, неверную кодировку и CRLF/BOM.
Формат производителя ASCII JSON с экранированным Unicode; raw не переписывается.
Экранированный Unicode разрешён, неэкранированные non-ASCII bytes — ошибка формата.

Защитный предел вложенности JSON — 32 уровня объектов/массивов. Более глубокая
полная строка получает INVALID_RECORD до вызова JSON-парсера; это внутренний
предел читателя, не расширение Low или Event. Strict base64 включает проверку
канонического padding и нулевых pad bits через повторное кодирование.

- UUID и timestamps проверяются по исходному формату; время RFC3339 UTC с `Z`,
  существующая дата, без молчаливого усечения точности сверх микросекунд.
- Header первый и единственный; schema точная; binding должен совпасть по всем
  трём полям. `journal_seq`, `first_seq`, revision >=1. Первый journal_seq=1
  имеет previous_journal_id=null. Для более позднего файла null не превращать
  автоматически в ошибку: после нового процесса sink может не знать predecessor.
- Event: `connection|request`, `tcp|http`; service.protocol совпадает с protocol,
  service.port 1..65535; seq — строгий int >=1; connection_id — UUID.
  peer — null либо объект host/port: host — IP-строка, port 0..65535, строгие типы.
  null не заменяется на 0.0.0.0 и не отклоняется из-за требований будущего Event.
- Base64 проверяется строго, включая неалфавитные символы/неверный padding.
  Для connection payload пустой. Байты request не обязаны быть UTF-8 или HTTP.
  Лимит Event.data=8192 здесь не применяется. max_line_bytes — независимый
  предел читателя; слишком большие standalone-журналы могут быть явно отклонены.
- `seq` строго возрастает, но пропуски допустимы: номера выделяются до записи,
  ошибка записи/квота могла оставить разрыв. Первый seq >= first_seq.
  Дубликат или уменьшение — INVALID_SEQUENCE.
- Времена не определяют порядок. Событие вправе быть раньше opened_at; часы
  могут идти назад. Не требовать монотонного timestamp/closed_at.
- Request может быть первой записью файла, а connection находиться в предыдущем.
  Не требовать локальной пары connection/request и не назначать session_id.
- Footer один, с тем же journal_id; last_seq равен последнему фактически
  прочитанному seq, либо first_seq-1 у пустого журнала; reason rotation|shutdown.
  Любые bytes после footer, даже неполная строка, — INVALID_STRUCTURE.

Структурные ошибки: нет header перед полной event/footer, второй header/footer,
данные после footer. Несовпадение footer journal_id — INVALID_STRUCTURE;
неправильный last_seq — INVALID_SEQUENCE. Ошибки формы — INVALID_RECORD.

## 5. Ротация и перезапуск: ограничение первого этапа

Читатель открывает каждый явно переданный файл независимо; физические offsets
каждого начинаются с нуля. ID поколения не выводится из имени. Интеграционные
тесты читают все файлы настоящей ротации и проверяют связи previous_journal_id,
порядок journal_seq и сохранение connection_id. Автоматического восстановления
цепи, перехода через отсутствующий файл или незавершённый predecessor здесь нет.

Повторный объект читателя выдаёт те же raw/positions при неизменном источнике.
Это безопасный повтор чтения, но потребитель сам отвечает за последующее
атомарное сохранение результата. Служебные строки пока только отдаются наружу;
проблема их checkpoint в SpoolRepository этим этапом не решается.

Фрагменты `specs/samples/low-executor-real/` нельзя считать целыми журналами:
footer может относиться к пропущенным строкам, а offsets выборки отличаются от
исходных. Не добавлять lax-режим ради успешного чтения таких фрагментов.

## 6. Приёмка и оставшиеся решения

Критерии приёмки и исполняемые проверки хранятся вместе с реализацией:

| Проверка | Критерий | Тесты |
| --- | --- | --- |
| Физические записи | Точные raw/offsets, независимый document, повтор после переименования, полный sample без изменения источника | [contract](../tests/telemetry/test_low_journal_reader_contract.py) |
| Снимок и ресурсы | Пустой файл/неполный header/хвост, дописывание после открытия, граница 65536 bytes, ограниченные чтение и память, закрытие дескриптора и недоступная summary при ошибке или раннем выходе | [contract](../tests/telemetry/test_low_journal_reader_contract.py), [files](../tests/telemetry/test_low_journal_reader_files.py) |
| Ошибки источника | Уменьшение и ранний EOF дают SOURCE_CHANGED; open/read/stat дают безопасную IO_ERROR; источник не меняется | [files](../tests/telemetry/test_low_journal_reader_files.py) |
| Безопасное открытие | Конечная ссылка/reparse point, подмена перед open и не-обычный файл отвергаются до чтения; POSIX FIFO не блокирует открытие | [files](../tests/telemetry/test_low_journal_reader_files.py) |
| Форма и диагностика | Обязательные и лишние поля, строгие типы, UUID, timestamp, IP/port, base64, ASCII/LF, duplicate keys, NaN/Infinity и глубина; секретные bytes не попадают в диагностику | [contract](../tests/telemetry/test_low_journal_reader_contract.py), [validation](../tests/telemetry/test_low_journal_reader_validation.py), [files](../tests/telemetry/test_low_journal_reader_files.py) |
| Порядок и привязка | Все поля binding, first_seq, gaps/duplicate/rollback, footer ID/last_seq и любые bytes после footer; обратное время и request без локального connection допустимы | [contract](../tests/telemetry/test_low_journal_reader_contract.py), [rotation](../tests/telemetry/test_low_journal_reader_rotation.py) |
| Настоящий Low | Полный процесс с ротацией на loopback, цепь journal_id/previous_journal_id, seq и connection_id; принудительный перенос connection/request, перезапуск sink; выборки executor не принимаются за полные файлы | [rotation](../tests/telemetry/test_low_journal_reader_rotation.py) |

Готовность требует прохождения этой матрицы либо явного указания ограничений
среды. Невыполненная платформенная проверка не считается успешной. Для Windows
проверяются reparse points и HANDLE, для POSIX — no-follow и FIFO. Ошибка запуска
или штатной остановки процесса Low является ошибкой smoke, а не skip/xfail.
Исходные файлы остаются неизменными; нет импорта Event/spool/backend или новых
сетевых форматов. Спецификация/архитектура и инструкция использования входят
в diff реализации.

Проверки из корня checkout с Python 3.12 и зависимостями `uv.lock`:

```text
uv sync --frozen
uv run --frozen pytest
uv run --frozen ruff check agent honeypots infra tests
uv run --frozen ruff format --check agent honeypots infra tests
uv run --frozen mypy
uv run --frozen mypy --platform linux
uv run --frozen mypy --platform win32
uv run --frozen python -m agent.telemetry.export_contracts --check
uv build --wheel
git diff --check
```

Дополнительно проверить импорт `agent.telemetry.low_journal` из установленного
wheel вне checkout. Проверки типов для обеих платформ не заменяют исполнение
платформенных тестов; Linux CI дополнительно проверяет типы Win32-ветки.
PostgreSQL, Docker, Event mapping, durable checkpoint и UI
не входят в приёмку независимого читателя и не считаются проверенными этой матрицей.

Следующий этап требует решений об event_id/source_seq/session_id, TCP type,
HTTP mapping/маскировании, null peer в Event, durable checkpoint служебных строк,
авторизации исторических instances, ingest/миграциях и правах сборщика.
Ни один из этих пунктов не считается закрытым тестами файлового читателя.

## 7. Долговечный независимый сборщик (следующий этап)

Mapping нового Low в Event остаётся несогласованным. `durable.py` хранит исходные
проверенные строки в отдельных `low_raw_records` и `low_raw_generations` той же
SQLite-базы; `pending`, ACK, quarantine, snapshots и старые checkpoints не меняются.
Дополнительная копия необходима: checkpoint без bytes не восстанавливает служебные
строки и не обнаруживает подмену уже прочитанного префикса. Автоочистки нет:
сырьё хранится до отдельного согласования преобразования, retention и cleanup.
Закрытие footer и ACK Event не разрешают удаление сырья или исходных файлов.

Внутренний ключ источника — канонический JSON-массив
`[agent_id, deployment_id, instance_id, profile_revision]` из доверенной конфигурации.
Ключ поколения добавляет проверенный journal_id, ключ строки — byte offset.
Это не публичные event_id/source_seq. Путь служит только для открытия файла.

`RawJournalRepository(path, agent_id, max_raw_bytes=...)` открывает WAL/FULL,
проверяет владельца существующей spool и добавляет только собственные таблицы.
`append(binding, record) -> bool`: True при новой строке, False при точном повторе.
INSERT BLOB и обновление checkpoint — одна BEGIN IMMEDIATE транзакция. Повтор
сравнивает точные bytes/границы/kind, даже после будущего ACK/удаления Event.
Иное содержимое, пропуск смещения и усечение дают безопасный конфликт.
Служебные строки сохраняются полностью и не становятся Event. SQLite сериализует
двух потребителей; повтор после commit возвращает False. Ошибка до commit откатывает
всю строку и позицию. Ошибка SQLite выдаётся как DB_ERROR без исходного исключения.

`LowJournalCollector(repository, binding).step(paths, limits) -> CollectionResult`
получает конечный явно заданный список путей одного источника. Не сканирует каталог
и не выводит binding из header. Сначала безопасно читает headers, проверяет единую
цепь по journal_seq/previous_journal_id (не именам), исключает развилки и циклы.
Начало с journal_seq >1/неизвестного predecessor — ожидание MISSING_PREDECESSOR.
Поздний null predecessor требует ручного согласования границы процесса и не
считается разрешением продолжить. Следующий файл допустим только после успешного
исчерпания predecessor с footer и проверки first_seq > предыдущего last_seq.
Незавершённый predecessor — OPEN_PREDECESSOR, исходные connection_id сохраняются.
Пропуск внутри цепочки запрещён. Независимые источники вызываются отдельно:
ошибка одного результата не мешает следующему источнику.

Восстановление всегда читает с нуля безопасным reader и сверяет все сохранённые
строки побайтно до checkpoint; seek отсутствует. Стоимость O(сумма проверенных
префиксов) на шаг, память O(max_files * max_line_bytes + max_line_bytes).
Лимиты задают число путей, новых строк, новых bytes и проверяемых bytes на шаг;
последние включают повторный проход и обнаружение headers. REPLAY_LIMIT означает,
что бюджет проверки префикса недостаточен: требуется увеличить его явно. Такая
пауза предпочтительнее обхода проверки; для больших журналов потребуется отдельный
контракт индексированного восстановления. Неполная строка не продвигает checkpoint.
Дописывание после снимка видит следующий шаг; дескрипторы закрываются при выходе.

Результат содержит state (progress/waiting/limited/error), saved_records,
saved_bytes, code и retry_in >=1 для состояний без продвижения. Шаг не запускает
бесконечный цикл; вызывающий ждёт retry_in и может остановить сборщик между шагами.
Квота max_raw_bytes ограничивает суммарные BLOB, проверяется под writer-lock.
Это логическая квота сырья, не строгий предел размера SQLite/WAL/индексов; для
физического ограничения нужна квота файловой системы. Переполнение не удаляет
данные, ошибка диска/lock не выдаётся за успешное чтение.

`RawEventMapper` задаёт будущий интерфейс `convert(binding, record) -> Event | None`.
Production реализация и автоматическое продвижение в Event отсутствуют. Будущая
конверсия обязана иметь собственный долговечный ledger, атомарный с pending, чтобы
ACK и удаление pending не разрешали повторную постановку. Старый SpoolRepository
уже отвергает повтор по сохранённому checkpoint после ACK; его append не меняется.

Приёмка: точные bytes/позиции header/event/footer, rollback в двух точках,
аварийный выход отдельного процесса после commit, повтор/конфликт/конкуренция,
совместимость старой spool, неполный хвост, цепь/развилка/цикл/пропуски, rename,
новый journal по прежнему пути, усечение/подмена, quota/lock и безопасная диагностика.
Настоящий процесс Low проверяется отдельно от Event ↔ тестовый HTTP со своей
долговечной дедупликацией. PostgreSQL и Low → UI этим этапом не проверяются.

### Использование сборщика и пределы

```python
from pathlib import Path
from agent.telemetry.low_journal import JournalBinding
from agent.telemetry.low_journal.durable import RawJournalRepository
from agent.telemetry.low_journal.collector import CollectionLimits, LowJournalCollector

# Значения привязки предоставляет доверенная конфигурация агента.
repository = RawJournalRepository(Path("spool.sqlite3"), agent_id)
try:
    collector = LowJournalCollector(repository, trusted_binding)
    result = collector.step(explicit_paths, CollectionLimits())
    # Вызывающий планировщик соблюдает result.retry_in, затем обновляет список путей.
finally:
    repository.close()
```

По умолчанию: 64 пути, 1000 новых строк, 4 МиБ новых bytes, 64 МиБ физических
bytes чтения, 65536 bytes в строке. `JournalStream.bytes_read` учитывает read-ahead
и неполный хвост, не только выданные строки. Перед чтением резервируется
max_line_bytes+1; маленький оставшийся бюджет может остановить шаг досрочно.
Дисковые чтения и повторная проверка входят в max_scan_bytes. Список должен
содержать начало и всех predecessor; архивный закрытый файл нужен для повторной
проверки, сохранённая копия в SQLite не разрешает молча обходить отсутствующий файл.
Перечисление каталога и доверенный runtime lifecycle остаются ответственностью
вызывающего кода, список путей не является manifest или seal.

`low_raw_usage` хранит транзакционный счётчик bytes: quota не требует полного
SUM таблицы в каждом шаге. Уникальные индексы поколений не допускают другого
journal_id с тем же journal_seq или второй successor даже между разными шагами.
`low_raw_locations` — диагностическая подсказка имени, записанная атомарно с
header: пустой/усечённый header по известному имени даёт SOURCE_TRUNCATED.
Полный header нового поколения по тому же имени разрешён при корректной цепи.
Пустой файл под новым неизвестным именем нельзя идентифицировать как прежнее
поколение: он остаётся INCOMPLETE_HEADER, без сброса сохранённых позиций.
IO_ERROR означает ожидание доступности источника без продвижения; не доказывает
отличие отсутствующего файла от отказа доступа. Повреждение структуры имеет
отдельный код error. STORAGE_LIMIT/STEP_LIMIT/FILE_LIMIT/REPLAY_LIMIT требуют
изменить доступный ресурс или бюджет; автоматического удаления нет.

Тесты процесса используют управляемый os._exit до/после commit, включая запись
с уже сохранённым header. Они доказывают восстановление после аварийного выхода
процесса, но не физическое отключение питания или исправность диска.

Номер journal_seq в таблице поколений хранится как каноническая десятичная TEXT:
исходный Low допускает целые больше SQLite INTEGER. Числовой порядок проверяется
сборщиком по проверенному JSON, а уникальность — по точному десятичному значению.
Это внутреннее хранение, не ограничение и не изменение формата Low.
