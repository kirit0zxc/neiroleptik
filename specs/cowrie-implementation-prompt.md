# Промпт реализации участника 4: Cowrie → события и TTY

Примечание исполнения 15.10.2026: исходные acceptance/cowrie тесты после зелёного
запуска перенесены в tests/telemetry/test_cowrie_adapter_contract.py и
tests/telemetry/test_cowrie_tty_contract.py. Проверки: [cowrie-validation.md](cowrie-validation.md).

Работай в neiroleptik от актуального main в отдельной ветке codex/.
Прочитай AGENTS.md и specs/cowrie-telemetry.md. Пользователь поручил подготовить
нашу часть без ожидания Данила. Не утверждай, что runtime уже реализован или
Данил подтвердил контракт. Не отправляй ему сообщения без отдельного поручения.

Цель первого PR: реализовать чистые CowrieAdapter/CowrieBinding и настоящий
CowrieTtyConverter по указанным интерфейсам, сделать зелёным весь набор
acceptance/cowrie и перенести его в tests/telemetry. Не подменяй Cowrie форматом
HF-TTY/1. Существующие Low, UUID, Event/ACK, PublicPolicy и runtime не ломать.

Порядок:

1. Обнови base относительно main, прочитай связанные спецификации и код схем,
   spool, durable conversion, frontend-protocol-v1 и tty_fixture. Зафиксируй SHA.
2. Запусти красные тесты acceptance/cowrie; сохрани фактический результат.
3. Реализуй mapper отдельно от file I/O/storage и parser отдельно от transport.
   Используй классы, строгие ограничения, безопасные коды ошибок. Авторские
   комментарии/docstring пиши по-русски. Дополняй тесты при найденных пробелах;
   не ослабляй утверждения ради прохождения.
4. Проверь существующие Event и ReplayChunk schemas, всю telemetry регрессию,
   ruff, format, mypy, export_contracts --check, build. Pure parser тесты не
   объявляй проверкой живого Cowrie или PostgreSQL.
5. Актуализируй architecture и статус спецификации, запиши результаты/ограничения.
   Перед push два независимых рецензента, затем третий с обоими отчётами;
   повтори цикл после правок. Не Approve и не merge: решение принимает человек.

Следующие PR: durable collector/worker, затем transport/storage/read replay
после точного описания HTTP/ownership/нескольких каналов в общем OpenAPI.
Не добавляй новый HTTP API по догадке в первый PR. События из command.input
не доказывают output, скачанный файл или полную запись сессии.

## Готовое сообщение Данилу

«Данил, следующий стык делаем так: ты запускаешь Cowrie с зафиксированными
версией и image digest, включаешь JSONL и бинарные TTY. На каждый подтверждённый
start отдаёшь доверенный binding agent/deployment/instance/revision/SSH port,
реестр UUID поколений JSONL и read-only root. UUID поколения сохраняется при
rename-ротации; новый файл получает новый UUID. После reset/start новый instance
и директория, старые журналы сохраняются. Для каждого TTY отдельно recording_id,
Cowrie session, безопасный относительный путь, seal с size/SHA-256 после закрытия
writer. session.closed и EOF сами по себе seal не заменяют. Доступ к файлам
только по binding, пути из Cowrie JSON не исполняем. Мы делаем JSONL→Event v1,
долговечную конверсию/доставку и TTY→replay. JSONL события и TTY не склеиваем
по IP; несколько SSH-каналов не смешиваем. Точный контракт —
specs/cowrie-telemetry.md. Для приёмки нужны PTY/exec, reconnect/rotation,
stop/reset и образцы без секретов. Нашу часть начинаем по этому контракту».

Сообщение подготовлено для передачи пользователем, фактически не отправлено.
