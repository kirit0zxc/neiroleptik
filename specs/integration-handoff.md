# Что передать участникам HoneyForge

## Frontend (участник 1)

Основа — contracts/openapi.json. База /api/v1. Использовать credentials: include.
POST login: Origin вашего frontend должен быть в HF_ALLOWED_ORIGINS; из ответа
сохранить csrf_token в памяти. После refresh получить GET auth/me. Каждый POST,
PATCH, DELETE пользователя: X-CSRF-Token и cookie. Не хранить session token вручную.
Профиль возвращает credential_usernames, паролей в read API нет. Для сохранения
паролей при PATCH config отправлять credentials: null; для замены — новый список.
Отображать planned как «Ожидает применения», не running. На 409 revision обновить
профиль и предложить пользователю повторно согласовать изменения.

Согласовать с владельцем backend: origin dev UI, генератор TS-типов, нужны ли
дополнительные поля. Не создавать отдельный несовместимый контракт в моках.

## Runtime (участник 3)

Одноразовая регистрация /agent/v1/enroll -> отдельный agent_token. Затем
Authorization: Bearer; heartbeat каждые 10 секунд. /agent/v1/config только читает
назначения, но пока не включает команды/ACK применения. До интеграции D2 нельзя
считать revision применённой. Профили имеют строгие сервисы и один шаблон High.
Передать backend реальные capabilities, названия драйверов и контракт результата.
Согласовать командный lease/TTL/generation до реализации agent command executor.

## Telemetry (участник 4)

Идентификаторы host/deployment/profile revision уже определены. Сессии и события
пока не добавлены: согласовать Event v1, instance_id, таблицы events/outbox,
accepted_ids/rejected и stream_seq. Добавлять модуль внутрь backend/src/honeyforge,
использовать общие Settings, Session и отдельную агентскую авторизацию.
Агент должен писать только за свой host/deployment. ACK после commit.

## Что пользователь может отправить коллегам сейчас

«Бэкенд HoneyForge — FastAPI в backend/src/honeyforge. Начинаем с auth, профилей,
регистрации узлов и planned deployments. Общий OpenAPI в contracts/openapi.json.
Фронтенду нужны cookie + CSRF; runtime — отдельный Bearer token. Перед реализацией
стыков сверьте integration-handoff.md и передайте мне ваши origin/capabilities/Event v1».

Этот документ не означает, что сообщение уже отправлено участникам.
