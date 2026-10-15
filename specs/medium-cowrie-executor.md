# Medium: Cowrie, минимальный исполнитель

Объём согласован владельцем в задаче завершения Medium: один SSH-сервис,
start/stop через существующие команды центра, durable ACK и журнал Cowrie.
Контракты HTTP и Event v1 не меняются. Reset, High и точный TTY replay не входят.

Medium включается явно параметром `--medium-image`; образ собирается заранее из
закреплённого Cowrie 3.1.1. Регистрация с `--medium` объявляет low и medium только
после проверки образа оператором. Без флага прежний Low работает без изменений.
Один профиль medium содержит ровно один ssh-сервис; пользовательский banner пока
не поддерживается. Единственный порт — 2222, обязательные credentials профиля —
ровно root/demo. Другие значения отвергаются до Docker run. Существующие lease,
generation, instance_id и повтор ACK общие.

Контейнер работает непривилегированно с read-only rootfs, ограничением CPU/RAM/PID,
без Docker socket. Для первого стенда порт привязан к loopback; отдельная Docker
bridge network обязательна для локального Medium. Исходящий доступ этой сетью
не блокируется; публичная эксплуатация не разрешена режимом драйвера. SSH-forwarding
и SFTP выключены. Каталог состояния Cowrie отделён по
deployment/instance, не удаляется при stop; содержит JSON, ключи, TTY и downloads.
Публичный доступ и доказательство полноценной изоляции на Linux в MVP не заявляются.

Проверено 15.10.2026 на Windows + Docker Desktop: настоящий центр принял start,
состояние стало running; вход root/demo и команда whoami вернули root; 11 событий
прошли batch ACK и появились в GET /api/v1/events; stop завершился состоянием stopped.
Назначение проверки: b7a7e17b-8825-426f-b643-7cf974ed6aa3. Журналы пережили stop.
Автотесты покрывают повтор ACK без повторного run, явное включение Medium,
checkpoint после рестарта, маскирование пароля и некорректный журнал.

## Запуск Windows + Docker Desktop

Из корня checkout с установленными зависимостями Python 3.12:

```powershell
docker build -f honeypots/medium/Dockerfile -t honeyforge-medium:local .
docker network create honeyforge-medium-local
New-Item -ItemType Directory -Force C:\HoneyForge\medium-private
# HF_ENROLLMENT_TOKEN передаётся отдельно, не сохраняется в репозитории.
python -m agent.runtime.executor.enroll --center https://CENTER --name medium-demo --medium --token-file C:\HoneyForge\medium-private\agent.token
python -m agent.runtime.executor --center https://CENTER --token-file C:\HoneyForge\medium-private\agent.token --state C:\HoneyForge\medium-private\executor\state.sqlite3 --journal-root C:\HoneyForge\journals --medium-image honeyforge-medium:local --network honeyforge-medium-local --telemetry
```

Для центра на этом же ПК допускается `--center http://127.0.0.1:8000
--allow-http-loopback` в обеих командах. Каталоги state и journals сохраняются вне
checkout; их нельзя очищать при обновлении. Очередь событий создаётся рядом с state.
В UI создать Medium SSH-профиль порт 2222, пустой banner, root/demo и назначить
зарегистрированному узлу. После start: `ssh -p 2222 root@127.0.0.1`, пароль demo.
После stop порт должен закрыться; в ленте остаются auth.attempt/session.command.

Проверенный стенд — Windows. На Linux нужно заранее обеспечить доступ UID/GID 999
к выделенному каталогу Cowrie; запуск и права на таком стенде здесь не проверены.
Лог plain не ротируется; предел одного файла 64 MiB. Общая дисковая квота, внешние
архивы, разбор TTY, reset, произвольные credentials и production firewall остаются
отдельными этапами. Журналы могут содержать введённые пароли: доступ только оператору;
в публичный Event исходный пароль не передаётся. Docker Desktop может показывать
адрес шлюза вместо настоящего IP клиента — source attribution не гарантируется.
