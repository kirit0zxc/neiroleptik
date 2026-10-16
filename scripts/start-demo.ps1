# Запуск центра и собранного UI; секреты и volume не перезаписываются.
param([string]$CreateAdmin = '')
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
Push-Location $taskRoot
try {
    & docker info --format '{{.ServerVersion}}'
    if ($LASTEXITCODE -ne 0) { throw 'Запустите Docker Desktop с Linux containers.' }
    if (-not (Test-Path -LiteralPath '.env')) {
        & python scripts/init_local_env.py
        if ($LASTEXITCODE -ne 0) { throw 'Не удалось создать .env; нужен Python 3.' }
    }
    $taskCompose = @('compose', '--env-file', '.env', '-f', 'infra/compose.center.yml', '-f', 'infra/compose.demo.yml')
    & docker @taskCompose up -d db
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось запустить PostgreSQL.' }
    & docker @taskCompose build api web
    if ($LASTEXITCODE -ne 0) { throw 'Сборка завершилась ошибкой.' }
    & docker @taskCompose run --rm api /app/.venv/bin/alembic upgrade head
    if ($LASTEXITCODE -ne 0) { throw 'Миграция завершилась ошибкой; API не обновлён.' }
    if ($CreateAdmin) {
        & docker @taskCompose run --rm api /app/.venv/bin/python -m honeyforge.cli create-user --username $CreateAdmin --role admin
        if ($LASTEXITCODE -ne 0) { throw 'Пользователь не создан. Существующий пароль не изменяется.' }
    }
    & docker @taskCompose up -d --wait api web
    if ($LASTEXITCODE -ne 0) { throw 'Сервисы не прошли healthcheck. Проверьте compose logs.' }
    Write-Host 'HoneyForge: http://localhost:5173 — настоящий API, события и replay.'
    Write-Host 'Исполнитель запускается отдельно по docs/deployment.md.'
} finally {
    Pop-Location
}
