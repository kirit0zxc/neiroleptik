# neiroleptik
Hackathon project: React + TypeScript, Python/Django, PostgreSQL, and machine learning.

## Начальный runtime HoneyForge

Реализуется локальный блок D1: ограниченная Low-ловушка, журнал команд и
неразрушающий preflight Linux/KVM. Центр, UI, Medium и High пока не реализованы;
локальные тесты не подтверждают сквозной сценарий или изоляцию реальной VM.

Требуется Python 3.12. Зависимостей времени выполнения нет.

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-dev.txt
.\.venv\Scripts\python -m pytest
.\.venv\Scripts\python -m ruff check .
.\.venv\Scripts\python -m ruff format --check .
.\.venv\Scripts\python -m mypy
.\.venv\Scripts\python -m build --no-isolation
```

В Linux после создания окружения использовать `.venv/bin/python`.

### Low на локальном компьютере

```powershell
.\.venv\Scripts\python -m honeypots.low
```

По умолчанию сервис доступен только на loopback: TCP 2222 и HTTP 8080.
Журнал JSONL выводится в stdout; это диагностический формат, не согласованный
Event центра и не устойчивый spool. Ctrl-C завершает сервис.

При работающем Docker Engine:

```text
docker compose -f compose.runtime.yml config --quiet
docker compose -f compose.runtime.yml up --build
```

Compose публикует TCP 13222 и HTTP 18080 только на 127.0.0.1. Контейнер ограничен
по ресурсам и использует внутреннюю сеть. Это локальная конфигурация разработки;
изоляцию хоста и будущего runtime-сервера необходимо отдельно проверить.

### Проверка будущего runtime-хоста

```text
python -m infra.vm.preflight
```

Команда ничего не создаёт и не изменяет. Linux/KVM/libvirt проверяются статически;
создание VM, права на изменение сети и матрица изоляции требуют отдельного smoke.
На Windows ожидается отчёт о неподдерживаемой платформе и ненулевой код возврата.

### Спецификации и следующие проверки

- [Архитектура](specs/architecture.md).
- [Требования D1](specs/runtime-d1.md).
- [Предложения общих контрактов](specs/runtime-contract-proposal.md), ожидают
  согласования участников 2 и 4.
- [Подготовка runtime и Cowrie spike](infra/runtime/README.md).

Сроки сохраняются: D1–D5, 30 человеко-часов (10 / 12 / 8).
Правила изменений, ревью и человеческого одобрения определены в [AGENTS.md](AGENTS.md).
