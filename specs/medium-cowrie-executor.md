# Medium Cowrie: локальный стенд Windows + Docker Desktop

Объём и контракт согласованы владельцем 15.10.2026: настраиваемый SSH-профиль,
start/stop/reset, постоянная доставка событий и текстовый replay. Серверное
развёртывание, публичная экспозиция и полноценная сетевая изоляция — отдельный этап.

## Профиль и жизненный цикл

Medium включается явно через --medium-image. Образ собран из закреплённого Cowrie
3.1.1. Профиль содержит один SSH-сервис, порт 1..65535, hostname, 1–8 credentials.
Необязательный banner — печатный ASCII с префиксом SSH-2.0-. Пароли сопоставляются
буквально, включая Unicode и символы правил Cowrie. Неподдерживаемая конфигурация
даёт UNSUPPORTED_PROFILE до Docker run.

Конфигурация сохраняется в medium-config/<deployment>/<instance>/profile.json
рядом с корнем medium-журналов. Файл неизменяем и монтируется read-only; пароли
не попадают в argv/env или repr. Каталог доступен только оператору и Cowrie.
Состояние executor SQLite не содержит credentials. Конфигурационные файлы и raw
журналы могут содержать секреты: не публиковать и не добавлять в Git.

Start/reset выделяют новый instance_id; reset останавливает прежний контейнер и
запускает новый с назначенной revision. Общие lease, fencing, generation и durable
ACK сохраняются. После потерянного ACK повтор не создаёт второй контейнер.
Неопределённое исполнение восстанавливается наблюдением сохранённого UUID.
Stop сохраняет журналы, а успешные прежние start/reset остаются bindings сборщика.
Low reset по-прежнему не поддерживается.

Контейнер непривилегированный, read-only rootfs, CPU/RAM/PID limits, без Docker
socket. Bind только loopback; отдельная Docker bridge network обязательна.
SSH forwarding и SFTP отключены. Bridge не блокирует весь исходящий трафик;
этот режим не предназначен для публичного размещения.

## События и replay

JSON Cowrie преобразуется в существующий Event v1. Фактический порт читается из
конфигурации instance; старые записи MVP без конфигурации используют 2222.
TTY начинается после аутентификации: login password в replay не включается.
Записанные shell-команды и ответы доступны авторизованным reader/operator/admin.

Завершённый TTY разбирается по формату Cowrie 3.1.1. Каждый session.output содержит
только data.replay: seq, capture_seq, recording_id, offset_ms, direction,
data_base64, truncated, started_at, final. recording_id — UUID log.closed;
внутренний seq = JSON offset * 8192 + capture_seq. UUID события детерминирован.
SQLite queue и checkpoint фиксируются атомарно; за шаг читается до 100 чанков,
поэтому большой TTY не блокирует отправку уже накопленных событий.

session.closed содержит replay_recordings — manifest записей этой SSH-сессии.
GET /api/v1/sessions/{id}/replay выдаёт данные только после публикации полного
manifest и всех последовательных чанков. До этого complete=false и chunks=[].
Это закрывает гонку поздней доставки за tail_cursor. Несколько SSH-каналов
сортируются по абсолютному времени каждого чанка, затем получают публичный seq.
Пароль входа и произвольные data-поля не открываются этой ручкой.

Ограничения: raw-файл 64 MiB, кадр 1 MiB, чанк 4096 байт, до 4000 чанков плюс
финальная отметка, до 128 TTY в manifest, до 10000 опубликованных событий сессии.
Обрезанный хвост или предел записи создаёт truncated=true и видимый разрыв.
complete означает завершённую доставку записи, а не отсутствие разрывов.
Повреждённый заголовок/неполный manifest не выдаются за полную запись.
Replay текстовый, без точной геометрии терминала/resize, доступен после закрытия
сессии; live-лента команд работает во время подключения.

## Запуск

Python 3.12, Docker Desktop в режиме Linux containers, зависимости корня проекта:

```powershell
docker build -f honeypots/medium/Dockerfile -t honeyforge-medium:local .
docker network create honeyforge-medium-local
New-Item -ItemType Directory -Force C:\HoneyForge\medium-private
# HF_ENROLLMENT_TOKEN передаётся отдельно, не хранится в репозитории.
python -m agent.runtime.executor.enroll --center https://CENTER --name medium-demo --medium --token-file C:\HoneyForge\medium-private\agent.token
python -m agent.runtime.executor --center https://CENTER --token-file C:\HoneyForge\medium-private\agent.token --state C:\HoneyForge\medium-private\executor\state.sqlite3 --journal-root C:\HoneyForge\journals --medium-image honeyforge-medium:local --network honeyforge-medium-local --telemetry
```

Для центра на том же ПК: --center http://127.0.0.1:8000 --allow-http-loopback
в обеих командах. State, очередь рядом со state, journals и medium-config сохранять
вне checkout. При обновлении их не очищать. Агент должен работать постоянно.
Для UI без моков включить VITE_API_MOCK=0, VITE_EVENTS_ENABLED=1,
VITE_INVESTIGATION_ENABLED=1 и VITE_REPLAY_ENABLED=1.

В UI создать Medium-профиль, например SSH 2224, hostname accounting-server,
banner SSH-2.0-OpenSSH_9.6 и тестовые credentials. Назначить узлу с medium.
В Парке нажать Запустить, дождаться работает; подключиться
`ssh -p 2224 root@127.0.0.1` с паролем профиля, выполнить whoami и выйти.
В Событиях открыть SSH-сессию и replay. Сбросить ловушку создаёт новый instance;
Остановить закрывает порт, сохраняя события и replay прежней сессии.

## Проверка и ограничения

15.10.2026 реальный Windows/Docker smoke: пользовательский порт/hostname/banner,
Unicode-пароль со спецсимволами, whoami, 27 доставленных событий и 16 replay-чанков,
reset с новым instance_id и stop. Запуск кнопкой UI также проверен с настоящим
SSH и автоматической доставкой команды. Автотесты покрывают lost ACK, checkpoint
после рестарта посреди TTY, отсутствие дублей, поздние чанки, пагинацию, авторизацию
и обратный порядок закрытия параллельных SSH-каналов.

На Linux потребуются права UID/GID 999 на выделенные каталоги; приёмка заявлена
для Windows. Общая квота диска, ротация/retention, внешний архив и production
firewall ещё не входят в этот стенд. Docker Desktop может показывать адрес шлюза
вместо реального IP клиента. High этим изменением не реализуется.
