# Локальная проверка Low → живая лента, 14.10.2026

База: main `f5ad092` (PR39). Дополнение этой ветки: `websockets==15.0.1`
перенесён из dev в основные зависимости backend. Docker устанавливает `--no-dev`;
без этого транспорта тесты с dev-окружением не доказывают доступность WebSocket.

## Проверенный сценарий

Windows + Docker Desktop Linux, PostgreSQL 17 и API из `infra/compose.center.yml`.
Фронт Vite использует настоящий API: `VITE_API_MOCK=0`, events/investigation включены.
API и frontend слушают loopback; Low опубликована только на `127.0.0.1:8080`.
Использован существующий зарегистрированный тестовый узел, его токен не изменялся.
Publisher запущен штатным lifespan API. SQL-вставок событий и mock sender нет.

1. Из интерфейса отправлен start. Агент создал настоящий Docker-контейнер,
   центр получил ACK: running, generation=6, applied_revision=1.
2. Отправлены три GET-запроса, в том числе с тестовым секретом в query.
   Клиент получил HTTP 200. Этот результат не подставлялся в Event.
3. `agent.telemetry.collect_low` прочитал журнал текущего подтверждённого instance:
   9 строк (header + 8 событий), 3064 байта. Mapper и очередь штатные.
4. `agent.telemetry.deliver --once` получил ACK на 8 событий, quarantine=0.
   Четыре подключения и четыре HTTP request включают стартовую HEAD-проверку здоровья.
5. Открытая заранее лента без перезагрузки получила 8 событий через WebSocket.
   REST также вернул 8 событий. Произвольные пути скрыты, query/raw/base64 не выданы.
   UI явно показывает неизвестную полноту захвата и отсутствие записи ответа.
6. Новый запуск collector/sender с той же SQLite не создал pending:
   conversion=idle, accepted=0, в REST осталось 8 событий.
7. Stop из интерфейса подтверждён агентом: stopped, generation=7.
   Контейнер удалён. Collector сохранил footer (147 байт), не создав новый Event.

Deployment: `d9d995a0-8ea3-467b-b776-d58845c17cde`.
Instance: `6a1d1e24-95ac-418b-a73b-decd32241859`.

## Повторение

Запустить центр, frontend и executor по существующим инструкциям. После start
получить подтверждённые host/deployment/instance/applied_revision из центра.
Не выводить binding из недоверенного содержимого журнала. Передать collector
только полную цепочку файлов соответствующего instance и отдельный writable spool:

```text
python -m agent.telemetry.collect_low --spool SPOOL --agent-id AGENT --deployment-id DEPLOYMENT --instance-id INSTANCE --profile-revision REVISION JOURNAL...
python -m agent.telemetry.deliver --spool SPOOL --agent-id AGENT --endpoint http://127.0.0.1:8000/agent/v1/events --once
```

Bearer передаётся только через `HF_AGENT_TOKEN` в окружении sender.
Collector и sender здесь запускаются отдельными ограниченными проходами;
постоянная служба сбора и автоматическое получение истории bindings не добавлены.
После новых запросов повторить проход, после stop дочитать footer.

## Ограничения

- Проверен HTTP Low; отдельный TCP-сервис, Medium/Cowrie и High не проверялись.
- Windows bind-mount не подтверждает Linux UID/GID/read-only mount/fencing.
- Потеря сетевого ACK и авария writer не воспроизводились; повтор после успешного
  ACK проверен с повторным открытием SQLite отдельным процессом.
- Запись сессии в UI пока показывает экран «в работе» при выключенном replay.
- Сборщик запускался вручную; автоматического получения новых событий без
  очередного прохода нет. Наличие WebSocket не заменяет запуск collector/sender.
- Источник `172.17.0.1` — наблюдаемый адрес Docker NAT, не восстановленный внешний IP.
- Автоматическая очистка журналов и spool не выполнялась.
