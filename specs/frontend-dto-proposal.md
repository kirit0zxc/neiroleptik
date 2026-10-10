# Предложение DTO для экранов расследования (frontend → участник 4)

Статус: **предложение, не контракт**. Моков и кода по этим DTO frontend не
делает, пока участник 4 не подтвердит или не поправит формат (иначе моки
разойдутся с центром). После согласования раздел переносится в
`specs/frontend-protocol-v1.md` и `contracts/openapi.json`.

Общие правила — как в протоколе v1: время RFC 3339 UTC; id — UUID строкой;
ошибки — `{"detail": ...}`; списки — `{items, next_cursor}` с непрозрачным
курсором, связанным с фильтрами (несовместимый курсор — 422); данные
атакующего — только строки, frontend показывает их текстом (S1, S2).
Каждый вывод (стадия, алерт, срабатывание токена) ссылается на исходные
события `event_refs` — так Юми и экран показывают основание, а не догадку
(F17, «выводы Юми — по доказательствам»).

`EventRef = {agent_id, event_id}` — пара, по которой событие уникально.

## 1. Паспорт сессии (F14)

`GET /api/v1/sessions/{id}` → `SessionRead`:

```json
{
  "id": "uuid",
  "deployment_id": "uuid",
  "instance_id": "uuid",
  "protocol": "ssh | http | tcp",
  "source": {"ip": "203.0.113.9", "port": 40112},
  "service": {"protocol": "ssh", "port": 2222},
  "started_at": "…", "ended_at": "… | null",
  "state": "active | closed",
  "auth": {"attempts": 3, "succeeded": true, "username": "root | null"},
  "counts": {"events": 41, "commands": 12, "artifacts": 1, "tokens_triggered": 0},
  "replay": {"available": true, "complete": false},
  "profile_revision": 4
}
```

- Пароли из попыток входа в паспорт не попадают (только в событиях, если
  профиль их пишет).
- 404 — сессии нет.

Timeline: `GET /api/v1/events?session_id={id}&order=asc&cursor=&limit=200`
— тот же EventV1, что лента. Просим добавить **новые** параметры: фильтр
`session_id` и `order=asc` (по `occurred_at, event_id, agent_id`; по
умолчанию — нынешний `desc`). Курсор связан и с `order`: курсор от `asc` в
запросе `desc` — 422. Нужно, чтобы клик по событию
переводил replay к моменту `occurred_at − started_at` сессии. Альтернатива —
отдельная ручка `/sessions/{id}/events` с тем же форматом.

Вопрос: нужен ли `stage` (стадия атаки) в паспорте сейчас или позже
отдельным выводом с `event_refs`?

## 2. Артефакты (F16)

`GET /api/v1/artifacts?session_id=&deployment_id=&cursor=&limit=50` и
`GET /api/v1/artifacts/{id}` → `ArtifactRead`:

```json
{
  "id": "uuid",
  "session_id": "uuid | null",
  "deployment_id": "uuid",
  "state": "pending | available | failed | expired",
  "name": "x.sh",
  "source_url": "http://203.0.113.9/x.sh | null",
  "size_bytes": 1532,
  "sha256": "64 hex | null",
  "mime": "text/x-shellscript | null",
  "observed_at": "…",
  "expires_at": "… | null",
  "error": "строка | null",
  "event_refs": [{"agent_id": "…", "event_id": "…"}]
}
```

- `size_bytes` и `sha256` — null, пока `state=pending`.
- Скачивание: `GET /api/v1/artifacts/{id}/content` — только при
  `available`, ответ `application/octet-stream` с
  `Content-Disposition: attachment` и `X-Content-Type-Options: nosniff`;
  иначе 409 с `detail`. Frontend не открывает содержимое в браузере.
- Связь с событием — `artifact_ids` в EventV1 (уже есть).

## 3. Медовые токены (F17)

`GET /api/v1/honeytokens?deployment_id=&cursor=&limit=50` → `HoneytokenRead`:

```json
{
  "id": "uuid",
  "deployment_id": "uuid",
  "kind": "credential | api_key | url | file",
  "label": "aws-key в /root/.aws/credentials",
  "created_at": "…",
  "last_triggered_at": "… | null",
  "trigger_count": 2
}
```

Срабатывания: `GET /api/v1/honeytokens/{id}/triggers?cursor=&limit=50` →
`{at, source: {ip, port}, session_id | null, event_refs}`. Само значение
токена в API не возвращается (как пароли).

## 4. Алерты (F17)

`GET /api/v1/alerts?state=open|acknowledged|resolved&severity=&cursor=&limit=50`
→ `AlertRead`:

```json
{
  "id": "uuid",
  "rule": "honeytoken.triggered | artifact.downloaded | auth.succeeded | …",
  "severity": "low | medium | high | critical",
  "state": "open | acknowledged | resolved",
  "title": "Сработал медовый токен aws-key",
  "deployment_id": "uuid",
  "session_id": "uuid | null",
  "source_ip": "203.0.113.9 | null",
  "created_at": "…",
  "updated_at": "…",
  "event_refs": [{"agent_id": "…", "event_id": "…"}]
}
```

- `title` формирует центр; frontend показывает его текстом.
- Смена состояния: `POST /api/v1/alerts/{id}/state`
  `{state, expected_updated_at}` → 200 `AlertRead`; гонка — 409; запись в
  аудит (`alert.state_changed`). Нужен ли этот шаг в MVP — решает участник 4.
- В потоке: просим событие `alert.opened` или отдельный тип WS-сообщения,
  чтобы обзор и Юми реагировали без опроса. Если нет — обзор опрашивает
  summary раз в 15 с (как сейчас в спецификации).

## 5. Сводка обзора

`GET /api/v1/analytics/summary` — спецификация frontend сейчас описывает:

```json
{
  "deployments": {"total": 4, "running": 3, "failed": 1},
  "hosts": {"total": 2, "online": 2, "degraded": 0},
  "events_last_hour": 128,
  "alerts_open": 1,
  "generated_at": "…"
}
```

Расхождение, которое надо снять: в текущем моке `hosts.offline` вместо
`hosts.degraded` и есть `top_sources: [{ip, count}]`, которых нет в
спецификации. Предложение: `hosts: {total, online, degraded, offline}` и
`top_sources` (до 5, за последний час) — оставить, если центр их считает;
иначе убрать из мока. Мок приведём к согласованному варианту.

## 6. CSV IoC (F18)

`GET /api/v1/iocs.csv?from=&to=&deployment_id=` — файл формирует сервер
(`text/csv; charset=utf-8`, `Content-Disposition: attachment`). Колонки на
усмотрение участника 4; frontend только скачивает.

## Что нужно от участника 4

Для каждого раздела: «ок» / правки полей / «не в MVP». После ответа
frontend переносит согласованное в протокол, делает моки по нему и экраны.
