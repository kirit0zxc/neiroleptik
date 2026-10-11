# Контракты расследования HoneyForge: согласованный MVP

## Карта исходного состояния (main 5a3aa24)

Auth/control/runtime работают в основном приложении. Приём, хранение, публикация,
REST/WS телеметрии проверены в изолированном стенде, но не подключены к production.
Артефакты, honeytoken, IoC и правила отсутствуют; сохранённых иных kind нет.
Предложение frontend af93ce0 прочитано из origin/feature/frontend-openapi-proposal
(включено в main PR33). Оно не заменяет общий OpenAPI; прочие x-added остаются предложениями.

## Source и публичное событие

source обязателен: endpoint прежнего формата или явный null. Отсутствие отвергается.
Неизвестный источник не получает фиктивный IP/порт. История, timeline и REST/WS
сохраняют null; фильтр IP, топ источников и IP-правила его исключают.
Публичная проекция используется также lookup: allowlist полей data по типу,
auth.password заменяется password_present; секреты и произвольные поля не выдаются.
Оригинал не изменяется. URL с query/fragment/userinfo не выдаётся как действующий URL.

## Доступ и артефакты

| Операция | viewer | operator | admin |
|---|---|---|---|
| Метаданные и события-основания | да | да | да |
| Содержимое артефакта | нет | да | да |
| CSV IoC | да | да | да |

Без cookie 401; viewer download 403 (UI скрывает кнопку). Только available:
pending/failed 409 ARTIFACT_PENDING/ARTIFACT_FAILED, expired 410 ARTIFACT_EXPIRED.
Содержимое только attachment, application/octet-stream. Сбор/загрузка файлов
runtime не добавляются: metadata из artifact.observed, содержимое заполняет
доверенный внутренний сборщик; без него артефакт остаётся pending.
event_refs не более 100, обязательный event_refs_truncated, выбор по паре
agent_id/event_id ASC. Идентичность артефакта — artifact_ids события.

## Honeytoken

Только kind=url. Нет публичной выдачи секретного значения или рабочего URL.
Read: id, deployment_id, kind, label, created_at, last_triggered_at, trigger_count.
Уникальное token.triggered (agent_id,event_id) даёт одно срабатывание;
data.token_id — UUID, ссылка ровно одна. session_id берётся только из события,
null разрешён; совпадение IP никогда не связывает сессию. Провижининг токенов
в ловушки не добавляется: read отражает наблюдаемые token_id.

## IoC

GET /api/v1/iocs.csv: обязательные from/to RFC3339 с timezone, [from,to) по
occurred_at, from<to, максимум 31 день; optional deployment_id UUID. UI предлагает
24 часа. Наблюдение не является доказательством вредоносности.

| Тип события | Структурированное поле | Тип IoC | Нормализация |
|---|---|---|---|
| Все EventV1 | source.ip, если source != null | ipv4/ipv6 | ipaddress, compressed |
| artifact.observed | data.sha256 | sha256 | 64 hex, lowercase |
| artifact.observed | data.source_url | url | только http/https, hostname lowercase/IDNA, без default port |

Других URL-полей согласованный EventV1 не определяет: http.request.path не URL.
Никакого поиска в payload/input/output/path, username/user_agent не IoC.
URL допускается только для artifact.observed, без userinfo/query/fragment;
чувствительные компоненты приводят к исключению всего URL, а не к созданию
потенциально действующего усечённого URL. URL honeytoken не извлекается.
Источник artifact URL считается доверенным структурированным наблюдением;
секреты в path неразличимы автоматически, поэтому принимаются только URL с
пустым путём либо путём / (остальные не экспортируются в MVP).

Столбцы: indicator_type,indicator_value,occurred_at,agent_id,event_id,deployment_id,session_id.
Уникальность: type+normalized value+agent_id+event_id. Повторы в других событиях
сохраняются. Сортировка occurred_at, agent_id, event_id, type, value ASC.
Один committed SQL snapshot для подсчёта/выгрузки; весь CSV формируется до ответа.
100000 разрешены, 100001 — 422 без частичного файла. UTF-8 BOM, comma, CRLF,
CSV quoting; все строковые ячейки с ведущим =,+,-,@ либо whitespace/control
перед ними получают апостроф. Имя honeyforge-iocs-YYYYMMDDTHHMMSSZ.csv, attachment.

## auth.bruteforce

Единственное правило: version=1, severity=high, state=open. auth.attempt с
data.success строго false и известным source.ip. Порог 5 уникальных событий.
Фиксированная минута UTC occurred_at [start,start+60s). DB UNIQUE группа:
rule/version/deployment/normalized source_ip/start. Успешный вход не влияет.
Поздние события обновляют историческую группу. Повторы и рестарт идемпотентны:
обработка и publication+outbox в одной транзакции, UNIQUE event identity.
created_at устанавливается при достижении порога и неизменен; updated_at — время
обновления. session_id только если все события имеют одну известную сессию.
count точный; первые 100 event_refs по agent_id,event_id ASC, truncated=count>100.
Атака на границе минут может не достичь порога. Нет acknowledge/resolve,
скользящих окон и объединения соседних групп.
Пагинация алертов created_at/id DESC слабая: не фиксирует изменяемые поля и
соответствие фильтрам, version курсора не создаёт исторический snapshot.

## История, ссылки и ошибки

История и timeline используют stream_seq<=W и отдельную исключительную границу
следующей страницы. Timeline: session_id, sort=occurred_at&order=asc;
история sort=received_at&order=desc. Другие комбинации 422. Новые вставки,
включая поздние, видны только в новом снимке. Гарантия не распространяется на
артефакты, токены и алерты. Сброс только по 422 INVALID_CURSOR, не по ошибке
фильтров. TTL 24ч остаётся предложением. Lookup POST /api/v1/events/lookup
до 100 пар, cookie+CSRF+Origin, тот же маскированный EventV1; недоступные пары
возвращаются отдельно. Исходный proposal count=7 с одной ссылкой требует true.

## Критерии приёмки

Матрица ролей через HTTP; source object/null/missing/invalid через agent→DB→
publication→REST/WS; история/timeline/IP/top; durable token identity и секреты;
каждое IoC поле, интервалы/31 день/лимит/snapshot/формат; алерт 4/5/6,
группы/UTC/граница/поздние/повтор/рестарт/конкуренция/сессии/100/101 ссылок.
Обязательные PostgreSQL проверки выполняются в honeyforge_test, миграция
сохраняет control и прежние события. Сборки/type/lint/schema/frontend checks
и три независимых рецензента фиксируются в отчёте до push.

Допустимый occurred_at после нормализации в UTC должен быть меньше
9999-12-31T23:59:00Z: конец его минутного окна обязан представляться RFC3339
и типом datetime. Переполнение при переводе в UTC также недопустимо.
Агент отклоняет такое событие; backend возвращает INVALID_EVENT до сохранения.
Публичная проекция принимает только строки для текстовых полей и boolean
для success/truncated; вложенные произвольные структуры не выдаются.
