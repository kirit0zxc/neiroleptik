# Backend → frontend: MVP расследования

Источник типов: `contracts/openapi.json`; публичное событие:
`contracts/event-v1.schema.json`. Семантика: [investigation-mvp.md](investigation-mvp.md).
Реализация маршрута не означает развёртывание сервера или готовность всего D3.

## Реализовано в рабочем приложении

Все read маршруты требуют hf_session (без неё 401). Viewer/operator/admin читают
метаданные, основания, token, alert и CSV. POST lookup требует также Origin и
X-CSRF-Token. Только operator/admin скачивают содержимое; viewer получает 403.

| Маршрут | Параметры/тело | Ответ |
|---|---|---|
| GET /api/v1/events | deployment_id, session_id, source_ip, type, from, to, sort, order, cursor, limit | EventPage |
| GET /api/v1/stream-events | after, until, limit | CatchUpPage |
| WS /api/v1/stream | cookie+Origin | hello, затем event; committed журнал |
| POST /api/v1/events/lookup | `{refs:[{agent_id,event_id}]}` 1..100 уникальных пар | `{items:[EventV1],unavailable:[EventRef]}` |
| GET /api/v1/artifacts | session_id, deployment_id, cursor, limit | ArtifactPage |
| GET /api/v1/artifacts/{id} | UUID | ArtifactRead |
| GET /api/v1/artifacts/{id}/content | UUID | attachment application/octet-stream |
| GET /api/v1/honeytokens | deployment_id, cursor, limit | HoneytokenPage |
| GET /api/v1/honeytokens/{id}/triggers | cursor, limit | TriggerPage; неизвестный id 404 |
| GET /api/v1/alerts | deployment_id, state, severity, cursor, limit | AlertPage |
| GET /api/v1/iocs.csv | обязательные from/to RFC3339, optional deployment_id | attachment UTF-8 BOM CSV |
| POST /agent/v1/events | AgentBatch, Bearer | AgentAck после commit |

Параметры расследования: limit 1..200/default50, конверт `{items,next_cursor}`,
курсор непрозрачный. Stream catchup limit 1..500/default100. UUID поля обязательны,
nullable отмечены ниже; время RFC3339. Названия path переменных в OpenAPI могут
отличаться от краткого `{id}` в таблице, URL строится по значению UUID.

- EventV1.source обязателен: `{ip:string,port:integer 0..65535}` либо null.
  Прочие поля endpoint не менялись. source=null не подходит source_ip фильтру,
  не создаёт IP IoC/алерт. session_id также nullable. Пароль удалён из data;
  auth.attempt возвращает password_present:boolean. Token событие выдаёт только token_id.
- ArtifactRead: id, session_id?, deployment_id, state, name, source_url?,
  size_bytes?, sha256?, mime?, observed_at, expires_at?, error?, event_refs,
  event_refs_truncated. `?` здесь означает обязательное nullable-поле, а не optional.
  state enum pending/available/failed/expired. Содержимое доступно только available;
  pending/failed →409 ARTIFACT_PENDING/ARTIFACT_FAILED; expired →410 ARTIFACT_EXPIRED.
  Без содержимого даже available возвращает 409 ARTIFACT_PENDING. UI не делает preview.
- HoneytokenRead: id, deployment_id, kind="url", label, created_at,
  last_triggered_at:null|date-time, trigger_count:integer>=0. Label наблюдаемого
  токена — его id; никакого секретного значения или рабочего URL. Trigger:
  at, source:null|endpoint, session_id:null|UUID, event_refs ровно из одной пары.
- AlertRead: id, rule="auth.bruteforce", rule_version:integer>=1,
  severity="high", state="open", title, window:{from,to}, count:integer>=5,
  deployment_id, session_id:null|UUID, source_ip:string, created_at, updated_at,
  event_refs (до100), event_refs_truncated:boolean. До порога запись отсутствует.
  Count не обязан равняться числу ссылок: true при >100. Ссылка — всегда полная пара.
- CSV типы ipv4/ipv6/url/sha256. From/to [from,to), 0<период<=31дня.
  Нет молчаливого периода. 100000 строк максимум, превышение 422 до файла.
  URL политика консервативна: только artifact.observed.data.source_url, без
  credentials/query/fragment и секретных path (в MVP path только пустой или `/`).
  Username/user_agent и произвольный payload не являются источниками индикаторов.

History sort=received_at&order=desc; timeline sort=occurred_at&order=asc, обычно
с session_id. Только эти пары разрешены. Snapshot — stream_seq<=W плюс отдельная
граница следующей страницы; поздние вставки видны в новом снимке. Alert/token/
artifact страницы имеют слабую гарантию, изменяемые значения могут обновляться.
Сбрасывать курсор только по 422 code=INVALID_CURSOR. Ошибка фильтра — обычная422,
не повод автоматического сброса. Ошибки сохраняют detail:string|ValidationIssue[].

## Необходимые изменения клиента

Frontend main всё ещё задаёт source как ненулевой объект; sourceLabel безопасно
обрабатывает неизвестный источник, но mocks/handlers напрямую используют source.ip.
Нужно обновить тип, фильтры/топ моков и тесты null. Viewer кнопку content скрыть.
Lookup использовать refs; пароли показывать по password_present. Генерировать
только утверждённые DTO из общего OpenAPI, оставшиеся x-added не переносить.
Сборка отдельного фронта сама по себе не доказывает совместимость нового сценария.

## Согласованное поведение с оставшимися границами

Metadata токенов и pending артефактов выводятся из committed событий. Нет
провижининга URL токена в runtime и сборщика, который скачивает/сохраняет файлы:
available content заполняется доверенным внутренним процессом. Эти интеграции
не называются реализованными. Нет production Low mapper, полного session/replay
и HTTP analytics/summary. Внутренний top_sources исключает null и проверен отдельно.

## Открытые предложения

TTL курсора24ч, прочие x-added исходного proposal, расширенные виды honeytoken,
другие правила/состояния/severity, acknowledge/resolve, скользящее окно, полнота
исторического снимка изменяемых сущностей. Атака на границе минут может не дать
пять событий ни в одном окне. Не утверждены этим MVP.
