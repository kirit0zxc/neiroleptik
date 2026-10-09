# Control plane v1: первый рабочий этап

Согласовано владельцем 09.10.2026 до реализации. Контракт HTTP сохраняется в
contracts/openapi.json. Ошибки имеют detail; validation errors FastAPI — 422.
detail — строка либо список loc/type/msg; оба варианта описаны в OpenAPI ErrorResponse.
Текстовые поля отклоняют NUL и некорректный Unicode до записи в PostgreSQL.
Все даты UTC, UUID идентифицируют записи. Списки имеют limit/offset с верхним пределом.

## Авторизация

CLI создаёт пользователей admin/operator/viewer без публичной регистрации.
POST /api/v1/auth/login принимает username/password JSON и доверенный Origin.
Возвращает пользователя и csrf_token; случайный session token хранится в HttpOnly
cookie hf_session, а в БД только SHA-256. Срок сессии 8 часов.
GET /api/v1/auth/me возвращает текущего пользователя и csrf_token после перезагрузки UI.
POST /api/v1/auth/logout отзывает сессию. Изменения требуют X-CSRF-Token и Origin.
admin управляет регистрацией агентов; operator/admin редактируют профили и
создают deployments; viewer только читает. CORS допускает только заданные origins.
Попытки входа ограничиваются в PostgreSQL по IP: 20 за 5 минут, включая неверные.
Это базовая защита MVP, а не распределённая система предотвращения атак.

## Профили

POST /api/v1/profiles создаёт name/description/config и revision=1.
PATCH /profiles/{id} требует expected_revision; конфликт даёт 409.
Изменение создаёт новую immutable revision; GET /profiles/{id}/revisions показывает
историю. DELETE архивирует профиль, новые назначения запрещены; существующие
deployments продолжают ссылаться на прежнюю revision.

config содержит interaction_level, services (protocol/port/banner), hostname,
logging (max_payload_bytes), channel (flush_interval_ms/batch_size),
credentials и high (template/ttl_seconds/cpu/memory_mb) только для High.
Поддерживаемые сервисы: Low tcp/http, Medium ssh, High ssh. Порты внутри профиля
уникальны; High использует только ubuntu-ssh-v1 и один SSH-сервис.
Medium/High требуют хотя бы одни приманочные credentials.

Credentials входа профиля шифруются Fernet ключом окружения. Общий read API
возвращает только usernames, никогда пароль. При изменении config credentials=null
сохраняет прежние credentials; [] явно очищает (для Medium/High это недопустимо).
Передача непустого списка полностью заменяет credentials. Настоящие пароли
пользователей хешируются Argon2 и не являются decoy credentials.

## Узлы и агенты

POST /api/v1/enrollments (admin) выдаёт одноразовый token с TTL 15 минут.
POST /agent/v1/enroll принимает token, name и capabilities, возвращает host_id и
agent_token один раз. Повтор/истечение token отклоняются. Секреты в БД хешированы.
POST /agent/v1/heartbeat обновляет last_seen; GET /api/v1/hosts показывает
online (не старше 40 секунд), offline либо revoked. Возможности узла — заявленные,
не доказательство KVM/изоляции. POST /api/v1/hosts/{id}/revoke (admin) отзывает token.
Агент никогда не получает права пользователя control API.

## Назначения

POST /api/v1/deployments (operator/admin) принимает host_id, profile_id и revision.
Создаёт planned и резервирует порты узла. Конкурирующее назначение того же порта
даёт 409. Совпадение capabilities проверяется; отозванный узел не используется.
GET /deployments и /deployments/{id} доступны читателям. Управление lifecycle
и освобождение портов добавятся вместе с подтверждённым stop runtime на D2.
GET /agent/v1/config выдаёт агенту только назначения его узла и расшифрованные
credentials для этих revision. Тело конфигурации не логировать. Endpoint требует TLS
на сервере; dev-only loopback HTTP разрешён только для локальной разработки.

## Аудит и проверки

Профиль/назначение/регистрация/отзыв/вход/выход записываются в audit_events без
паролей и токенов. GET /api/v1/audit доступен admin. Запись операции и аудит
фиксируются одной транзакцией, где это применимо.

Приёмка: PostgreSQL migrations upgrade/downgrade; роли и CSRF; сессии/expiration;
immutable revisions и конфликт версий; архивирование; единственность enrollment;
чужие/отозванные agent tokens; собственный config; конфликт портов;
health/readiness; воспроизводимый OpenAPI и CI. Это не тест совместной работы с
реальными ловушками: их пока нет в main.
