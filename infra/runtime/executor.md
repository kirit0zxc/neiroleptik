# Исполнитель команд Low

Один агент на каталог состояния и узел. Нужны Python 3.12, зависимости `uv.lock`,
Docker Engine, заранее собранный образ Low, собственный Bearer узла центра.
Каталог состояния должен принадлежать пользователю агента и находиться на локальном
диске. SQLite и lock нельзя удалять для обхода конфликта или восстановления.

Из корня репозитория на согласованном runtime-хосте:

```sh
uv sync --frozen
docker build -f honeypots/low/Dockerfile -t honeyforge-low:local .
# HF_AGENT_TOKEN передаётся процессу через защищённое окружение развёртывания.
uv run --frozen python -m agent.runtime.executor \
  --center https://center.example \
  --state /var/lib/honeyforge-agent/executor/state.sqlite3 \
  --journal-root /var/lib/honeyforge-agent/low \
  --image honeyforge-low:local --bind-address 127.0.0.1
```

Публикация на другом IP задаётся оператором через `--bind-address`; сетевой доступ
согласуется отдельно. Профиль центра не меняет образ, сеть и корень журналов.
Для отдельного локального центра HTTP допускается только с
`--allow-http-loopback --center http://127.0.0.1:8000`.
`--once` обрабатывает сохранённый результат или одну команду и завершает процесс.
HTTP таймаут — 10 секунд, ответ ограничен 1 MiB, redirects запрещены.
При временной сетевой ошибке polling повторяется через две секунды; конфликты
состояния и HTTP 4xx останавливают процесс с безопасным кодом причины.
После потери lease исполняющаяся Docker-операция дожидается завершения;
следующая мутация запрещена до повторной выдачи. SIGTERM/Ctrl-C дожидаются операции;
SIGKILL оставляет durable recovery. При systemd нужно предусмотреть TimeoutStopSec
не меньше максимального времени Docker-операции и health check, например 120 секунд;
принудительное завершение хоста или всей process group требует сверки после рестарта.
Heartbeat узла относится к отдельному регистрационному процессу и не заменяет renew.

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
   контейнера, прежние instance_id/applied_revision и увеличенное generation.
6. Отдельно проверить потерю lease, неизменный execution_expires_at и cleanup
   через stop из unknown. В протоколе указать SHA main/ветки и версии Docker/центра.

Локальные тесты `tests/runtime/test_executor.py` запускают настоящую LowService
через подмену Docker. `backend/tests/test_runtime_executor.py` связывает тот же
executor с настоящими FastAPI и PostgreSQL. Они не заменяют smoke Docker на сервере.

## Проверки

```sh
uv run --frozen ruff check agent honeypots infra tests
uv run --frozen ruff format --check agent honeypots infra tests
uv run --frozen mypy
uv run --frozen pytest
```

Backend тест запускается из `backend/` в его locked окружении, с отдельной БД
`honeyforge_test` через `HF_TEST_DATABASE_URL` (тесты очищают эту БД).
Ошибки ACK_CONFLICT/ACK_MISMATCH требуют сверки контракта и сохранённого результата;
автоматически переписывать ACK или историю запрещено.
