# Cowrie: контракт работ участника 4

Обновлено: 16.10.2026. Первоначальное основание: `9c47c0a`; первый PR проверялся
с `16791d4`. Совместимость обновлена относительно удалённого `origin/main`
`6183092dc9234dd35f25c94834dc539b48f078ea`.
Статус: чистые CowrieAdapter/CowrieBinding и CowrieTtyConverter реализованы;
acceptance-тесты перенесены в штатный набор. Ожидание ответа Данила не блокирует
подготовку нашей части. Это не заявление о согласии Данила, готовности нового
durable collector или выполненной интеграции новых чистых интерфейсов.
Публичные Event/ACK и Low не изменяются. Новый HTTP transport replay данным
этапом не вводится. Актуальный main уже содержит legacy replay через Event и
GET read API; он сохраняется без изменений. Следующие этапы ниже относятся к
новому durable/sealed пути, а не отрицают существующий legacy replay.

В обновлённом main уже существует Medium runtime/collector с legacy replay
([medium-cowrie-executor.md](medium-cowrie-executor.md)). Его поведение и UUID
сохранены: collector вынесен в `adapters/cowrie_collector.py` с реэкспортом по
старому импорту. Новые чистые интерфейсы пока не подключены к worker. Успех
старого smoke не считается проверкой нового mapper, seal или TTY replay.

## Разделение ответственности

Данил реализует запуск/остановку Cowrie, фиксирует image digest и версию,
включает JSONL и бинарный ttylog, сохраняет журналы после stop/reset и экспортирует
доверенные bindings из успешно подтверждённых start. Новый instance получает
новую директорию; старая не переиспользуется. Запись временно отсутствующего
файла не считается законченной. Нельзя выполнять cleanup до отдельной политики.

Участник 4 реализует адаптер JSONL, ограниченный TTY parser, долговечные raw,
checkpoint и conversion ledger, включение в существующий worker/spool/sender,
серверное хранение replay и read API. Интерфейс воспроизведения делает frontend.
Нельзя заменить production приёмку демонстрационной SQLite или mock ACK.

## Вход от runtime

Никакие UUID, пути и признаки завершения из атакующего JSON не являются binding.
Доверенный внутренний объект содержит agent_id, deployment_id, instance_id
(UUID), profile_revision (int >=1), service_port (int 1..65535), journal_id
(UUID поколения JSONL), read-only root и относительное имя JSONL. Регистрация
binding разрешена только после успешного ACK start; исторические bindings
остаются действительными для дочитывания. Это требование к будущему wiring,
не новая реализация авторизации.

JSONL: UTF-8, один объект на LF; ротация rename сохраняет journal_id, новый
файл получает новый ID из доверенного реестра. Copytruncate, замена bytes ранее
сохранённой позиции и повторное использование journal_id блокируют источник.
Неполный хвост ждёт LF без продвижения checkpoint. Сначала сохраняются raw и
физическая позиция, затем Event+ledger одной транзакцией существующего spool.
Неизвестный валидный тип учитывается как ignored в ledger, raw сохраняется.

TTY binding дополнительно содержит recording_id UUID, исходный Cowrie session,
относительный путь, доверенное завершение `sealed` и итоговые size/SHA-256.
Несколько каналов одной SSH-сессии могут иметь разные recording_id: нельзя
сливать их файлы по одному IP или session. До модели нескольких записей API
не должен молча выбирать произвольный канал. Пути открываются внутри read-only
root с проверкой подмены/symlink; ttylog path из JSON сам по себе не разрешает чтение.
Seal — подтверждённый runtime snapshot, а не EOF/OP_CLOSE/session.closed.
До seal разрешено читать законченный префикс, повторно проверяя bytes.

## Чистый интерфейс JSONL адаптера — первый этап

Модуль `agent.telemetry.adapters.cowrie`:

```python
CowrieBinding(agent_id: UUID, deployment_id: UUID, instance_id: UUID,
              profile_revision: int, service_port: int)
CowrieAdapter().map_record(raw: bytes, *, binding: CowrieBinding,
                          journal_id: UUID, offset: int) -> Event | None
CowrieMappingError.code: str
```

Binding неизменяемый, проверяет UUID и строгие int (bool запрещён), revision>=1.
Offset — строгий int 0..9007199254740991. Raw ограничен 64 KiB вместе с LF;
ровно одна LF в конце, без других LF. Нужны JSON object, уникальные ключи на
любой глубине, строгий UTF-8, без NaN/Infinity и суррогатов. Лишние поля Cowrie
сохраняются в raw, но не копируются в Event.data.

Общие поля: непустой eventid и session (строки <=128 UTF-8 bytes), timestamp
в RFC3339 по существующему Event (UTC, точность до 6 знаков). Сначала проверяется
общая форма, затем неизвестный eventid даёт None. Даже неизвестный тип с неверным
timestamp/session не считается обработанным. Для MVP runtime включает UTC Z.
Формат timezone `+HHMM` требует отдельного явного расширения parser и тестов.

Event UUID = UUIDv5(NAMESPACE_URL, compact_json([
agent_id, deployment_id, instance_id, "cowrie-jsonl/1", journal_id, offset])).
Session UUID = UUIDv5(NAMESPACE_URL, compact_json([
agent_id, deployment_id, instance_id, "cowrie-session/1", source_session])).
UUID в массивах — lowercase canonical string; offset — JSON integer;
compact_json — ensure_ascii=False, sort_keys=True, separators=(",",":"),
allow_nan=False. Ни имя файла, ни revision, ни IP не определяют идентичность.
Изменение исходных bytes той же позиции — конфликт ledger, не новый Event.
source_seq=offset; service={protocol:"ssh",port:binding.service_port};
artifact_ids=[]; schema_version=1. Свои UUID из JSON игнорируются.

source строится только из пары src_ip/src_port данной записи. Оба отсутствуют
или оба null → null. Частичная пара, неверный IP, bool/строка вместо порта → ошибка.
Порт источника допускает 0..65535. IP нормализуется существующим Source.
Без stateful обогащения нельзя угадывать адрес по предыдущему событию.

| eventid Cowrie | Event.type | Event.data |
| --- | --- | --- |
| cowrie.session.connect | connection.opened | {} |
| cowrie.login.failed | auth.attempt | username, password, success=false |
| cowrie.login.success | auth.attempt | username, password, success=true |
| cowrie.command.input | session.command | input |
| cowrie.session.closed | session.closed | {} |
| другой валидный тип | None | только raw/ignored ledger |

username/password/input — обязательные строки; пустая строка допустима.
data ограничен существующими 8192 UTF-8 bytes: превышение DATA_LIMIT,
без молчаливого усечения или потери исходника. Оригинальный password допустим
только в закрытом Event; существующая PublicPolicy выдаёт password_present.
Новый pure TTY parser не превращает запись в session.output: отдельный transport
для новых интерфейсов ещё не реализован. Legacy collector main продолжает
передавать свой replay через session.output по medium-cowrie-executor.md.
`cowrie.session.file_download` не подтверждает наличие скачанного артефакта у нас.

Ошибки: INVALID_RECORD, INVALID_POSITION, RECORD_LIMIT, DATA_LIMIT. Сообщение,
repr и traceback не должны включать raw/password/input/path/token. Ошибка чистого
mapper не меняет хранилище. Известная полная неверная строка блокирует данный
источник; не блокирует другие bindings и отправку прежнего pending.

## TTY parser — первый этап

Используем наблюдаемый upstream формат `cowrie.core.ttylog`, header `<iLiiLL`
(24 bytes, little-endian): op, tty, length, direction, seconds, microseconds.
Источник: [ttylog.py](https://github.com/cowrie/cowrie/blob/main/src/cowrie/core/ttylog.py).
JSONL источник: [jsonlog.py](https://github.com/cowrie/cowrie/blob/main/src/cowrie/output/jsonlog.py).
Эти ссылки на main — основание формата, не фиксация установленной версии.
Данил обязан записать фактический digest/версию; настоящий образ проверяется
отдельным smoke. Тестовые bytes строятся по этому формату, не выдаются за захват.

Модуль `agent.telemetry.cowrie_tty`:

```python
CowrieTtyConverter().convert(stream: BinaryIO, *, sealed: bool = False,
                             limits: CowrieTtyLimits | None = None) -> CowrieTtyResult
CowrieTtyLimits(max_bytes=1048576, max_frames=10000, max_payload=65536)
# result: chunks (tuple[dict]), started_at (UTC datetime|None),
# complete (bool), code (str|None), consumed_bytes (int)
```

Лимиты — положительные строгие int. Короткие read допустимы; каждый read <=4096,
нет read(-1). Бюджет bytes допускает один lookahead byte для различения точного
EOF от превышения; он не входит в consumed_bytes. Ошибочная/неполная запись
также не входит в consumed_bytes: это конец последнего полностью принятого frame.
max_frames считает все принятые headers, включая OPEN/CLOSE, не только chunks.
max_payload — предел одной WRITE до allocation. Parser не владеет потоком и не
закрывает его. Перезапуск parsing того же snapshot даёт те же chunks.

Первая запись — OPEN=1; tty=0, length=direction=0. WRITE=3 допускает direction
1(in)/2(out), length>=0. CLOSE=2 имеет нулевые length/direction. microseconds
0..999999; время не убывает, равно допустимо. Новый OPEN, WRITE после CLOSE,
OP_EXEC=4/неизвестный op, TYPE_INTERACT=3 или tty!=0 не интерпретируются молча.
started_at берётся из OPEN; offsets вычисляются целочисленно floor((us-start)/1000).
Каждый WRITE, включая пустой, даёт chunk с seq=0..N-1, offset_ms, direction,
исходными data_base64 и truncated=false по существующей replay-chunk схеме.
Бинарные bytes и UTF-8, разделённый между frames, сохраняются буквально.

complete=true только при sealed=true, единственном корректном CLOSE и точном EOF.
Один OPEN+CLOSE — полная пустая запись. OPEN и чистый EOF без seal:
RECORDING_ACTIVE; CLOSE без seal: AWAITING_SEAL. EOF без CLOSE при seal:
MISSING_CLOSE. Неполный header/payload всегда INCOMPLETE_HEADER/INCOMPLETE_PAYLOAD.
Сохранённые chunks возвращаются и при ошибке; ошибка не добавляет ложный chunk
или фиктивное truncated=true. Результат complete=false, code обязателен.
Другие коды: EMPTY_INPUT, INVALID_HEADER, INVALID_TIME, REVERSED_TIME,
UNKNOWN_OPERATION, UNKNOWN_DIRECTION, INVALID_SEQUENCE, BYTES_LIMIT, FRAME_LIMIT,
PAYLOAD_LIMIT, IO_ERROR. Код не содержит payload или текста внешнего exception.

## Последующие этапы и приёмка взаимодействия

1. Подключить durable raw/ledger и worker без второго sender; повтор после ACK
   не создаёт pending; crash между raw и conversion восстанавливается; ACK loss
   повторяет ID/bytes. Один испорченный источник не задерживает остальные.
2. Сохранить replay по recording_id/seq с конфликтом при иных bytes; повтор seal
   идемпотентен, неверный digest не означает complete. Не добавлять bytes TTY
   в Event.data: уже существует предел 8192 и отдельный replay DTO.
3. Перед введением upload/read replay дополнить этот документ точным transport,
   общим OpenAPI, моделью нескольких каналов, ownership, quotas и миграцией.
   Пока не выдумывать существующий агентский endpoint или разрешение viewer.
   Существующий раздел Replay frontend-protocol-v1 описывает legacy read API;
   он не определяет upload новых чистых chunks и доверенного seal.
4. Проверить PostgreSQL event/outbox/publication и REST/WS/lookup: пароль скрыт
   в каждом пути; origin/cookie/CSRF и Bearer ownership сохранены. Для replay
   отдельно определить и проверить роли до открытия исходных bytes пользователю.
5. UI использует отдельный incremental UTF-8 decoder по направлению, отображает
   только текст, не исполняет ESC/OSC/HTML/URL; split escape тоже безопасен.
   Timeline seek использует время OPEN, отрицательные смещения явно обозначаются.
6. Настоящий Cowrie SSH smoke: failed/success login, PTY, exec, две сессии одного
   IP, несколько каналов, Unicode, disconnect, rotation, stop/reset/restart.
   JSONL/session/TTY коррелируют; выход команды не угадывается из command.input.

## Запуск приёмочного набора

Набор `acceptance/cowrie/` сначала запускался красным: на обновлённой базе
4 passed, 5 failed, 84 errors (отсутствовали новые интерфейсы, не collector).
После реализации все исходные 93 теста прошли без ослабления утверждений.
Тесты перенесены в штатный testpaths=["tests"] и дополнены проверками границ,
цепочек исключений, UTF-8, коротких чтений и сохранения принятого префикса:

```sh
python -m pytest tests/telemetry/test_cowrie_adapter_contract.py tests/telemetry/test_cowrie_tty_contract.py -q
```

Event/replay schemas проверяются также на фактическом результате конверторов.
Матрица нового durable/backend/UI выше этим набором не покрыта и не объявляется
проверенной. TTY bytes синтезируются struct.pack по wire-контракту, это не capture
живого Cowrie. Результаты и ограничения: [cowrie-validation.md](cowrie-validation.md).
