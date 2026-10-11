# DTO экранов расследования (frontend ↔ участник 4)

Статус: **предложение контракта, согласовано по составу с участником 4
(10.10.2026)**. Серверных ручек пока нет. После публикации сверяем точные
имена, nullable-поля и пагинацию, затем раздел переносится в
`specs/frontend-protocol-v1.md` и `contracts/openapi.json`. До этой сверки
frontend моков по разделам 1–4 и 6 не делает; сводка (раздел 5) уже в моке
в согласованном виде.

**Обновление 11.10:** разделы 2–4 и 6, выборка событий по ссылкам и
timeline реализованы участником 4 (PR #35): точные типы — в
`contracts/openapi.json`, семантика — `specs/investigation-handoff.md`; ниже
они остаются как история согласования. Паспорт сессии (раздел 1) и сводка
(раздел 5) — в черновике `specs/frontend-openapi-proposal.yaml` (proposal).

Общие правила — как в протоколе v1: время RFC 3339 UTC; id — UUID строкой;
ошибки — `{"detail": ...}`; списки — `{items, next_cursor}` с непрозрачным
курсором, связанным с фильтрами и сортировкой (несовместимый курсор — 422);
новые поступления не сдвигают текущий снимок страниц. Данные атакующего —
только строки, frontend показывает их текстом (S1, S2).

`EventRef = {agent_id, event_id}` — пара, по которой событие уникально.
Каждый вывод (алерт, срабатывание токена) ссылается на исходные события
`event_refs`. Стадию атаки сервер пока не классифицирует; Юми может
показывать объяснимую оценку **с основаниями** (`event_refs`), но это не
поле серверной классификации.

## 1. Паспорт сессии (F14) — ок

`GET /api/v1/sessions/{id}` → `SessionRead`:

```json
{
  "session_id": "uuid",
  "agent_id": "uuid",
  "deployment_id": "uuid",
  "instance_id": "uuid",
  "protocol": "ssh | http | tcp",
  "source": {"ip": "203.0.113.9", "port": 40112},
  "service": {"protocol": "ssh", "port": 2222},
  "started_at": "…",
  "ended_at": "… | null",
  "auth": {"attempts": 3, "succeeded": true, "username": "root | null"},
  "counts": {"events": 41, "commands": 12, "artifacts": 1, "tokens_triggered": 0},
  "replay": {"available": true, "complete": false},
  "profile_revision": 4
}
```

- `ended_at: null` не значит «соединение активно»: события закрытия могло
  не дойти. Frontend пишет «окончание неизвестно», а не «идёт».
- Счётчики — по событиям, полученным центром.
- Пароли из попыток входа в паспорт не попадают. 404 — сессии нет.

**Timeline:** `GET /api/v1/events?session_id=UUID&sort=occurred_at&order=asc`
— тот же EventV1. Порядок `occurred_at ASC, event_id ASC, agent_id ASC`.
Отдельный `sort` нужен потому, что обычная история идёт по времени приёма,
а после offline-доставки оно отличается от времени события. По умолчанию —
нынешний порядок истории (`received_at DESC, event_id DESC, agent_id DESC`).
Курсор связан с фильтрами и сортировкой. Клик по событию переводит replay к
моменту `occurred_at − started_at`.

## 2. Артефакты (F16) — ок

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
  "size_bytes": "1532 | null",
  "sha256": "64 hex | null",
  "mime": "text/x-shellscript | null",
  "observed_at": "…",
  "expires_at": "… | null",
  "error": "строка | null",
  "event_refs": [{"agent_id": "…", "event_id": "…"}]
}
```

- `size_bytes` и `sha256` могут быть null до проверки файла; для
  `available` обязательны.
- `GET /api/v1/artifacts/{id}/content` — авторизованное скачивание как
  attachment (`X-Content-Type-Options: nosniff`). `pending`/`failed` —
  409, `expired` — 410. Этот 410 — «файл недоступен», не пересинхронизация
  потока. Frontend не открывает содержимое в браузере.

## 3. Медовые токены (F17) — ок

`GET /api/v1/honeytokens?deployment_id=&cursor=&limit=50` → `HoneytokenRead`:
`{id, deployment_id, kind, label, created_at, last_triggered_at | null,
trigger_count}`. Секретное значение токена API не возвращает.

Срабатывания: `GET /api/v1/honeytokens/{id}/triggers?cursor=&limit=50` →
`{at, source: {ip, port}, session_id | null, event_refs}` — по событиям
`token.triggered`. `session_id` может быть null: одного совпавшего IP для
привязки к сессии недостаточно.

## 4. Алерты (F17) — чтение в MVP

`GET /api/v1/alerts?state=&severity=&cursor=&limit=50` → `AlertRead`:

```json
{
  "id": "uuid",
  "rule": "auth.bruteforce",
  "rule_version": 1,
  "severity": "low | medium | high | critical",
  "state": "open | acknowledged | resolved",
  "title": "5 неудачных входов за 60 с",
  "window": {"from": "…", "to": "…"},
  "count": 7,
  "deployment_id": "uuid",
  "session_id": "uuid | null",
  "source_ip": "203.0.113.9 | null",
  "created_at": "…",
  "updated_at": "…",
  "event_refs": [{"agent_id": "…", "event_id": "…"}]
}
```

- Первое правило: ≥ 5 неудачных входов за 60 с. Область группировки и
  подавление повторов — закрепим отдельно.
- Смена состояния — после MVP.
- Отдельного типа алерта в WS пока нет: обзор и экран алертов опрашивают
  список/сводку раз в 15 с.

## 5. Сводка обзора — согласовано, ручки пока нет

`GET /api/v1/analytics/summary`:

```json
{
  "as_of": "…",
  "deployments": {"total": 4, "running": 3, "failed": 1},
  "hosts": {"total": 3, "online": 2, "offline": 1, "revoked": 0},
  "events_last_hour": 128,
  "alerts_open": 1,
  "top_sources": [{"ip": "203.0.113.9", "count": 40}]
}
```

- `degraded` отложен: центр различает `online / offline / revoked`;
  `revoked` не считается `offline`.
- События и топ-5 источников — по опубликованным событиям с `occurred_at`
  в `[as_of − 1 ч, as_of)`; равные счётчики — стабильный порядок по IP.

## 6. CSV IoC (F18) — серверный файл

Одна строка на индикатор и событие-основание:

```
indicator_type,indicator_value,occurred_at,agent_id,event_id,deployment_id,session_id
```

Без паролей; значения экранируются с защитой от формул CSV. Frontend только
скачивает файл.
