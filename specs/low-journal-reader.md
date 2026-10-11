# Читатель Low JSONL: внутренняя спецификация

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
