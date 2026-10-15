# Low → Event v1: handoff Диме

## Граница процесса: дополнение Данила, 14.10.2026

Для PR #39 head 20f19d2ce3c1aae6ec79205a971a6d358260dfb1 подготовлены
[формат runtime proof и совместный restart-сценарий](journal-process-boundary-proposal.md).
Формат остаётся на согласование. Обновление 15.10.2026: сторона runtime
реализована (producer, startup-выделение Low CLI, registry, снимки и локальная
публикация write-once), сторона collector — нет. Совместный прогон выполняется
стендом `python -m infra.runtime.boundary_stand` и ещё не проводился.
Старый writer закрывается отдельно от instance; новая жизнь в том же instance
требует другого writer_id, ограждения старого ресурса, защищённого crash snapshot
и доверенной привязки первого нового journal. До этого collector остаётся в
waiting, а ранее проверенный raw продолжает conversion/delivery.
Mapping/DTO/UUID разделов ниже этим дополнением не меняются.
Ответы runtime на вопросы consumer proposal (формат и источник anchor, канал/права,
snapshots, восстановление producer, лимиты 1 MiB / 64 / 4 MiB) — в §8 того же
документа; это тоже предложение на согласование.

## Согласование MVP, 13.10.2026

Кирито подтвердил направление MVP; ответ передан пользователем в чате 13.10.
Добавляем `tcp.request` во входные и публичные схемы, серверную валидацию,
OpenAPI, фронтовые типы, фильтры, отображение и тесты в одном PR.
Точные UUID/кодирование ниже приняты без переименования сохранённых событий.
`source_seq` — byte offset начала строки конкретного journal; исходный процессный
`seq` сохраняется отдельно как **закрытое `data.low_seq` (десятичная строка)**,
чтобы не терять точность больших значений. Только `stream_seq` задаёт порядок
публикации. Session ID не зависит от ротации и совпадения IP.

Публичный request DTO разрешает `captured_bytes` (целое 0..9007199254740991),
`mapper_truncated` (boolean), `capture_completeness="unknown"`.
HTTP дополнительно разрешает проверенный `method`, `http_parse_state`
(`parsed|incomplete|invalid|limited`) и `path`: `/` только для точного `/`,
иначе `[скрыто]`. Отсутствующее поле остаётся отсутствующим; неизвестный method
не угадывается. Ни seq процесса, ни http_version, ни raw/base64, query, headers,
body, target или произвольные поля не публичны. Сервер применяет эту проекцию
для REST, WS, lookup и любого экспорта Event. IoC export не экспортирует payload.
Пример HTTP data: `{"captured_bytes":37,"mapper_truncated":false,
"capture_completeness":"unknown","method":"GET","path":"[скрыто]",
"http_parse_state":"parsed"}`. Пример TCP data: `{"captured_bytes":3,
"mapper_truncated":false,"capture_completeness":"unknown"}`.
Это итоговый DTO для передачи Диане; отправка участникам здесь не выполняется.

Footer не доказывает полноту. UI показывает «полнота захвата неизвестна» и для
HTTP «ответ не записан»; request не доказывает HTTP 200. Поздний null predecessor
блокирует новую цепочку с видимой причиной, сохраняя данные. Конверсия ранее
сохранённых проверенных строк продолжается. Это не запрет приёма запоздалых
событий известных завершённых instances. Механизм доверенной границы процесса
остаётся отдельным согласованием с Данилом; автоматический cleanup запрещён.

Критерии приёмки: повтор/rename дают прежний UUID; другая позиция — другой;
ротация сохраняет session; разные connections одного IP имеют разные sessions;
TCP принимается и фильтруется во всех компонентах; HTTP method не угадывается;
сокращение base64 учитывает точные 8192 UTF-8 bytes data; секреты одинаково
скрыты во всех публичных путях; replay после ACK не создаёт pending; поздний
null не удаляет raw и не блокирует конверсию уже проверенного raw.
Сквозная серверная приёмка остаётся обязательной. Локальные исполняемые проверки
по прежнему указанию пользователя запрещены; согласование не означает их успеха.

Следующий раздел уточняет реализацию принятого направления; исторические
разделы ниже отражают состояние на 11.10 и не отменяют это согласование.

## Production mapping: техническая спецификация

Этот раздел сверяет исторический handoff с `origin/main`
`01f8272ba37ef16af7b905042b00a7a1d36808ca` (13.10). PR35 уже подключил
`POST /agent/v1/events`, production authorization, PostgreSQL event/outbox,
publisher, REST/WS и единую `PublicPolicy`; PR36 добавил экран расследования.
Утверждения ниже по тексту о неподключённых маршрутах относятся к прежнему этапу.
В этом изменении добавлены LowProductionMapper, CLI collect_low и tcp.request
в общих схемах/OpenAPI/UI. Исполняемая серверная совместимость ещё не проверена.

### Вход, доверие и идентичность

Вход — точный `honeyforge-low-journal/1` из [low-journal-v1.md](low-journal-v1.md),
строго проверяемый `RecordValidator`/`LowJournalReader`: первая header, события,
опциональная последняя footer. Все поля таблиц раздела 3 обязательны,
лишние/повторные ключи и неизвестные виды отвергаются. stdout не является входом.
`agent_id` задаёт конфигурация агента, deployment/instance/revision — доверенный
`JournalBinding` от runtime/control; header только сверяется с этой привязкой.
`peer=null` становится `source=null`, отсутствие peer или неверный endpoint — ошибка.
Адрес нормализуется через `ipaddress`, неизвестный адрес не получает фиктивного IP.
Неполная LF-строка ждёт дописывания без checkpoint; повреждённая полная строка
останавливает источник с безопасным кодом и offset без payload в диагностике.

Event ID сохраняет алгоритм прежнего `LowAdapter`:
`uuid5(NAMESPACE_URL, compact_json([agent_id,deployment_id,instance_id,journal_id,offset]))`.
UUID в массиве — канонические lowercase строки; offset — JSON integer начала строки;
JSON UTF-8, ensure_ascii=False, sort_keys=True, separators=(",",":"), allow_nan=False.
NAMESPACE_URL = `6ba7b811-9dad-11d1-80b4-00c04fd430c8`.
Путь, timestamp, revision, seq и версия mapper не входят в ID. Изменение bytes
той же позиции является конфликтом, а не новым Event. `source_seq=offset`
в пределах 0..9007199254740991; `stream_seq` назначает только сервер.
Session ID:
`uuid5(NAMESPACE_URL, compact_json([agent_id,deployment_id,instance_id,"low",connection_id]))`.
Он сохраняется при ротации, не связывает разные соединения одного IP и не зависит
от наличия connection в текущем файле. Для mapper задаётся версия `low-journal-v1/1`.
Смена версии не разрешает повторно отправить позицию: нужен отдельный план миграции
ledger; явный повтор с другим version/result должен дать `RESULT_CONFLICT`.

Контрольные UUID (NAMESPACE_URL, UTF-8 компактного массива без пробелов):
agent=`00000000-0000-4000-8000-000000000000`,
deployment=`00000000-0000-4000-8000-000000000001`,
instance=`00000000-0000-4000-8000-000000000002`,
journal=`00000000-0000-4000-8000-000000000003`,
connection=`00000000-0000-4000-8000-000000000004`.

| Вход | UUIDv5 |
|---|---|
| Event, offset=512 | `1024f648-cd77-5b90-8834-6be84c3cfa50` |
| Event, offset=513 | `8954694d-3908-5db0-8b3f-effbc2e1080a` |
| Session, указанное connection | `d2756db2-741d-5edb-b9f6-b4b347ea754b` |

Векторы вычислены отдельно стандартным UUIDv5, без запуска кода проекта.
Это эталоны для будущих серверных тестов, а не результат исполнения mapper.

### Mapping и ограниченный разбор

Общие Event поля: schema_version=1, occurred_at=timestamp с нормализацией UTC,
service из проверенной записи, source из peer, artifact_ids=[], доверенные IDs/revision.

| Low запись | Event v1 | data / результат |
|---|---|---|
| header, footer | отсутствует | только raw/checkpoint и service ledger, никогда pending |
| connection TCP/HTTP | connection.opened | закрытый low_seq; payload обязан быть пустым |
| request TCP | tcp.request | low_seq, payload_b64, captured_bytes, mapper_truncated, capture_completeness="unknown" |
| request HTTP | http.request | те же capture поля; http_parse_state, при корректной первой строке method/http_version/path |
| неизвестный event/protocol, неверная форма | отсутствует | безопасная ошибка, raw не признаётся сконвертированным |

Полный оригинальный payload всегда остаётся base64 в durable raw. В Event хранится
base64 префикса; нет декодирования бинарных/неверных UTF-8 в replacement-текст.
`captured_bytes` — длина декодированного **исходного захвата**, не длина сообщения
клиента. `mapper_truncated` — только сокращение префикса при сборке Event.
Пустой payload допустим. Ровно capture limit, меньше него, footer и HTTP delimiter
не доказывают полноту сетевого запроса: capture_completeness всегда unknown.
Поле `truncated=false` не создаётся. HTTP status/success/response не выводятся
из request; UI пишет, что результат ответа не записан.

Разбор HTTP читает не более 4096 decoded bytes, стартовая строка не более 1024 bytes,
не более 32 строк заголовков и 1024 bytes на строку. Только ASCII стартовая строка
`METHOD SP target SP HTTP/1.0|HTTP/1.1 CRLF`, METHOD из GET/HEAD/POST/PUT/DELETE/
OPTIONS/PATCH/TRACE/CONNECT. Неполная строка/блок => incomplete; неверный синтаксис
=> invalid; выход за бюджет => limited; корректный блок => parsed. Ошибка HTTP
синтаксиса не теряет наблюдение request и сохраняет raw; это не ошибка формы журнала.
Headers/query/body никогда не становятся публичными полями. Исходный target
сохраняется только в закрытом raw/payload_b64. Предлагаемая публичная path:
`/`, если target ровно `/`; иначе `[скрыто]`. Это скрывает также секреты в path,
которые удаление query из существующей PublicPolicy само по себе не скрывает.

Event.data ограничен **точно** 8192 serialized UTF-8 bytes через `compact_json`.
Mapper сокращает только base64 префикс по максимальному числу исходных bytes,
которое помещается вместе с metadata; флаг mapper_truncated участвует в расчёте.
Metadata не усекаются молча: если сами не помещаются, конверсия завершается ошибкой.
Повтор даёт те же канонические bytes. Никакой сетевой запрос не исполняется.

### Публичная проекция и frontend

В одной общей PublicPolicy для REST/WS/lookup разрешить tcp.request и для обоих
request типов только captured_bytes (integer >=0), mapper_truncated (boolean),
capture_completeness (строго unknown). Для HTTP дополнительно method из списка,
http_parse_state из указанного enum и безопасную path по правилу выше.
Не публиковать payload_b64, body, headers, target, tokens, credentials, digest
и произвольные поля. UI показывает TCP request и неизвестную полноту захвата,
для HTTP — отсутствие записи результата ответа. Source=null отображается и
исключается IP фильтром/IoC. Обновить agent schema, backend schema copy/OpenAPI,
public schema, frontend enum/фильтры/подписи/mocks и contract tests в одном PR.
Существующая политика остальных типов этим предложением не меняется.

### Долговечность, ротация и запуск

Сохранение raw/checkpoint — существующая BEGIN IMMEDIATE транзакция raw repository.
Конверсия pending/ledger — отдельная существующая BEGIN IMMEDIATE транзакция
LowRawConverter; исходное сырьё проверяется побайтно. До commit падение откатывает
pending и ledger; после commit повтор/ACK не создают нового pending. Ошибка mapper
останавливает конверсию источника (`MAPPER_ERROR`), ledger успеха не записывается;
сырьё и checkpoint остаются. Исправление причины требует повторного запуска,
автоматического обхода/удаления/успешного карантина нет.

Sender использует прежние snapshots/backoff/ACK validation. Нет ACK/потеря ответа/
неполный ACK => неподтверждённые pending сохраняются; корректный полный ACK с
retryable отказами удаляет только accepted, повторяет retryable, permanent
сохраняет в quarantine. Backend commit event+outbox предшествует ACK; одинаковая
пара agent/event даёт повторное accepted без новой записи/publication.

Ротация следует previous_journal_id, journal_seq и исходному seq; session не меняется.
Рестарт сборщика/sender перечитывает сохранённые позиции. Перезапуск **Low** с тем
же instance и новой цепочкой previous_journal_id=null остаётся блокирующим
MISSING_PREDECESSOR/OPEN_PREDECESSOR по текущему collector: доверенная граница
процесса отсутствует. Предлагаем сохранить это ограничение в данном PR и проверить
диагностируемый отказ, не разрешать поздний null автоматически. Новый instance
получает отдельный binding и начало цепи. Для сквозного теста рестарта доставки
перезапускаются сборщик/sender; сохранённый журнал Low не удаляется.

CLI должен получать explicit paths, binding, spool, endpoint и лимиты из
конфигурации/аргументов, Bearer только из HF_AGENT_TOKEN; HTTPS проверяет TLS,
HTTP допустим только loopback. Один sender на spool. Ошибка конверсии видима
и не становится успешным exit status. Linux: collector читает группу 65532
через read-only mount, spool отдельно writable; нужны реальные POSIX проверки.
Cleanup raw/ledger/journal/snapshots не добавляется. Историческая принадлежность
instances проверяется production ingress, а не header и не тестовой identity таблицей.

### Приёмка и состояние проверки

Решение Кирито получено 13.10 и зафиксировано в начале документа.
Обязательны реальные TCP/HTTP Low, бинарный/Unicode/invalid UTF-8, лимиты serialized
data, ошибки формы/HTTP, ротация/конфликт bytes/рестарт/mapper version, crash commit,
недоступный центр/ACK loss/partial ACK, PostgreSQL commit/dedup/publication/ownership,
REST=WS и браузер без MSW с точными IDs/counts. Команды и SHA будут записаны после
реальных запусков; proposal и существующие тесты не подтверждают production mapper.
Windows/Linux/PostgreSQL/browser результаты отчёт обязан разделять.

Исходная проверка до согласования (Windows, 11.10.2026): ветка
`codex/low-production-mapper`, HEAD/main
`391d292a92c54180dc76ab08ce2417b0afbad027`, Python 3.12.13 из существующей
`.venv` основного checkout. Код не изменён, единственный tracked diff — этот
proposal. Из корня отдельного worktree выполнены:

```powershell
$python = 'C:\DTSU\vsprojects\neiroleptik\.venv\Scripts\python.exe'
$env:TEMP = Join-Path (Get-Location) 'work\temp'
$env:TMP = $env:TEMP
New-Item -ItemType Directory -Path $env:TEMP -Force | Out-Null
& $python -m pytest tests/telemetry -q --basetemp work/baseline-native --tb=short
& $python -m agent.telemetry.export_contracts --check
& $python -m mypy --cache-dir work/mypy-baseline
& $python -m ruff check agent/telemetry tests/telemetry
& $python -m ruff format --check agent/telemetry tests/telemetry
git diff --check
```

Результат вне sandbox: 530 passed, 37 subtests passed, 25.65 s; mypy — 49 файлов
без ошибок; экспорт схем совпадает; lint/format/diff без ошибок. Первый прогон
в sandbox дал 34 failed/496 passed/19 errors (сокеты, ссылки и TEMP недоступны);
он не считается успехом. Повтор был сделан с разрешённым локальным TEMP и
полным доступом к локальным тестовым процессам. Это исходная регрессия main,
включая существующие настоящие Low/SQLite/loopback HTTP тесты, а не приёмка
нового mapping. PostgreSQL, браузер без MSW, Linux, сборка итогового изменения
и цикл трёх субагентов перед push пока не выполнены. Push/PR ещё не сделаны.

Статус: реальные примеры готовы; 11.10 получены ответы backend (13:19), runtime
(13:25) и Дианы (14:36). Общий production mapping и точные публичные DTO ещё
согласуются; Диана поддержала серверное маскирование и порядок интеграции.
Владелец проекта передаёт этот документ Диме. Текущий адаптер
`agent/telemetry/adapters/low.py` не принимает новый файловый формат; здесь он
не меняется. Сроки D1–D5 остаются прежними. Cowrie/TTY — следующий этап.

## Реальные примеры

10.10.2026 запущен настоящий процесс `python -m honeypots.low` на loopback;
167 синтетических TCP/HTTP соединений, лимит захвата 2048 байт, размер файла
262144 байта, квота 2097152 байта, корректное завершение, три файла ротации.
Контейнер Docker и Event v1 доставка этим запуском не проверяются.

В [samples/low-executor-real/](samples/low-executor-real/) лежат **фрагменты**
неизменённых строк: header, TCP connection/request, HTTP connection/request,
усечённый HTTP payload, footer rotation, следующий header и footer shutdown.
Это не полные журналы: пропуски seq здесь вызваны выборкой. `positions.json`
содержит имя, размер, SHA-256 полного исходного файла и исходные byte_offset
начала каждой выбранной строки. Смещения в файлах фрагментов не являются
позициями исходных событий. Полные локальные файлы находятся в
`artifacts/executor-low-real-20261010/` и не коммитятся.

Воспроизведение полного набора в новом пустом каталоге:

```text
python -m infra.runtime.sample_low_journal --output <пустой-каталог> --rotate
```

Новый запуск создаст другие UUID и timestamps. `connection_id` связывает события
одного TCP-соединения; оно может пересекать границу файлов (первый файл заканчивается
connection, следующий начинается request с тем же ID). `seq` непрерывен внутри
процесса/непрерывной цепи. Новый процесс того же instance начинает seq заново,
продолжает journal_seq и пишет previous_journal_id=null; seq не глобален для instance.
`journal_seq` монотонен, новый файл получает новый
journal_id и previous_journal_id. Footer подтверждает только закрытие runtime-файла,
а не сохранение телеметрии или ACK центра.

## Ответ на предложения телеметрии

- Позиция `(journal_id, byte_offset)` подходит для детерминированного event_id
  с доверенными agent/deployment/instance. Смещение начинается с первого байта строки;
  Event v1 stream_seq назначает центр. Изменение текущего source_seq=offset
  остаётся предметом согласования; journal_seq не заменяет stream_seq.
- connection → connection.opened и HTTP request → http.request предложены Димой.
  Для TCP request Дима предложил новый тип `tcp.request` (вместо прежнего варианта
  network.data/inbound); добавление типа в Event v1 и во фронтовые типы утверждают
  владелец и Диана. До этого TCP request не отображается в другой тип.
- payload_b64 содержит первые байты **входящего** чтения, ограниченного
  max_payload_bytes профиля. В текущей схеме нет исходной полной длины, truncated
  и результата отправки HTTP-ответа. Поэтому адаптер не вправе считать захват полным
  при длине, равной лимиту, или утверждать успешный HTTP 200 по одной записи request.
  Ограниченный разбор HTTP можно выполнить в адаптере; Event v1 data ≤8192 байта.
- Отдельный непривилегированный сборщик и общая группа предложены Димой;
  UID/GID и проход по родительским каталогам согласуются для сервера. Runtime сейчас
  создаёт Low-каталог с 0750 и владельцем 65532:65532. Монтирование сборщику read-only
  требует проверки на сервере; Windows ACL не доказывают POSIX доступ.
- Cleanup закрытых файлов после долговечного сохранения полных строк/сырых данных
  — предложение. Runtime автоматического удаления не делает. Ошибки/квота не должны
  превращаться в успешную доставку; цель чтения за секунду ещё не измерена.
- Health probe использует публичный порт и попадёт в журнал. Фильтрация по адресу
  хоста/шлюза и метке в пользовательском payload не согласована. Для надёжного
  отличия probe нужен отдельный management-порт или доверенная корреляция.

## Что нужно подтвердить

1. Принять этот Low JSONL как вход адаптера с межфайловой связью connection_id.
2. Зафиксировать event_id/source_seq/session_id и правило TCP request в Event v1.
3. Выбрать добавление truncated/captured_bytes/исходной длины и подтверждённого
   результата HTTP-ответа или явно описать текущие ограничения захвата.
4. Согласовать UID/GID, read-only доступ, cleanup и измеримую нагрузку/квоты.

Изменения исходной схемы Low и Event v1 вносятся после согласования владельцами
интеграции в одном PR со спецификацией, адаптером и тестами. Seal/manifest и Cowrie
JSON/TTY в этот этап не входят.

## Вопросы следующего этапа долговечного сборщика (11.10.2026)

Независимый сборщик сохраняет оригинальные строки; production Low → Event не
подключён. Пользователь отправил вопросы и передал ответы backend/runtime;
агент самостоятельно коллегам не пишет. Следующий раздел уточняет прежние вопросы.

- Данил: подтвердить правило позднего `previous_journal_id=null` при новом процессе
  с прежним instance и `journal_seq>1`. Как доверенно установить новую границу цепи,
  если прежний файл оборван без footer? До решения сборщик останавливается в ожидании.
  Подтвердить права read-only сборщика, UID/GID, сохранность каталогов и политику cleanup.
- Владелец backend/серверов («Ты» в отчёте): утвердить scope детерминированного
  event_id, смысл source_seq, авторизацию исторических instances и null peer;
  назначить согласованные ingest-маршруты и изолированный PostgreSQL-стенд.
  Подтвердить HTTP mapping, обработку превышения Event.data, маскирование и срок
  хранения raw/ledger/snapshots. До этого production API и миграции не меняются.
- Диана совместно с backend: утвердить `tcp.request`, отображение capture без
  доказательства полноты и результата HTTP, связь connection_id/session_id,
  допустимый UI DTO и правила маскирования. Тестовый mapper не заменит это решение.

После согласования конверсия должна атомарно связать pending с отдельным ledger,
сохраняющим обработанные исходные позиции после ACK. Повтор чтения raw не должен
создавать новый Event после удаления pending. Для существующего старого адаптера
эту границу уже обеспечивает сохраняемый spool checkpoint.

## Полученные ответы и границы реализации (11.10.2026)

Подтверждено backend: event identity задаётся agent/deployment/instance/journal_id/
байтовым offset начала строки; сервер дедуплицирует пару agent_id/event_id и
сравнивает неизменяемый нормализованный оригинал. source_seq остаётся byte offset
поколения. Принимать запоздалые события завершённых instances нужно по исторической
принадлежности/revision, без перевода runtime в running; неизвестная регистрация
временно INSTANCE_NOT_REGISTERED, чужая принадлежность отклоняется. ACK следует
commit event+outbox и не ждёт publisher/UI. Это подтверждённые требования, а не
заявление о готовом production ingress. UUID namespace/кодирование и контрольные
примеры для нового Low ещё совместно фиксируются; старый LowAdapter не меняется.

Backend предложил: nullable source без фиктивного IP, tcp.request, session_id по
agent/deployment/instance/источнику low/connection_id без journal_id; четыре состояния
полноты; отсутствие выдуманного HTTP 200; единую серверную проекцию REST/WS/export.
Политика скрывает password/passwd/pwd, Authorization/Proxy-Authorization/Cookie/
Set-Cookie, tokens/API keys в headers/query/структурированных данных. Raw/base64/body
по умолчанию закрыты, commands/replay требуют allowlist; оригинал/digest закрыты.
Точные DTO, типы, поля и доступы ожидают совместной фиксации с Дианой.
Тестовая маска password в новом стенде не заменяет эту политику.

Распределение работ из ответа backend: наша область — Low adapter/очередь/доставка
и существующие storage/publisher/read с тестами; backend владеет production Alembic,
агентской авторизацией, /agent/v1/events, подключением пользовательских routes и
запуском publisher. Таблицы/mapping/проекция совместные. Для MVP предложен отдельный
процесс publisher с единственным активным издателем. Общие миграции запускает backend.
Первый стенд — локальный PostgreSQL-центр и Low Данила; внешнего TLS URL/реквизитов
пока нет. TLS verification сохраняется. Веспер пока не согласованный стенд.

Факты runtime сверены с journal.py, service.py, LowDriver и executor/specs:
полные LF-строки без footer сохраняются, неполный хвост ждёт LF; новый процесс
того же instance продолжает journal_seq, обнуляет seq и предыдущий UUID. Поэтому
порядок по одному (instance,seq) неверен и такая уникальность запрещена. Collector
по-прежнему требует predecessors и останавливается на MISSING_PREDECESSOR/
OPEN_PREDECESSOR; конверсия уже сохранённого raw может продолжаться независимо.
Новая попытка start и пересоздание на apply_config/reset меняют instance, повтор
команды/rotation/stop/recovery существующего контейнера — нет. Start UUID сохраняется
до run; recovery сверяет managed/deployment/level/instance/revision/container ID;
при отсутствии контейнера попытки — RECOVERY_UNCERTAIN без повторного run.

Каталог Low сейчас 0750, 65532:65532; UID/mounts сборщика не назначены. Cleanup нет.
Предложенный runtime receipt journal_id+конечный размер+hash ещё не определяет
доверенную закрытость, полноту аварийного хвоста или replay после удаления predecessor.
Read-only сборщик не удаляет источник. Код service._accept проверяет stopping и
max_connections, но не disk pressure: ошибка sink закрывает текущее соединение,
listener продолжает; TCP banner может предшествовать неудачной записи request.
Конец ответа runtime об initial connection оборван; его продолжение не восстановлено.
Нулевые потери pending после append не означают нулевые потери сетевых запросов.

## Варианты оставшихся точечных решений

Все строки ниже — предложения. Они не подключены как production-контракт.

| Тема | Рекомендуемый вариант | Альтернатива | Вход → выход / влияние на тесты |
| --- | --- | --- | --- |
| Crash/новый процесс и provenance | Доверенная runtime граница процесса; отдельный journal, явный gap и порядок journal_seq/offset с сохранением исходного seq | Блокировать переход до ручного подтверждения | instance I: J1/seq=9, J2/journal_seq=2/null/seq=1 → два источника, gap; тесты collector перехода отложены, raw conversion ID проверены |
| UUID Event | Зафиксировать общий namespace/encoding с совместимостью старого adapter и golden vectors | Отдельная явно версионированная identity миграция | rename J/offset O → прежний ID; J2/O → другой; алгоритм production отложен |
| Историческая принадлежность | Неизменяемая таблица разрешённых старых instances/revisions в backend | Архивный реестр с явной проверкой доступа | stopped I → accepted без running; чужой I → rejected; регистрация не видна → retryable; production auth тесты у backend |
| peer/session/type и DTO | source=null, tcp.request, session из достоверного connection_id без journal_id | До общего обновления сохранять raw без Event | connection C в J1/J2 → одна session; C1/C2 одного IP → разные; схема/UI тесты отложены до Дианы |
| Capture/HTTP | Явные incomplete/gap/unknown/confirmed состояния; ответ только при фактической записи | Пока UI показывает только request с unknown completeness | request без ответа → «не записан», footer не повышает полноту; mapping тесты отложены |
| Проекция | Единый серверный allowlist и секретные оригиналы | Временно минимальный DTO метаданных | password A/B → одинаковый public mask, оригиналы разные → конфликт; password тест исполнен, полный allowlist/export отложен |
| Linux-права | Отдельный непривилегированный UID, supplementary group 65532, узкие read-only mounts и writable spool | Отдельный runtime relay сырья | журнал читается/не удаляется; spool записывается; требуется Linux-стенд |
| Cleanup и replay | Runtime receipt с hash/size и доверенным закрытием + согласованный retained provenance для replay | Пока без cleanup | неверный hash/активный writer/tail/repeat/crash/replay без predecessor → запрет удаления; все cleanup тесты отложены |
| Disk pressure | Совместный degraded/admission/recovery сигнал collector↔runtime и замер до durable append | Существующая обработка sink failure с явной диагностикой | sink fail → отдельный отказ записи/сессии, listener сейчас продолжает; admission не обещан; ENOSPC только bounded volume |
| TLS-стенд и publisher | Backend передаёт проверяемые URL/requisites; publisher отдельный singleton process | Изолированный локальный Event-стенд | ACK при остановленном publisher проверен; внешний TLS/production worker ещё не проверены |

## Ответ frontend 11.10.2026 14:36 и предложение следующего ответа

Диана приняла порядок: лента/поток → живой Low → сводка, параллельно точные DTO.
До приёмки экраны используют моки с пометкой «демо-данные»; о PR сообщил frontend,
его содержимое здесь не проверялось. Серверное маскирование поддержано, но wire
формат ещё выбирается. Диана предложила подготовить отдельный OpenAPI draft по
frontend-dto-proposal.md для совместной сверки с backend и проверить реальные
Cowrie-образцы после их получения от Данила.

Рекомендуемый ответ, ещё не отправленный участникам: удалять data.password из
публичного auth.attempt и добавлять boolean data.password_present. True означает
наличие ключа password в исходной записи, включая пустую строку; False означает
отсутствие этого ключа, а не доказательство отсутствия пароля в сетевой сессии.
Отсутствие самого флага в старом DTO означает неизвестное состояние. При True
frontend показывает «пароль скрыт», при False — «пароль не записан»; исходный
пароль, его длина и крайние символы не передаются. Флаг вычисляет сервер,
одноимённое входное поле атакующего не используется. Оригинал для хранения и
дедупликации остаётся неизменным. Один формат применяется в REST/WS; export
подчиняется той же политике без секретных значений. Это предложение публичной
проекции, не изменение agent Event v1, и требует фиксации с frontend/backend.

Черновик точных схем предлагается принять отдельным файлом
specs/frontend-openapi-proposal.yaml со статусом proposal, без замены
генерируемого contracts/openapi.json. Помимо имён и nullable-полей нужны фильтры,
sort/order, snapshot/cursor, ошибки и примеры публичной проекции. Документ
согласуется с backend до публикации в рабочем контракте.

Реальные Cowrie JSON/TTY сначала нужны адаптеру. Frontend проверяет отображение
соответствующих нормализованных EventV1/ReplayChunkV1 с ожидаемыми результатами;
сырой формат Cowrie не становится новым публичным форматом replay. Политика
публичного replay ещё согласуется. Production mapping/проекция и UI обработка
password_present этим уточнением не реализованы; тестовая маска не меняется.
