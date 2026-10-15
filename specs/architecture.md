# Архитектура HoneyForge

Доверенная граница процесса Low при перезапуске в прежнем instance описана в
[journal-process-boundary-proposal.md](journal-process-boundary-proposal.md):
writer identity, durable fencing, защищённые crash snapshots и привязка новой
цепочки, начальная привязка writer (anchor), локальная публикация с правами
runtime/collector и durable registry producer. **Сторона runtime реализована**
в `agent/runtime/boundary/` вместе со startup-выделением первого поколения в
CLI Low; потребление proof сборщиком (сторона Димы) не реализовано, поэтому до
его согласования действуют прежние waiting и обработка проверенного raw.
Формат журнала, Event DTO и API центра не меняются.

## Low production mapping, 13.10.2026

`agent/telemetry/low_journal/production.py` реализует RawEventMapper согласно
low-telemetry-handoff.md. Не хранит состояние: идентичность вычисляется из
доверенной привязки и физической позиции, транзакциями владеет LowRawConverter.
`agent/telemetry/collect_low.py` — ограниченный CLI проход reader/collector →
RawJournalRepository → LowRawConverter → SpoolRepository в одном файле SQLite.
При ожидании predecessor ранее сохранённые записи продолжают конвертироваться.
Отправляет существующий deliver.py; требуется один sender на spool.
Общий тип tcp.request проходит ingress/history/PublicPolicy и UI. Контракт
принят; факт сквозной работоспособности до серверной приёмки не утверждается.

## Независимая серверная подготовка 12.10.2026

[infra/telemetry/server-acceptance.md](../infra/telemetry/server-acceptance.md)
содержит команды и матрицу доказательств, без развёртывания и запуска.
`tests/telemetry/test_low_journal_readonly_mount.py` — изолированная серверная
фикстура private mount namespace, не новый runtime модуль. Вместе с DAC,
ACK/crash и блокирующим late-null тестом она использует существующие
reader/collector/raw/spool/conversion. Production mapping и контракты не меняются.
Итоговые проверки не выполнялись; будущая сквозная приёмка зависит от согласований.

## Рабочий MVP расследования 11.10.2026

Требования: [investigation-mvp.md](investigation-mvp.md). telemetry/runtime.py
подключает настоящие ingest/read/stream зависимости и outbox poller к factory.
ingress.py проверяет включённую в wheel экспортированную agent JSON Schema и
историческую привязку RuntimeInstance. Backend не импортирует agent runtime.
hf_telemetry — рабочая схема с отдельной Alembic миграцией; прежние control
таблицы сохраняются. Test schemas остаются изолированными и имеют ту же модель.
investigation/tables.py хранит alert группы/основания, URL token/срабатывания
и артефакты. projector.py работает в транзакции publisher под DB блокировкой
журнала; service.py формирует публичные DTO, lookup и согласованный snapshot CSV.
Проекция имени артефакта заменяет NUL, а оригинал остаётся в canonical_bytes
события-основания. Метаданные читаются без content, список использует SQL keyset
observed_at/id DESC с limit+1; срок доступности вычисляется одним методом для
списка, карточки и скачивания. Ingress проверяет вместимость нормализованного
source.ip до записи, чтобы одно событие не откатывало пачку.
iocs.py ограничивает поля/URL и формирует безопасный файл до HTTP ответа.
api/investigation.py применяет общие Reader/Operator роли, схемы — в
api/investigation_schemas.py. masking.py задаёт одну политику REST/WS/lookup.
Индексы истории/timeline разделены. Нет загрузки файлов runtime, provision
URL токенов, replay, паспорта сессии, HTTP analytics/summary и других правил.
Эти границы не означают готовность полного D3 сценария с реальными ловушками.
Прежние разделы об отсутствии production подключения отражают историю этапов.

Независимые стенды следующего этапа: `agent/telemetry/benchmark.py` измеряет
существующие spool/sender с синтетическим ACK-получателем, а `tty_fixture.py`
читает собственный тестовый двоичный формат. `scripts/telemetry_acceptance.ps1`
запускает сетевую PostgreSQL-приёмку под существующей эксклюзивной блокировкой.
Они не подключают production Low, replay, runtime lifecycle или cleanup.

## Внутренняя конверсия и тестовая интеграция (11.10.2026)

`agent/telemetry/low_journal/conversion.py` связывает сохранённый raw с существующим
SpoolRepository через чистый внедряемый RawEventMapper. Одна транзакция spool
фиксирует pending и ledger, raw repository продолжает отвечать за своё сырьё.
Ledger переживает ACK/карантин; production mapping и runtime wiring отсутствуют.
`backend/tests/telemetry/integration_app.py` — только тестовая сборка реальных
storage/publisher/read/history/stream/transport сервисов с HTTP ingress. Routes
рабочего приложения и production миграции не меняются. Тесты используют агентский
пакет как dev-зависимость, а не второй sender или копию Event/ACK-контракта.

## Согласованное решение 09.10.2026

Владелец проекта подтвердил использование существующего neiroleptik, FastAPI
вместо Django и API /api/v1/auth/*, /profiles, /hosts, /deployments.
Работа идёт от main; незавершённая ветка Юми не является зависимостью HoneyForge.

## Первый этап: control plane

- backend/src/honeyforge: модульное приложение FastAPI.
- config.py и db.py: параметры окружения и SQLAlchemy sessions.
- models/: пользователи, сессии, профили/версии, узлы/регистрация, deployments, аудит.
- auth/: сессии, роли, CSRF, Argon2, ограничение попыток входа.
- control/: профили, регистрация и назначение профиля узлу.
- api/: HTTP-контракт и проверка прав; бизнес-операции в сервисных классах.
- migrations/: Alembic; production-приложение не создаёт таблицы автоматически.
- contracts/openapi.json: сгенерированный и проверяемый общий API.
- infra/compose.center.yml: локальный центр с PostgreSQL и постоянным volume.

Пользователь -> FastAPI -> PostgreSQL. Агент -> отдельная Bearer-авторизация ->
конфигурация только своего узла. TLS и сетевое разделение устанавливаются
при развёртывании сервера. Локальный Compose публикует сервисы только на loopback.

## Границы первого этапа

Deployment создаётся в состоянии planned. Это запись намерения, а не запуск
процесса и не доказательство изоляции. Реальные Low/Medium/High, события, replay,
WS и артефакты реализуются следующими этапами/участниками. Из UI нельзя выдать
planned за running.

## Второй этап: команды и подтверждение применения (10.10.2026)

Общий протокол согласован владельцем после обсуждения с Данилом: specs/commands.md.
models/commands.py хранит долговечные задания; control/commands.py сериализует
создание, lease, продление и ACK блокировками host → deployment. command_policy.py
проверяет смысл действий и результатов; api/command_schemas.py содержит единый
JSON-контракт пользователя и агента. models/instances.py и control/instances.py
сохраняют историю UUID, чтобы после reset нельзя было повторно использовать
идентичность старой ловушки и смешать сессии. Миграция 4fa62bdeddd4 расширяет deployments
поколением, применённой ревизией и идентичностью экземпляра, добавляет commands.
Откат после использования lifecycle запрещён, чтобы не потерять подтверждения.

Пользователь → command/job API → PostgreSQL ← Bearer-agent claim/renew/ack.
Состояние runtime подтверждается ACK; lease/timeouts не доказывают остановку.
В unknown нельзя start/reset, требуется подтверждённая очистка stop. Повторный
claim выдаёт прежний command_id с новым lease после истечения; новый token
ограждает ACK от старого исполнителя. Долговечный журнал и физическую
сериализацию на runtime-хосте реализует Данил: это не гарантия exactly-once на сервере.
GET /deployments и /agent/v1/config обновляют сроки при чтении; worker не требуется.
Frontend отображает фактические состояния; его экраны и подключение к API описывает
specs/frontend.md.
Телеметрия связывается с instance_id, который подтверждает агент; reset/start
создают новый ID, restart демона и повторы ACK его не меняют.

## Ответственность команды

Участник 1: frontend. Участник 2: auth/control/db/CI. Участник 3: runtime и
применение конфигурации. Участник 4: telemetry/realtime/analysis.
Общие миграции и изменения OpenAPI согласуются через владельца backend.
Форматы событий не фиксируются молча в рамках этого PR.

## Runtime ловушек (участник 3)

Статус: локальный блок D1 и исполнитель команд центра для Low; Medium и High —
следующий этап. Срок — пять дней, 30 человеко-часов
(10 на агент/Low/Medium, 12 на High, 8 на сеть, интеграцию и выпуск).

### Границы модулей

- `agent/runtime/` — локальная логика участника 3: идентификаторы операций,
  защита от повторных команд и устаревших поколений.
- `agent/runtime/executor/` — HTTP claim/renew/ACK по `specs/commands.md`,
  закрытый SQLite-журнал исполнения и истории ревизий, один владелец состояния,
  lease перед каждой мутацией Docker, сверка Low после рестарта. Точка входа
  `python -m agent.runtime.executor`; требования — `runtime-agent-executor.md`.
  `client.py` разделяет ограниченный HTTP-транспорт и Bearer-команды;
  `enroll.py` регистрирует только Low, `tokens.py` резервирует новый закрытый
  файл до одноразового enroll. CLI независимо отправляет heartbeat каждые 10 с.
  `LowHealthCheck` подтверждает ответ сервиса при start и отказ TCP-подключения
  по всем опубликованным портам при stop. Наблюдение не даёт права удалять
  контейнер с чужой идентичностью или процесс, занявший порт.
  Управление Medium/High и VM остаётся следующим этапом.
- `agent/runtime/boundary/` — доверенная граница процесса Low
  ([journal-process-boundary-proposal.md](journal-process-boundary-proposal.md)).
  Отдельная внутренняя операция runtime, которую исполнитель команд центра не
  вызывает: `documents.py` — канонические anchor и proof, `journals.py` —
  наблюдение каталога слота и цепочки поколений, `registry.py` — долговечные
  intents, состояния, сожжённые идентичности и выданные байты документов,
  `snapshots.py` — закрепление байтов закрываемого писателя до выдачи нового RW,
  `publication.py` — локальная публикация write-once через
  `renameat2(RENAME_NOREPLACE)` (только Linux), `launcher.py` — одна жизнь
  контейнера-писателя и привязка фактического заголовка, `producer.py` — порядок
  шагов перехода и восстановление после сбоя. Зависимости направлены в одну
  сторону: пакет использует `drivers/` и `contract.py`, а они о нём не знают.
- `honeypots/low/` — ограниченные TCP-баннер и HTTP-заглушка. Не исполняют
  полученные команды. Пишут локальные диагностические JSONL-события. Журнал
  принимает необязательное startup-выделение первого поколения
  (`--journal-first-id`, `--journal-first-seq`), которое передаёт runtime
  при привязке нового писателя; формат журнала от этого не меняется.
- `infra/vm/` — неразрушающая предварительная проверка Linux/KVM/libvirt.
- `infra/runtime/` — инструкции подготовки runtime-хоста и Cowrie spike; Compose-файл
  Low лежит в корне (`compose.runtime.yml`), Dockerfile — в `honeypots/low/`.
  `boundary_stand.py` — CLI совместного теста границы процесса: он вызывает
  настоящий producer на настоящем Docker Engine, печатает пути опубликованных
  документов и не выполняет аварийного завершения писателя сам.
- Сборщик, spool, передача событий и артефактов принадлежат участнику 4.
  Локальный JSONL Low не является заменой spool или контрактом центра.
- Центр (`backend/`) принадлежит участнику 2. Runtime будет обращаться к нему
  только через `/agent/v1/*` из `contracts/openapi.json`; исполнитель использует
  определённые enroll/heartbeat/config/claim/renew/ack, не меняя общий API.

Low предназначен для запуска в непривилегированном контейнере (запуск в Docker ещё не
проверялся); для разработки допускается локальный запуск на loopback. Среда High требует отдельного Linux-хоста с KVM.
Менеджер VM и proxy имеют раздельные полномочия. Proxy не получает токен центра,
libvirt socket или Docker socket. Окончательная сеть задаётся после preflight.

Спецификация локального блока: [runtime-d1.md](runtime-d1.md).
Исторические предложения межмодульных форматов: [runtime-contract-proposal.md](runtime-contract-proposal.md).
Командный API определяют [commands.md](commands.md) и `contracts/openapi.json`; формат
журнала Low для телеметрии ещё согласуется с участником 4.

Корневой `pyproject.toml` и `uv.lock` общие для телеметрии и runtime: проект
`honeyforge-agent` собирает пакеты `agent` (`agent.telemetry`, `agent.runtime`),
`honeypots` и `infra`; зависимости времени выполнения только у телеметрии (pydantic),
код runtime и образ Low их не используют. Инструменты и их версии — из `uv.lock`.

## Первый шаг телеметрии D1 (участник 4)

Источник планов: HoneyForge-plan.html и HoneyForge-4-Telemetry.html, 09.10.2026.
agent/telemetry/schemas.py: входной Event/Batch/ACK на Pydantic, точная
канонизация оригинала и коды временных/окончательных отказов; адаптер Low
в adapters/low.py. spool.py отвечает за атомарные event+checkpoint и полный
ACK: удаляет accepted, сохраняет retryable, переносит permanent в долговечный
SQLite WAL карантин. QuarantineEntry — отдельный снимок доказательства;
чтение ограничено, автоматической очистки и повторной постановки нет. Python 3.12.
`next_batch()` одной SQLite-транзакцией читает pending и сохраняет неизменяемый
канонический BLOB пачки в batch_snapshots по batch_id. `acknowledge()` проверяет
состав, порядок и payload переданной модели по этому снимку, включая после
рестарта и удаления pending. Привязка не создаётся и не меняется при ACK;
снимки пока сохраняются для повторных/поздних ответов без автоочистки.
Корневой pyproject.toml/uv.lock изолируют зависимости агента от backend.
Входные JSON Schema и синтетические fixtures лежат в contracts/.
Существующий Event для UI и frontend-protocol-v1.md остаются основой интеграции.

Реализован локальный путь Low JSONL → SQLite → batch → ACK/карантин. Формат исходного
Low JSONL предложен для согласования, реальный журнал ещё не проверен.
Следующий шаг: ingest в backend/src/honeyforge/telemetry с общей DB/auth,
ACK после PostgreSQL commit, outbox/publisher, минимальный WS; затем реальный
Low → БД → UI. HTTP sender и backoff описаны ниже; квоты пока не реализованы.

`agent/telemetry/sender.py`: HttpBatchTransport отправляет Batch напрямую в центр
по HTTPS (HTTP только loopback), без proxy/redirect, с таймаутом и лимитом ответа.
TelemetrySender читает снимок spool, проверяет полный ACK, применяет его атомарно
и назначает экспоненциальный backoff с jitter/Retry-After. 401/403 и неожиданные
HTTP-статусы приостанавливают доставку; события остаются на диске.
`agent/telemetry/deliver.py`: CLI существующего spool, token из HF_AGENT_TOKEN,
один sender на очередь. Сбор файлового Low и серверный ingest подключаются отдельно.
Тесты используют настоящий loopback HTTP и сценарии обрыва/повтора, но не
доказывают PostgreSQL commit, наличие работающего ingress или живую ленту.

Следующие серверные модули пока только спроектированы, endpoints и миграций нет:

- ingest: отдельная валидация каждого недоверенного входного элемента;
  идентичность устанавливает общий Bearer current_agent, проверяются agent,
  deployment/revision и согласованный runtime lifecycle instance.
- storage: неизменяемый канонический входной Event отдельно от маскированного
  UI DTO; UNIQUE (agent_id,event_id) и один outbox на внутренний event PK.
  Одинаковый повтор accepted, иной payload EVENT_ID_CONFLICT; event+outbox
  записываются атомарно, ACK выпускается после commit.
- publisher: один worker MVP; сохранённая publication с уникальной ссылкой
  на event и отметка processed outbox в одной транзакции. WS выпускает только
  committed журнал; WS, REST и watermark читают один журнал публикаций.
  stream_seq монотонен в видимом committed журнале, это не sequence таблицы events.

Серверные тесты потери ACK, конфликтов среди хороших элементов, конкурентных
повторов и падений worker до/после commit обязательны на реальном PostgreSQL;
локальные тесты SQLite их не заменяют. Точные границы — specs/events.md.
Не согласованы instance_id при restart/reset, поколение источника, область
source_seq, связь connection/session и Cowrie/TTY. В VM_trap Low имеет другой
формат; root pyproject runtime требует отдельного согласованного объединения.
seal/reset требует остановки источников и manifest файлов, которого в spool нет.

D2: Cowrie/доставка/отказы; D3: timeline/replay/артефакты/token/alert/CSV;
D4: нагрузка/изоляция/целостность; D5: freeze и воспроизводимое демо.
Подробный контракт и критерии первого шага — specs/events.md.
Имена участников: Диана — frontend; владелец проекта — backend и серверы;
Данил — runtime/ловушки; Дима — telemetry/realtime/analysis.

## Frontend (участник 1)

- frontend/: React + TypeScript, Vite; требования — specs/frontend.md.
- src/app/: оболочка приложения, маршруты, тема.
- src/features/: экраны auth, overview, events, fleet (мастер запуска, команды),
  profiles; бизнес-логика экранов вынесена в отдельные модули рядом со страницами.
- src/shared/api/: HTTP-клиент с CSRF; типы ответов стадии 1 — по openapi.json.
- src/shared/stream/: StreamClient — WebSocket-клиент с курсором, догрузкой
  пропусков, переподключением, пересинхронизацией по 410 и дедупликацией по
  паре (agent_id, event_id); провайдер React поверх него (превью этапа 4).
- src/shared/security/: очистка вывода терминала атакующего, маскирование секретов.
- src/mocks/: MSW-заглушки на основе contracts/fixtures/*.draft.json.

Поверхности стадии 1 (auth, профили, хосты, развёртывания) приведены к
contracts/openapi.json: имена и форма полей совпадают с контрактом, развёртывание
создаётся в состоянии planned. Экраны команд/заданий, ленты событий и обзора —
помеченное превью на моках: соответствующих эндпоинтов и формата события в
контракте стадии 1 нет (этапы 3–4), их закрепление в OpenAPI согласуется с
владельцем backend и участником 4.

Редактор профиля отделяет снимок формы и его optimistic-lock revision от
фонового query-кеша. FeedBuffer отделяет REST-историю, показанный live и
pending; переполнение pending требует свежего снимка. StreamClient получает
новый hello до пересинхронизации и ожидает её Promise, буферизуя live.
Моки используют разные курсоры истории и журнала согласно общему протоколу.

### Восстановление runtime Low

`agent/runtime/contract.py` разбирает текущий AgentConfigRead, изолирует ошибки
назначений и скрывает секреты в диагностике. `drivers/base.py` сериализует lifecycle;
`infra/process.py` ограничивает время и вывод Docker CLI и preflight.
LowService выдаёт seq, connection_id и service по low-journal-v1, а CLI связывает
публичные порты и файловый журнал с параметрами LowDriver. Локальный smoke
infra.runtime.sample_low_journal запускает настоящий процесс и сохраняет образец.
CommandJournal хранит целевые поколения команд центра, включая stop/failed и
пропущенные недоставленные команды; старый журнал требует явной миграции.
Сетевой исполнитель Low реализован по текущему командному API. Преобразователь
нового журнала в Event v1 и реальные Docker/KVM/High требуют следующих проверок;
loopback smoke не подтверждает Docker-изоляцию или Linux/systemd.

## Изолированные сервисы чтения телеметрии

telemetry/read_contract.py задаёт read-only границу committed журнала.
telemetry/cursors.py подписывает ограниченные snapshot/keyset курсоры;
history.py и stream.py оркестрируют страницы без памяти вместо хранилища.
presentation.py создаёт отдельную публичную копию по явно внедрённой политике.
transport.py собирает router только по явному вызову для тестового приложения:
рабочий main.create_app его не подключает. WS читает ограниченные страницы,
проверяет настоящую AuthService в короткой Session вне event loop и освобождает
задачу disconnect при завершении. Сетевая перегрузка завершает WS стандартным
1013 (Try Again Later), восстановление через новый hello и REST. Это временная
политика изолированного приложения; production rollout ещё согласуется.
Ключ HMAC передаётся конструктору, production настройка не добавлена.
## Неподключённое ядро PostgreSQL-телеметрии, 10.10.2026

backend/src/honeyforge/telemetry/: types — внутренний неизменяемый PreparedEvent;
tables — отдельная MetaData с явно заданной тестовой схемой; storage — атомарная
запись event/outbox и побайтная дедупликация; publisher — блокировка стабильного
счётчика и атомарная publication/processed; reader — committed snapshot/keyset.
Engine и Session предоставляются вызывающим кодом. Импорт не меняет общую Base,
FastAPI, models/__init__.py, Alembic и запуск worker. Связи с Host, Deployment,
ProfileRevision и RuntimeInstance требуют согласования; UUID прототипа синтетические.
Тестовый setup создаёт только telemetry_test_<uuid> в honeyforge_test.

### Переданный ответ runtime 10.10.2026, 20:12 Москва

low-journal-v1 подходит для первой интеграции; реальные фрагменты ротации пока
в PR runtime. Явное отклонение переполнения сохраняет файл/offset. Профиль первой
интеграции может ограничить max_payload_bytes <=4096; предел Event.data остаётся
8192 сериализованных UTF-8 байта. Приблизительная оценка объёма сырых байтов не
заменяет этот предел. Отсутствие флага усечения не доказывает полноту журнала.
Deployment определяется управляемой привязкой каталога, revision — конфигурацией
центра; instance подтверждается ACK. Это не реализует авторизацию ingest.
TCP type требует решения владельца и Дианы; эвристика health-проб и удаление
файлов не утверждены. Третья runtime-ветка в этом задании не создаётся.

CommittedReadAdapter связывает сервисы stream с PublicationReader из storage SHA
50b4f3383b1aa27d896f466cb55ce29c75fc6028. Преобразует UUID/keyset/фильтры и
StoredEvent.original() в отдельный read DTO, не сериализует PK/bytes. Retention
возвращает None: очистки хранилища ещё нет, SQL gaps не считаются утратой.
Тестовый FastAPI может явно внедрять этот adapter; production main не меняется.

WS telemetry использует внутренний DBOperations: учтённый worker, контекстный
серверный SQL deadline и обязательная асинхронная очистка при отмене. Hook
SQLAlchemy не изменяет операции вне контекста WS. Общие модели, миграции и
production FastAPI не подключаются к этому прототипу.

## Независимый читатель файлового журнала Low

`agent/telemetry/low_journal/` — локальный read-only компонент по
[low-journal-reader.md](low-journal-reader.md). `models.py` задаёт неизменяемые
binding/record/summary и безопасную ошибку; `validation.py` проверяет JSON и
исходные записи, `source.py` открывает обычный файл без следования конечной
ссылке, `reader.py` читает ограниченный снимок и проверяет порядок записей.
Компонент использует только stdlib, не импортирует Event, adapter или spool.
Header/footer выдаются с исходными bytes и физическими позициями; checkpoint,
Event mapping, HTTP-разбор, sender, backend и UI остаются следующими этапами.
Статус low-journal-v1 как интеграционного предложения сохраняется.

## Долговечное сырьё Low

`agent/telemetry/low_journal/durable.py` — отдельные SQLite WAL/FULL таблицы
проверенных BLOB и поколений; атомарные raw+checkpoint, повтор и конфликт.
`collector.py` — ограниченный шаг по явно заданным файлам доверенного источника,
порядок цепочки, безопасная перепроверка префикса, ожидание ротации/предшественника.
`mapping.py` — только интерфейс будущего преобразования, production mapping нет.
Существующие Event/spool/sender и публичные API не изменяются. Сырьё не попадает
в pending и не удаляется автоматически. Граница и цена восстановления описаны
в [low-journal-reader.md](low-journal-reader.md), раздел 7.
