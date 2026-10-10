param(
    [Parameter(Mandatory=$true)][string]$UvPath,
    [Parameter(Mandatory=$true)][string]$LockPath,
    [Parameter(Mandatory=$true)][string]$BackendPath,
    [Parameter(ValueFromRemainingArguments=$true)][string[]]$TestArguments
)
# Общий эксклюзивный дескриптор защищает pytest вместе с миграциями и cleanup.
$deadline = [DateTime]::UtcNow.AddSeconds(30)
$handle = $null
while ($null -eq $handle) {
    try {
        $handle = [System.IO.File]::Open($LockPath, [System.IO.FileMode]::OpenOrCreate,
            [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
    } catch [System.IO.IOException] {
        if ([DateTime]::UtcNow -ge $deadline) { throw 'PostgreSQL lock занят более 30 секунд' }
        Start-Sleep -Milliseconds 100
    }
}
try {
    Push-Location -LiteralPath $BackendPath
    try {
        & $UvPath run --frozen pytest @TestArguments
        $testExit = $LASTEXITCODE
    } finally { Pop-Location }
} finally { $handle.Dispose() }
exit $testExit
