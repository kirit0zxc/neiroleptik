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
назначения с generation/applied_revision/instance_id. planned не подтверждает
применение. Профили имеют строгие сервисы и один шаблон High.
Передать backend реальные capabilities, названия драйверов и контракт результата.
Протокол команд согласован 10.10.2026: specs/commands.md и contracts/openapi.json.
claim → renew → ack; lease 60 с, renew каждые 20 с, общий deadline не продлевается.
При повторе command_id использовать сохранённый результат, не повторять запуск/reset.
После потери lease повторный claim даёт новый token для той же команды; старый ACK
отклоняется. После общего таймаута unknown устраняется подтверждённым stop/reconcile.
Нельзя принимать конфигурацию из GET config за команду автоматического запуска.
Данил реализует локальный журнал, проверку поколения и runtime executor отдельно.
Ограничения Low (один tcp + один http, печатный ASCII TCP-баннера), согласованные
в обсуждении runtime, пока не добавлены в валидатор профиля: это следующий
отдельный участок. Этот PR добавляет именно транспорт команд и результат применения.

## Подключение команд на frontend (Диана)

POST /api/v1/deployments/{id}/commands: command start/stop/reset, UUID
idempotency_key, expected_generation из последнего DeploymentRead → 202 job_id.
Повтор сетевого запроса сохраняет все три поля; новый ключ означает новое действие.
GET /jobs/{id} опрашивается до succeeded/failed/expired. lease token в UI не выдаётся.
DeploymentRead теперь включает реальный уровень, desired/applied revision,
generation и instance_id. Статусы чтения уже подключены; реальные кнопки пока
не включены. apply_config/rotate_credentials по-прежнему только демо-превью.
unknown показываем как «состояние не подтверждено», без start/reset.

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
