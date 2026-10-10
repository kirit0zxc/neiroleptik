# Все backend прогоны удерживают один общий дескриптор, а не файл-флаг.
param(
    [Parameter(Mandatory = $true)][string]$LockPath,
    [string]$Python = 'backend/.venv/Scripts/python.exe',
    [string[]]$TestArguments = @('-q', 'backend/tests/telemetry'),
    [int]$WaitSeconds = 30
)
$ErrorActionPreference = 'Stop'
if ($WaitSeconds -lt 1 -or $WaitSeconds -gt 60) { throw 'Ожидание должно быть 1..60 секунд' }
$deadline = [DateTime]::UtcNow.AddSeconds($WaitSeconds)
$handle = $null
while ($null -eq $handle -and [DateTime]::UtcNow -lt $deadline) {
    try {
        $handle = [System.IO.File]::Open($LockPath, [System.IO.FileMode]::OpenOrCreate,
            [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
    } catch [System.IO.IOException] {
        Start-Sleep -Milliseconds 200
    }
}
if ($null -eq $handle) { throw 'Общая блокировка PostgreSQL занята; тесты не запущены' }
try {
    & $Python -m pytest @TestArguments
    $testResult = $LASTEXITCODE
} finally {
    $handle.Dispose()
}
exit $testResult
