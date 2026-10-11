# Изолированный сценарий использует существующий PostgreSQL lock-wrapper.
param(
    [Parameter(Mandatory = $true)][string]$Python,
    [Parameter(Mandatory = $true)][string]$LockPath,
    [Parameter(Mandatory = $true)][string]$ReportDirectory
)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$runDirectory = Join-Path ([System.IO.Path]::GetFullPath($ReportDirectory)) ([guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $runDirectory | Out-Null
$reportPath = Join-Path $runDirectory 'result.json'
$previousReport = $env:HF_ACCEPTANCE_REPORT
$env:HF_ACCEPTANCE_REPORT = $reportPath
$timer = [System.Diagnostics.Stopwatch]::StartNew()
$testExit = 1
try {
    Push-Location -LiteralPath (Join-Path $taskRoot 'backend')
    try {
        & (Join-Path $PSScriptRoot 'test_backend_locked.ps1') -Python $Python -LockPath $LockPath `
            -TestArguments @('tests/telemetry/test_ingress_end_to_end.py::test_automatic_acceptance',
                '-q', '--tb=short', "--basetemp=$runDirectory/tmp", "--junitxml=$runDirectory/junit.xml")
        $testExit = $LASTEXITCODE
    } finally { Pop-Location }
    if ($testExit -eq 0 -and (Test-Path -LiteralPath $reportPath)) {
        $report = Get-Content -Raw -LiteralPath $reportPath | ConvertFrom-Json
        if ($report.status -ne 'passed') { $testExit = 1 }
    } else {
        $testExit = 1
    }
} catch {
    # Исключение среды не копируется: оно может содержать URL или секрет.
    $testExit = 1
} finally {
    $env:HF_ACCEPTANCE_REPORT = $previousReport
    $timer.Stop()
}
if ($testExit -ne 0) {
    @{ status = 'failed'; code = 'TEST_OR_INFRASTRUCTURE_FAILURE';
       elapsed_seconds = $timer.Elapsed.TotalSeconds; exit_code = $testExit } |
        ConvertTo-Json | Set-Content -LiteralPath $reportPath -Encoding utf8
}
Write-Output "Report: $reportPath"
exit $testExit
