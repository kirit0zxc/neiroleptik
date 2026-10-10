# Исполнитель команд Low

Один агент на каталог состояния и узел. Нужны Python 3.12, зависимости `uv.lock`,
Docker Engine, заранее собранный образ Low, собственный Bearer узла центра.
Узел регистрируется в центре только с capabilities `["low"]`: команды других уровней
исполнитель подтверждает failed ACK `UNSUPPORTED_COMMAND`, не наблюдая ловушку.

## Регистрация

Администратор выдаёт одноразовый enrollment token через `POST /api/v1/enrollments`
(TTL 15 минут). Передайте его через `HF_ENROLLMENT_TOKEN`, затем выполните
`python -m agent.runtime.executor.enroll --center URL --name NAME --token-file PATH`.
Capabilities всегда `["low"]`; запрос enroll не использует Bearer. Новый файл
резервируется до HTTP-запроса, не перезаписывается, на POSIX имеет режим `0600`,
после записи выполняется fsync. Родительский каталог должен существовать и быть
закрыт для других пользователей. Windows ACL устанавливает оператор.

Если ответ регистрации потерян, автоматического повтора одноразового enroll нет.
Администратор сначала проверяет созданный узел и отзывает лишнюю регистрацию,
затем выдаёт новый token. Исполнитель использует ровно один источник Bearer:
`HF_AGENT_TOKEN` или `--token-file`. Права, владелец POSIX-файла, обычный тип файла
и отсутствие ссылок проверяются до чтения. Токены не передаются в аргументах CLI.

Каталог состояния находится на локальном диске (не сетевой том) и принадлежит
пользователю агента. На POSIX каталог должен иметь режим ровно `0700`, файлы SQLite
и `state.lock` — ровно `0600`; ни сам путь, ни его предки не могут быть символической
ссылкой (на Windows — reparse point). Иначе запуск завершается кодом
`UNSAFE_STATE_PATH`. Агент сам создаёт отсутствующий каталог с нужными правами;
каталог, заранее созданный пакетом с режимом `0755`, нужно исправить `chmod 0700`.
SQLite и lock нельзя удалять для обхода конфликта или восстановления.

Часы узла синхронизируются с центром (NTP, расхождение много меньше 60 секунд). Если
часы агента спешат более чем на пять секунд относительно уже истёкшего lease,
процесс останавливается кодом `CLOCK_SKEW` до Docker-операций. При небольшом
истечении повторяет claim, не меняя стадию prepared/executing. Отставание часов не
обнаруживается и продлевает локальное разрешение на величину расхождения.

Из корня репозитория на согласованном runtime-хосте:

```sh
uv sync --frozen
docker build -f honeypots/low/Dockerfile -t honeyforge-low:local .
uv run --frozen python -m agent.runtime.executor \
  --center https://center.example \
  --token-file /var/lib/honeyforge-agent/agent.token \
  --state /var/lib/honeyforge-agent/executor/state.sqlite3 \
  --journal-root /var/lib/honeyforge-agent/low \
  --image honeyforge-low:local --bind-address 127.0.0.1
```

Публикация на другом IP задаётся оператором через `--bind-address`; сетевой доступ
согласуется отдельно. Профиль центра не меняет образ, сеть и корень журналов.
Для отдельного локального центра HTTP допускается только с
`--allow-http-loopback --center http://127.0.0.1:8000`.
`--once` обрабатывает сохранённый результат или одну команду и завершает процесс.
HTTP таймаут — 10 секунд, ответ ограничен 1 MiB, redirects запрещены. HTTPS к центру
может идти через прокси окружения (`HTTPS_PROXY`, туннель CONNECT, Bearer шифруется);
loopback-центр прокси никогда не использует. Прочитанные `HF_AGENT_TOKEN` и
`HF_ENROLLMENT_TOKEN` не наследуются дочерними процессами; это не переписывает
исходное `/proc/<pid>/environ`, доступ к которому ограничивает оператор ОС.

Политика повторов: сетевая ошибка, HTTP 5xx и 409 `LEASE_EXPIRED`/`LEASE_MISMATCH`
повторяются через две секунды. Остальные HTTP 4xx, неверный ответ центра
(`INVALID_RESPONSE`, `RESPONSE_TOO_LARGE`, `REDIRECT_REJECTED`), конфликты состояния
(`STALE_GENERATION`, `ACK_MISMATCH`, `ACK_CONFLICT`, `INVALID_RESULT`) и `CLOCK_SKEW` останавливают
процесс с безопасным кодом причины.
После потери lease исполняющаяся Docker-операция дожидается завершения;
следующая мутация запрещена до повторной выдачи. SIGTERM/Ctrl-C дожидаются операции;
SIGKILL оставляет durable recovery. Под systemd нужны `KillMode=mixed` (SIGTERM
получает только основной процесс, а не дочерние docker CLI посреди `docker run`) и
`TimeoutStopSec`, рассчитанный на всю последовательность операций: отдельный
Docker CLI ограничен 30 с, health check по умолчанию 15 с, HTTP — 10 с, но CLI
может вызываться несколько раз. Значение 120 с не доказывает завершение всей
последовательности. SIGKILL после срока закрывается durable recovery.
Принудительное завершение хоста или всей process group требует
сверки после рестарта; на Linux с systemd это поведение ещё не проверялось.

Heartbeat отправляется сразу и затем каждые 10 с независимой задачей, даже во
время долгой Docker-операции. Ошибка печатается безопасным кодом, heartbeat
продолжается. Отличающиеся от `["low"]` capabilities вызывают предупреждение.
Renew продлевает только командный lease и heartbeat не заменяет.

## Windows 11 и Docker Desktop

Пользователь запускает Docker Desktop с Linux-контейнерами. Для первого прогона
назначьте HTTP-профиль 8080. По умолчанию порт опубликован на `127.0.0.1`;
для другой машины оператор задаёт адрес хоста и правило брандмауэра Windows.
Пути состояния и журналов — на локальном диске.

Центр доступен по HTTPS либо через SSH-туннель в отдельном PowerShell:
`ssh -N -L 8000:127.0.0.1:8000 USER@SERVER`. Во втором случае параметры клиента:
`--center http://127.0.0.1:8000 --allow-http-loopback`.
Исходящие соединения: HTTPS 443 либо SSH 22; входящий порт ловушки — 8080.
Пользователь сам создаёт SSH-ключ: `ssh-keygen -t ed25519 -C "danil-honeyforge"`,
владельцу передаёт только файл `id_ed25519.pub`.

Пример после получения доступа к центру, из корня репозитория:

```powershell
$agentDir = Join-Path $env:LOCALAPPDATA 'HoneyForge'
New-Item -ItemType Directory -Path $agentDir -Force | Out-Null
icacls $agentDir /inheritance:r /grant:r "$($env:USERNAME):(OI)(CI)F"
$enrollmentSecret = Read-Host 'Enrollment token' -AsSecureString
try {
    $env:HF_ENROLLMENT_TOKEN = [System.Net.NetworkCredential]::new('', $enrollmentSecret).Password
    uv run --frozen python -m agent.runtime.executor.enroll --center http://127.0.0.1:8000 --allow-http-loopback --name danil-low --token-file "$agentDir\agent.token"
} finally {
    Remove-Item Env:HF_ENROLLMENT_TOKEN -ErrorAction SilentlyContinue
    $enrollmentSecret.Dispose()
}
docker build -f honeypots/low/Dockerfile -t honeyforge-low:local .
uv run --frozen python -m agent.runtime.executor --center http://127.0.0.1:8000 --allow-http-loopback --token-file "$agentDir\agent.token" --state "$agentDir\executor\state.sqlite3" --journal-root "$agentDir\low"
```

Stop подтверждается после отсутствия управляемого контейнера и TCP-отказа на
всех опубликованных портах. Если порт занят другим процессом, ACK содержит
`PORT_STILL_OPEN`; агент не завершает чужой процесс. TCP-таймаут не считается
доказательством освобождения порта.

## Восстановление после конфликта с центром

`ACK_MISMATCH`/`ACK_CONFLICT`: исполнитель остановлен, результат команды и история
уже записаны локально. Сверьте контракт и сохранённый результат. Перезапуск повторит
тот же ACK; после истечения срока команды центр отвечает `COMMAND_FINISHED`, запись
становится abandoned, и расхождение устраняет stop с reconcile из unknown.

`STALE_GENERATION` при приёме новой команды, 404 узла или `HOST_BUSY` после
восстановления БД центра или перерегистрации узла: история агента больше не совпадает
с центром, и повтор этого не исправит. Порядок действий оператора:
1. Остановить исполнитель.
2. Сохранить архивную копию каталога состояния, не удаляя её.
3. Сверить с центром фактические контейнеры (`docker inspect`, метки `honeyforge.*`).
4. Только после этого запустить исполнитель с новым пустым каталогом состояния: история
   заново возьмётся из `GET /agent/v1/config`.

Автоматически агент историю не стирает.

## Совместный smoke

1. Зарегистрировать отдельный тестовый узел с уровнем Low и создать профиль на
   свободном разрешённом порту; назначить неизменяемую revision.
2. Запустить executor. В центре отправить start с текущим expected_generation
   и новым idempotency_key. Дождаться succeeded и deployment running.
3. Проверить реальный HTTP/TCP ответ ловушки, Docker inspect метки и Low JSONL.
   Сохранить command_id, generation, instance_id, applied_revision без токенов.
4. Перезапустить только агент: instance_id ловушки сохраняется. Проверить повтор
   команды/потерю ответа ACK на тестовом транспорте, без повторного docker run.
5. Отправить stop с новым idempotency_key. Получить succeeded/stopped, отсутствие
   контейнера и отказ подключения по всем портам, прежние instance_id/applied_revision
   и увеличенное generation.
6. Отдельно проверить потерю lease, неизменный execution_expires_at и cleanup
   через stop из unknown. В протоколе указать SHA main/ветки и версии Docker/центра.

Локальные тесты `tests/runtime/test_executor.py` запускают настоящую LowService
через подмену Docker и подмену транспорта центра. `backend/tests/test_runtime_executor.py`
связывает тот же executor с настоящими FastAPI и PostgreSQL: большинство сценариев идут
через транспорт TestClient; wire-сценарии — через настоящий `HttpCommandClient`
(urllib) и uvicorn на loopback, включая истечение lease перед ACK, повторную
выдачу, регистрацию и heartbeat. Они не
заменяют smoke Docker на сервере.

## Проверки

```sh
uv run --frozen ruff check agent honeypots infra tests
uv run --frozen ruff format --check agent honeypots infra tests
uv run --frozen mypy
uv run --frozen pytest
```

Backend тест запускается из `backend/` в его locked окружении, с отдельной БД
`honeyforge_test` через `HF_TEST_DATABASE_URL` (тесты очищают эту БД).
