# Развёртывание HoneyForge: Low и Medium

## 1. Подготовка

Основной проверяемый стенд — Windows + Docker Desktop с Linux containers.
Нужны Git, Python 3.12, uv 0.11.28; интернет нужен для первой сборки образов и зависимостей.
Рекомендуемый запас для демо: 8 GB RAM и 10 GB диска. Docker и Windows не должны уходить в сон.
Все команды ниже выполняются из корня checkout. Не запускайте два executor с одним state или agent token.
Установка uv после Python: `python -m pip install uv==0.11.28`.

## 2. Центр и UI (Windows)

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start-demo.ps1 -CreateAdmin admin
```

Пароль вводится интерактивно. Повторный запуск — без `-CreateAdmin`.
Сайт: http://localhost:5173. В этой Docker-сборке MSW выключен; события, расследование и replay включены.
Не запускайте одновременно Vite на том же 5173. Ошибка создания существующего admin не меняет пароль.

Скрипт эквивалентен последовательности:

```powershell
# Только один раз, если .env ещё нет:
python scripts/init_local_env.py
docker compose --env-file .env -f infra/compose.center.yml -f infra/compose.demo.yml up -d db
docker compose --env-file .env -f infra/compose.center.yml -f infra/compose.demo.yml build api web
docker compose --env-file .env -f infra/compose.center.yml run --rm api /app/.venv/bin/alembic upgrade head
docker compose --env-file .env -f infra/compose.center.yml run --rm api /app/.venv/bin/python -m honeyforge.cli create-user --username admin --role admin
docker compose --env-file .env -f infra/compose.center.yml -f infra/compose.demo.yml up -d --wait api web
```

Publisher событий запускается внутри FastAPI автоматически; отдельную команду для него выполнять не надо.

## 3. Подготовка runtime (Windows)

```powershell
uv sync --frozen
docker build -f honeypots/low/Dockerfile -t honeyforge-low:local .
docker build -f honeypots/medium/Dockerfile -t honeyforge-medium:local .
# Создать один раз; существующую сеть не удалять при обновлениях.
docker network create honeyforge-medium-local
$runtimeDir = Join-Path $env:LOCALAPPDATA 'HoneyForge/runtime'
New-Item -ItemType Directory -Force $runtimeDir | Out-Null
```

У Docker Desktop должен быть доступ к каталогу runtime. Не храните state/SQLite в OneDrive,
сетевой папке, каталоге временных файлов или внутри контейнера. Каталог содержит токен и сырой
записанный ввод: ограничьте доступ своим пользователем. В демо контейнеры публикуются только на loopback.
Сеть bridge не является полной защитой от исходящего трафика и не предназначена для публичной ловушки.

## 4. Регистрация узла — один раз

Войдите как admin, откройте «Парк» → «+ Добавить хост», получите одноразовый enrollment token.
Это не пароль от кабинета и не постоянный agent token. Регистрация ниже сама сохранит постоянный токен
в новый файл; существующий файл не перезаписывается.

```powershell
$runtimeDir = Join-Path $env:LOCALAPPDATA 'HoneyForge/runtime'
$secret = Read-Host 'Одноразовый enrollment token' -AsSecureString
$env:HF_ENROLLMENT_TOKEN = [System.Net.NetworkCredential]::new('', $secret).Password
try {
    uv run --frozen python -m agent.runtime.executor.enroll --center http://127.0.0.1:8000 --allow-http-loopback --name medium-demo --medium --token-file "$runtimeDir/agent.token"
} finally {
    Remove-Item Env:HF_ENROLLMENT_TOKEN -ErrorAction SilentlyContinue
    Remove-Variable secret -ErrorAction SilentlyContinue
}
```

Если token истёк, получите новый в UI. Если запрос регистрации оборвался, проверьте созданный узел
и файл токена: не повторяйте регистрацию бесконечно. Не удаляйте рабочий token/state для повторного запуска.

## 5. Постоянный запуск исполнителя

В отдельном PowerShell (оставить работающим):

```powershell
$runtimeDir = Join-Path $env:LOCALAPPDATA 'HoneyForge/runtime'
uv run --frozen python -m agent.runtime.executor --center http://127.0.0.1:8000 --allow-http-loopback --token-file "$runtimeDir/agent.token" --state "$runtimeDir/executor/state.sqlite3" --journal-root "$runtimeDir/journals" --image honeyforge-low:local --medium-image honeyforge-medium:local --network honeyforge-medium-local --telemetry
```

Это долгоживущий процесс, а не одноразовая команда. Он получает команды, продлевает lease, сообщает
heartbeat и постоянно отправляет события. После перезагрузки Windows запустите Docker Desktop,
центр и эту же команду с теми же путями. Ctrl-C завершает исполнитель, **но не останавливает ловушки**:
перед завершением нажмите «Остановить» в UI и дождитесь подтверждения.

Сохранять весь `$runtimeDir`:

- `agent.token` — постоянный доступ конкретного узла;
- `executor/state.sqlite3` и соседние файлы — команды, ACK и история экземпляров;
- `executor/state.sqlite3.telemetry.sqlite3` — checkpoint и очередь повторной доставки;
- `journals/medium/<deployment>/<instance>/` — JSON, TTY и файлы Cowrie;
- `journals/medium-config/` — закрытая конфигурация с тестовыми credentials.

Файлы SQLite копировать только при остановленном исполнителе либо штатным SQLite backup.
Не удалять журналы, пока не проверены события и replay. Автоматический retention в демо отсутствует.

## 6. Настоящая проверка из браузера

1. «Парк»: узел `medium-demo` на связи, возможности включают Medium.
2. «Профили» → новый Medium: один SSH-сервис, порт `2224`, hostname `accounting-server`.
   Баннер пустой либо, например, `SSH-2.0-OpenSSH_9.6`; 1–8 пар username/password.
3. «Парк» → «+ Запустить ловушку»: выбрать узел, профиль и ревизию, запланировать.
4. В строке назначения нажать «Запустить», дождаться успешного задания и состояния «работает».
5. С того же ПК выполнить:

```powershell
ssh -p 2224 root@127.0.0.1
# Внутри эмулируемой оболочки:
whoami
pwd
ls
exit
```

Введите пароль профиля. После reset Cowrie может сменить host key: сначала проверьте, что адрес
по-прежнему относится к вашей тестовой ловушке, затем удалите только её старую запись командой
`ssh-keygen -R "[127.0.0.1]:2224"`. Не отключайте проверку ключей глобально.

6. «События»: найдите SSH-подключение и команды, откройте связанную сессию.
   На странице доступны события и replay. После `exit` дождитесь доставки: запись появляется
   после закрытия сессии и публикации всех её чанков, а не мгновенно при вводе команды.
7. «Сбросить»: после подтверждения создаётся новый instance; повторите SSH, старая история сохраняется.
8. «Остановить»: дождитесь успеха. `Test-NetConnection 127.0.0.1 -Port 2224` должен показать
   `TcpTestSucceeded: False`. После успешного stop назначение можно удалить; история остаётся.
9. «Журнал аудита»: команды оператора и подтверждения агента. Это отдельный журнал от обращений атакующего.

Medium не запускает настоящие системные команды хоста: это SSH-эмуляция Cowrie.
Login-пароль скрывается из публичных событий, но shell-команды и записанные ответы видны пользователям кабинета.

## 7. Удалённый исполнитель и TLS

Центр и runtime могут быть на разных ПК в частной сети. На runtime вместо localhost укажите
проверенный `https://имя-центра`; уберите `--allow-http-loopback`. Исполнителю нужны исходящие HTTPS
к центру и доступ к registry при сборке. Порт PostgreSQL никогда не передавайте участникам.

Для Tailscale Serve передавайте `/agent/v1/*` и `/healthz` через выделенный шлюз к API; для удалённого
кабинета проксируйте frontend целиком вместе с `/api/*` и WebSocket. Публичный URL кабинета нужно
добавить в `HF_ALLOWED_ORIGINS`; при HTTPS задайте `HF_COOKIE_SECURE=true`, пересоздайте API и войдите заново.
Сертификат должен проверяться стандартным клиентом. Не используйте `-k`/verify=false.
При смене origin не оставляйте старую вкладку вошедшей под другим адресом.

Доступ к центру не означает доступ к SSH-ловушке: локальный runtime слушает `127.0.0.1` своего ПК.
Первый тест выполняется на этом runtime-ПК. Публикация SSH в интернет требует отдельного
изолированного runtime-хоста и firewall; этого данная инструкция не включает.

## 8. Linux / отдельный сервер

Центр можно собрать теми же Docker Compose командами из раздела 2 (без PowerShell-скрипта).
Сначала `python3 scripts/init_local_env.py`, затем сборка, миграции и создание пользователя.
Порты остаются loopback; внешний доступ — через HTTPS reverse proxy, с явным allowed origin.
`HF_ENVIRONMENT=production` требует HTTPS-cookie и отключает `/docs`.

Runtime должен работать на выделенном хосте. Каталоги Cowrie должны быть доступны UID/GID 999
в контейнере, конфигурация — только для чтения. На Linux заранее согласуйте владельцев каталогов,
дисковую квоту, хранение evidence и сетевую политику. Windows-приёмка не подтверждает корректность
Linux mounts/UID/firewall. High, KVM и nested virtualization для Medium не нужны.

## 9. Обновление и остановка

Остановите ловушки через UI, дождитесь ACK, завершите executor. Сохраните `.env` и runtime-каталог.
Перед миграцией сделайте резервную копию:

```powershell
docker compose --env-file .env -f infra/compose.center.yml exec db pg_dump -U honeyforge -Fc -f /tmp/honeyforge.dump honeyforge
docker compose --env-file .env -f infra/compose.center.yml cp db:/tmp/honeyforge.dump ./honeyforge.dump
```

Храните dump вне Git и ограничьте доступ: в нём есть данные проекта. Сохраните отдельно `.env` с ключом
шифрования credentials. Перейдите на проверенный коммит, повторите `scripts/start-demo.ps1`, пересоберите
Medium-образ при изменении runtime, запустите executor с прежними путями. Не используйте `down -v`.

Остановка центра без удаления данных:

```powershell
docker compose --env-file .env -f infra/compose.center.yml -f infra/compose.demo.yml stop
```

## 10. Диагностика

```powershell
docker compose --env-file .env -f infra/compose.center.yml -f infra/compose.demo.yml ps
docker compose --env-file .env -f infra/compose.center.yml -f infra/compose.demo.yml logs --tail 80 api web
curl.exe --fail http://localhost:8000/readyz
```

- Нет узла: проверьте enrollment, token и адрес центра; 401/403 требуют исправления доступа.
- Команда ожидает: executor должен работать и иметь доступ к Docker; heartbeat сам команду не исполняет.
- SSH не открывается: дождитесь ACK running, проверьте порт профиля и Docker; `running` не отменяет сетевую проверку.
- Нет событий: executor нужен с `--telemetry`; смотрите его строку «Телеметрия» и ошибки ingest.
- Нет replay: выйдите из SSH, дождитесь публикации; raw/TTY не удаляйте. Отсутствующие байты не восстанавливаются из команд.
- Во время пересоздания API возможен краткий 502; nginx обновляет адрес через Docker DNS. Если ошибка сохраняется, проверьте healthcheck API и logs web.
