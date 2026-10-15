# Ответ frontend: контракт интеграции v1

Уточнение владельца backend от 09.10.2026, дополнено согласованным MVP
расследования 11.10.2026: [investigation-mvp.md](investigation-mvp.md).
Общий OpenAPI генерируется из рабочего приложения. События/stream/lookup и
маршруты расследования подключены. Replay завершённых Cowrie TTY реализован;
паспорт сессии пока не реализован. Точная памятка клиенту: [investigation-handoff.md](investigation-handoff.md).

## Файлы для генерации

- contracts/openapi.json: control, auth, агенты, события и расследование.
- contracts/event-v1.schema.json: нормализованное событие для UI, не входной batch агента.
- contracts/replay-chunk-v1.schema.json: текстовый replay v1.

agent_id сейчас совпадает с host_id (один агент на узел); instance_id вводится
runtime вместе с жизненным циклом. Cowrie source session ID нормализует адаптер,
а UUID сессии в API не должен меняться при повторной доставке.

## Stream

WS /api/v1/stream — live-поток без исторической пагинации; параметр after в WS v1
не используется. Это уточняет черновой HTML-план, где он рассматривался.
Первое сообщение: {"type":"hello","watermark":"120"}.
Следующие: {"type":"event","stream_seq":"121","event":{...EventV1}}.
stream_seq и watermark — десятичные строки неотрицательного bigint, не Number.
watermark — максимальный опубликованный и committed stream_seq; пустой журнал — "0".
Внешний event_id не дублируется. Live передаёт записи строго после hello.watermark.
Сервер исключает гонку между подпиской и получением watermark: журнал PostgreSQL —
источник истины, одних pub/sub сообщений недостаточно.

GET /api/v1/stream-events?after=100&until=120&limit=100
Ответ: {"items":[{"stream_seq":"101","event":{...}}],
"watermark":"120","next_cursor":"101"}.
next_cursor=null, когда достигнут until; watermark остаётся тем же на всех страницах.
Если until не передан, сервер фиксирует watermark в первом ответе; клиент передаёт
его как until на следующих страницах. after исключительный, until включительный.
Страницы отсортированы по stream_seq ASC, limit 1..500, по умолчанию 100.
При next_cursor != null он становится следующим after.

Алгоритм: открыть WS -> hello(W) -> буферизовать live >W -> REST догрузить
(last_processed,W] -> на последней странице сохранить W -> слить live по порядку.
Дубли удалять по stream_seq, запись события в store идентифицировать парой
(agent_id,event_id). Если буфер переполняется, повторить синхронизацию, не пропускать
данные молча. История событий /events — отдельная пагинация, не журнал stream.
Курсор старше retention или неизвестный/будущий until даёт явную ошибку (410 для
утраченной истории, 422 для неверного значения); клиент выполняет полный resync.

## Сессия

HttpOnly cookie hf_session; Secure в production, SameSite=Lax. login и auth/me
возвращают {user:{id,username,role},csrf_token}. POST/PATCH/DELETE требуют cookie,
X-CSRF-Token и доверенный Origin. Исключение: login ещё не имеет CSRF-токена,
но требует доверенный Origin; агентские ручки используют Bearer вместо cookie. fetch: credentials='include'. Origin не задаётся
вручную из браузера; согласовать настоящий dev origin (пока http://localhost:5173).
WS отправляет cookie; проверяет Origin и сессию, принимает только чтение.
При обнаружении истечения/отзыва открытый WS закрывается 4401, запрет доступа — 4403.
Сервер проверяет валидность перед отправкой данных и периодически при простое.
Чтобы передать custom close code при начальной ошибке авторизации, сервер может
принять соединение и сразу закрыть его без данных. Сетевой/прокси отказ до handshake
может дать 1006: клиент вызывает auth/me, на 401 возвращает пользователя ко входу,
на сетевой ошибке использует ограниченный reconnect с backoff. Бесконечного retry
при 4401/4403 нет.

## Replay

GET /api/v1/sessions/{id}/replay?after=opaque_cursor&limit=200
Ответ: {session_id,started_at,chunks:[ReplayChunkV1],next_cursor,tail_cursor,complete}.
started_at — RFC 3339; offset_ms — время от старта сессии; направление in/out.
data_base64 содержит исходные байты. TextDecoder работает потоково отдельно для
направлений, а не заново на каждом чанке: UTF-8 может пересекать границу чанков.
truncated=true означает недостающие данные; показывать разрыв и сбросить состояние
декодера на границе разрыва. next_cursor=null означает конец доступных сейчас
чанков. complete=false — запись ещё идёт: при next_cursor=null следующий опрос
использует after=tail_cursor. tail_cursor — непрозрачный курсор последнего
доступного чанка, null для пустой записи; при null параметр after не передаётся.
limit 1..500. Порядок seq ASC; большие записи выдаются страницами. MVP — безопасный
текстовый replay: точная геометрия resize не обещается. OSC/HTML/URL не исполняются.

Для Cowrie выдача начинается после публикации session.closed с manifest всех
TTY и получения непрерывной последовательности чанков каждой записи. Пока доставка
не закончена, chunks пуст и complete=false. После этого seq назначается по общему
времени чанков всех каналов; порядок закрытия каналов на replay не влияет.
Входные capture_seq и recording_id остаются внутренними полями Event.data.replay.
Курсор подписан и привязан к сессии; неверный курсор — 422 INVALID_CURSOR,
отсутствующая опубликованная сессия — 404 SESSION_NOT_FOUND, конфликт seq —
409 REPLAY_CONFLICT, более 10000 опубликованных событий — 413 REPLAY_LIMIT.
Пароли аутентификации не включаются. Команды и вывод после входа доступны ролям
reader/operator/admin; строка, введённая пользователем уже в shell, может содержать
секрет и остаётся частью записи. Произвольный JSON события через replay не выдаётся.

## Список событий

GET /api/v1/events?deployment_id=UUID&from=RFC3339&to=RFC3339&source_ip=IP&type=TYPE&cursor=OPAQUE&limit=50
Ответ {items:[EventV1],next_cursor:null|OPAQUE}. limit 1..200, default=50.
from включительно, to исключительно; фильтр по occurred_at, при from>=to — 422.
Порядок received_at DESC, event_id DESC, agent_id DESC; cursor непрозрачный,
содержит watermark stream_seq<=W и отдельную границу страницы, связан с фильтрами.
При изменении фильтра клиент начинает первую страницу. Несовместимый cursor —
422 INVALID_CURSOR; ошибки фильтров не вызывают автоматического сброса. Новые события приходят
по WS, а пагинация текущего снимка не сдвигается при новых вставках.

source — обязательный endpoint прежнего формата или null. Timeline использует
session_id и sort=occurred_at&order=asc, границу occurred_at/event_id/agent_id ASC;
история — sort=received_at&order=desc. Другие комбинации 422. Поздние вставки
не входят в снимок. Эта гарантия не применяется к изменяемым алертам/токенам/
артефактам. Их keyset пагинация слабая; TTL курсора 24ч не утверждён.

## Ошибки

Бизнес-ошибка: {"detail":"Сообщение"}. Валидация 422:
{"detail":[{"loc":["body","name"],"type":"...","msg":"..."}]}.
Поле input/context не возвращается, чтобы не раскрывать secrets. Клиент
различает строку и массив, работает по HTTP-статусам, не по тексту.
401 -> вход, 403 -> права/CSRF, 404 -> нет объекта, 409 -> конфликт версии/порта,
410 -> полный resync stream, 422 -> валидация, 429 -> задержка повторного входа,
503 -> сервис временно не готов. Ошибки reverse-proxy могут быть не JSON;
frontend должен иметь fallback по статусу.

## Внутренний источник будущего stream, 10.10.2026

Неподключённое PostgreSQL-ядро предоставляет committed watermark и чтение
publication (after,until], а также snapshot <=W с keyset по
received_at/event_id/agent_id DESC. Канонические байты, digest и внутренний PK
не являются UI DTO. HTTP/WS, курсоры и публичная маскирующая проекция остаются
отдельной интеграцией. Готовность ядра не означает доставку Low → UI.
