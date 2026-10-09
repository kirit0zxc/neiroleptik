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

Идентификаторы host/deployment/profile revision уже определены. Подход Event v1,
ACK и серверного outbox принят backend с уточнениями 09.10.2026:
обязательный index каждого rejected, ровно один результат на входной элемент,
канонизация оригинала до UI-маскирования, Bearer identity и UNIQUE в PostgreSQL.
Локальный ACK/карантин реализуется в agent; серверные таблицы и endpoints ещё
не добавлены. Точный контракт и сценарии сбоев — specs/events.md.
Согласовать instance_id/lifecycle с Данилом и миграции с backend.
Добавлять серверный модуль внутрь backend/src/honeyforge,
использовать общие Settings, Session и отдельную агентскую авторизацию.
Агент должен писать только за свой host/deployment по проверенному токену.
ACK после commit event+outbox. Один worker атомарно пишет уникальную publication
и processed outbox; WS, REST и watermark читают committed журнал публикаций.
Потеря ACK после commit, конфликт среди хороших элементов, конкурентные повторы
и сбои worker до/после commit требуют будущих PostgreSQL integration-тестов.

## Что пользователь может отправить коллегам сейчас

«Бэкенд HoneyForge — FastAPI в backend/src/honeyforge. Начинаем с auth, профилей,
регистрации узлов и planned deployments. Общий OpenAPI в contracts/openapi.json.
Фронтенду нужны cookie + CSRF; runtime — отдельный Bearer token. Перед реализацией
стыков сверьте integration-handoff.md и передайте мне ваши origin/capabilities/Event v1».

Этот документ не означает, что сообщение уже отправлено участникам.
