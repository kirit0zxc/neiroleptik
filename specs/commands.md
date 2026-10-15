# Команды центра и подтверждение применения v1

Согласовано владельцем проекта после обсуждения с Данилом 10.10.2026.
Область этого этапа: центр FastAPI и PostgreSQL. Исполнитель агента — Данил;
ingest/outbox/WS — Дима. Реальные ловушки и аренда сервера в этот PR не входят.

## Пользовательский API

`POST /api/v1/deployments/{id}/commands` (operator/admin, cookie + CSRF):
`{command: "start"|"stop"|"reset", idempotency_key: UUID, expected_generation: integer}`.
Ответ `202 {job_id}`; `GET /api/v1/jobs/{id}` доступен читателю:
`{job_id, deployment_id, command, state, error, error_code, generation,
created_at, completed_at}`. Job ID одновременно является глобальным command_id.
Состояния job: requested → leased → running → succeeded/failed/expired.

Generation — счётчик принятых команд deployment, начиная с 0. Создание новой
команды атомарно увеличивает его на 1. expected_generation пользователя должен
совпадать с текущим значением, иначе 409 STALE_GENERATION. Повтор той же пары
(deployment_id, idempotency_key) и того же тела возвращает прежний job_id даже
после завершения; изменённое тело — 409 IDEMPOTENCY_CONFLICT. Ключ не разделяется
между deployments. На deployment допускается одно незавершённое задание.

start: planned/stopped/failed; stop: running/failed/unknown; reset: Medium/High,
running/failed. Архивный профиль и неподдерживаемый уровень не запускаются.
В этом этапе apply_config/rotate_credentials не реализованы.
Команды всегда используют уже назначенную неизменяемую revision; изменение
самого профиля не меняет конфигурацию работающего deployment автоматически.

DeploymentRead сохраняет id/host_id/profile_id/revision/created_at и добавляет
interaction_level, desired_revision, applied_revision, generation, instance_id.
Состояния: planned/deploying/running/stopping/stopped/failed/unknown.
Создание deployment по-прежнему не запускает процесс. running появляется только
после успешного ACK start/reset. Порты остаются зарезервированными и после stop:
назначение сохраняется для повторного запуска; удаление назначения — отдельный этап.

## Агентский API

Все запросы — собственный Bearer token узла, без пользовательской cookie/CSRF.
host_id агент не задаёт. Авторизация повторно проверяется под блокировкой узла.
Чужой command_id возвращает 404, отозванный token — 401.

`POST /agent/v1/commands/claim` принимает `{}` либо `{command_id: UUID}` для
восстановления известной команды. Ответ `200 {command: null}` при пустой очереди,
иначе `{command: {command_id, deployment_id, action, profile_id, revision,
expected_generation, generation, expected_instance_id, reconcile, config, lease_token,
lease_expires_at, execution_expires_at, delivery_expires_at}}`.
config содержит секреты только назначенного этому узлу профиля: её нельзя логировать.
В MVP один узел получает одно активное задание за раз. Повтор claim при действующем
lease возвращает ту же команду и token, не продлевая срок молча.

`POST /agent/v1/commands/{command_id}/renew`: `{lease_token}` →
`{command_id, lease_expires_at, execution_expires_at}`. Первое renew переводит job
в running (выполняется операция), но не deployment в running. Lease 60 секунд,
рекомендуемое продление каждые 20 секунд; heartbeat — отдельно, каждые 10 секунд.

Первое получение возможно в течение 300 секунд после создания. Общий срок
выполнения с первого claim — 300 секунд Low/Medium, 900 секунд High. Продление и
повторная выдача не сдвигают execution_expires_at. high.ttl_seconds — отдельный
срок жизни VM, не lease и не срок выполнения операции.

После истечения lease тот же command_id может быть выдан повторно с новым token;
старый token больше не допускает renew/ACK. Новое задание для этого deployment
не создаётся. Агент хранит журнал команд на диске и сначала сверяет фактический
экземпляр: повтор не запускает процесс и не сбрасывает VM второй раз. Агент обязан
прекращать изменения после потери lease и сериализовать операции deployment;
один HTTP-протокол не обеспечивает физическую изоляцию или exactly-once execution.

Истечение общего срока завершает job как expired. Если команда уже выдавалась,
deployment становится unknown: центр не знает, завершился ли процесс. start/reset
в unknown запрещены; подтверждённый stop позволяет вернуться к stopped.
Если задание не выдавалось вообще, фактическое состояние остаётся прежним.
Истечения фиксируются при успешных обращениях к jobs/config/deployments/claim.
renew/ACK с истёкшим lease отклоняются; отказ откатывает транзакцию, поэтому
обновление отображаемого состояния фиксируется следующим чтением или claim.
Перед новой командой сроки всё равно проверяются повторно. Worker не требуется.

## Итоговый ACK

`POST /agent/v1/commands/{command_id}/ack`:
`{lease_token, deployment_id, generation, applied_revision: integer|null,
instance_id: UUID|null, outcome: "succeeded"|"failed",
runtime_state: "running"|"stopped"|"failed", error: {code, message}|null}`.
Ответ — JobRead, HTTP 200. Для succeeded ошибка отсутствует; для failed обязательна.
Тексты ошибок краткие, без конфигурации, паролей и токенов.

Успех start/reset требует running, целевую applied_revision и непустой instance_id.
Новый запуск/reset создаёт новый instance_id, не использованный ни в одном
предыдущем экземпляре. История UUID хранится в runtime_instances даже после
неуспешного запуска с известной идентичностью; повторное использование —
INSTANCE_REUSED, принадлежность другому deployment — INSTANCE_CONFLICT.
stop сохраняет идентичность последнего
экземпляра и последнюю применённую revision для истории. Сам рестарт демона агента
instance_id не меняет. Failed ACK не выдаётся за успех применения: он сообщает
наблюдаемое runtime_state; например неудачный stop может оставить running.
applied_revision может быть только revision этого назначения либо null.
ACK обычного stop не вправе менять прежнюю revision/instance_id. Исключение —
stop из unknown: команда содержит reconcile=true, агент сверяет реальный процесс
и может сообщить обнаруженную идентичность того же назначения. Например start
мог завершиться физически, хотя его ACK потерялся. После такой очистки stopped
подтверждает безопасную возможность нового запуска.

Generation ACK должна совпадать с выданной командой. На агенте сначала ищется
command_id в долговечном журнале; для новой команды expected_generation ниже
локального обработанного поколения означает STALE_GENERATION. Пропуски поколений
допустимы (команда могла истечь ещё в очереди). Новая команда имеет generation
больше предыдущего; повтор известной команды возвращает сохранённый результат.

Первый финальный ACK и состояние deployment фиксируются одной транзакцией до ответа.
Нормализованный повтор без lease_token в сравнении возвращает прежний результат;
другое содержимое — 409 ACK_CONFLICT. Повтор уже зафиксированного ACK допускается
после истечения срока, но с token последнего выданного lease. Для незавершённой
команды просроченный lease — 409 LEASE_EXPIRED; неверный token — 409 LEASE_MISMATCH.

## Приёмка

PostgreSQL migration upgrade/check/downgrade; пользовательские роли/CSRF;
чужие и отозванные агенты; конкурентные create/claim/ACK/revoke; стабильный token
при повторном claim; повтор после потери claim/ACK и рестарта приложения;
разное тело при одном ключе/ACK; stale generation; lease renewal/expiry/reclaim;
неизменный hard deadline; unknown после потери связи; невозможность повторного
start в unknown; корректные start→stop→start и High reset с новой идентичностью;
ложный running и чужие revision/instance_id отклоняются; атомарный rollback.
OpenAPI генерируется из приложения. Фронт пока не включает реальные кнопки:
Диана подключает их по этому контракту; чтение состояний уже должно быть корректным.
