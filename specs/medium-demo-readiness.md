# Готовность Medium к локальной демонстрации

16.10.2026: доводим согласованный Windows + Docker Desktop сценарий на main.
HTTP/Event/replay контракты не меняются. Экспериментальный durable Cowrie из
PR #53/#55 не включается автоматически: его transport TTY ещё отдельный этап.

## Требования

- Воспроизводимый запуск PostgreSQL, FastAPI и собранного frontend без MSW.
- Frontend доступен на localhost:5173; запросы API и WebSocket проходят через
  один nginx origin. Events, investigation и replay включены; отсутствующая
  analytics/summary не включается общим флагом.
- Executor работает отдельным процессом Windows с постоянными token/state/spool
  и journals вне checkout. Docker socket остаётся только у доверенного executor.
- Настраиваемый Medium SSH: start, проверка пароля, команды, автоматическая лента,
  replay после закрытия, reset с новым instance и stop с закрытием порта.
- Перезапуск executor не теряет очередь и не создаёт второй контейнер.
- README отражает реально реализованное; инструкция содержит установку,
  регистрацию, проверку, сохранение данных, остановку и отдельный TLS-сценарий.

## Приёмка

Экран сессии вместо заглушки показывает страницы существующего
GET /api/v1/events?session_id=...&sort=occurred_at&order=asc&limit=50.
Навигация по next_cursor; обновление начинает чтение заново. Выводятся только
время, тип, наблюдаемый источник и безопасные поля команды/результата входа;
произвольный data и пароль не выводятся. Это история опубликованных событий,
а не утверждение о полноте захвата. Сбой истории не выключает replay.

Проверить настоящие контейнеры на Windows и frontend через браузер без моков.
Проверить login, запуск кнопкой, SSH, события/replay, reset и stop. Сохранить
результаты с коммитом и ограничениями; не выдавать localhost за публичный сервер.
Образ Medium сохраняет ограничения demo bridge/loopback; публичный honeypot,
High/KVM, внешний архив, retention и новое durable/sealed wiring не заявляются.

## Результат локальной приёмки 16.10.2026

База main: `6183092dc9234dd35f25c94834dc539b48f078ea`. Проверены собранные
Docker API/web, PostgreSQL и Cowrie на Windows + Docker Desktop; отдельный
узел `Medium demo 16.10`. Реальная регистрация, heartbeat и команды исполнителя.

- Вход в кабинет, start из браузера, SSH на 127.0.0.1:2224, настроенные
  banner/hostname, команды whoami/pwd/ls/echo. Ответы записаны Cowrie.
- Автоматическая публикация событий, WebSocket через nginx, 67 чанков replay
  после закрытия SSH; запись прочитана в UI. MSW/service workers отсутствуют.
- Reset из UI заменил instance_id; stop подтверждён, TCP-порт закрылся.
- Executor принудительно остановлен при работающей ловушке. Новая SSH-сессия
  записана без него; после запуска с прежним state/journals доставлены команды
  и полный replay (67 чанков), instance не изменился. После проверки выполнен stop.
- Документированный `scripts/start-demo.ps1` выполнен через Windows PowerShell;
  API/web/db прошли healthcheck, данные и доступ сохранились.
- Frontend: 526 unit-тестов, typecheck и Docker build; Playwright: 11 E2E.
  Medium/collector/replay: 13 тестов, 1 POSIX FIFO skip на Windows.

Первый общий E2E прогон обнаружил гонку старого live-feed теста: первый demo IP
уже присутствовал до измерения количества строк. Тест теперь ожидает новое
событие сверху и рост списка относительно одного snapshot, без привязки к тому,
какой по счёту demo event успел прийти. Повторный полный E2E прошёл.
Это не проверка High, production firewall или нового durable collector PR #55.
