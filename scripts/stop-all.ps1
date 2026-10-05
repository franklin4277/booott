[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$logsDir = Join-Path $root "logs"
$python = [System.IO.Path]::GetFullPath(
    (Join-Path $root ".venv\Scripts\python.exe")
)

$services = @(
    @{ Name = "api-gateway"; Module = "services.api_gateway.app.main:app" }
    @{ Name = "market-data"; Module = "services.market_data.app.main:app" }
    @{ Name = "reconciliation"; Module = "services.reconciliation.app.main:app" }
    @{ Name = "risk-engine"; Module = "services.risk_engine.app.main:app" }
    @{ Name = "pattern-engine"; Module = "services.pattern_engine.app.main:app" }
    @{ Name = "ai-engine"; Module = "services.ai_engine.app.main:app" }
    @{ Name = "execution-engine"; Module = "services.execution_engine.app.main:app" }
    @{ Name = "telegram-bot"; Module = "services.telegram_bot.app.main:app" }
    @{ Name = "market-engine"; Module = "services.market_engine.app.main:app" }
    @{ Name = "trade-monitor"; Module = "services.trade_monitor.app.main:app" }
    @{ Name = "mt5-host-adapter"; Module = "scripts.mt5_host_adapter:app" }
)

if (-not (Test-Path -LiteralPath $logsDir)) {
    Write-Host "[INFO] No service PID directory found; nothing to stop."
    exit 0
}

$failed = $false
foreach ($service in $services) {
    $pidFile = Join-Path $logsDir "$($service.Name).pid"
    if (-not (Test-Path -LiteralPath $pidFile)) {
        continue
    }

    $rawPid = (Get-Content -LiteralPath $pidFile -Raw).Trim()
    $servicePid = 0
    if (-not [int]::TryParse($rawPid, [ref]$servicePid) -or $servicePid -le 0) {
        Write-Host "[FAIL] Invalid PID in $pidFile"
        $failed = $true
        continue
    }

    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $servicePid" `
        -ErrorAction SilentlyContinue
    if (-not $process) {
        Write-Host "[INFO] Removing stale PID file for $($service.Name)."
        Remove-Item -LiteralPath $pidFile -Force
        continue
    }

    $actualExecutable = [System.IO.Path]::GetFullPath($process.ExecutablePath)
    $expectedCommand = "-m uvicorn $($service.Module)"
    if (
        -not [string]::Equals(
            $actualExecutable,
            $python,
            [System.StringComparison]::OrdinalIgnoreCase
        ) -or
        $process.CommandLine -notlike "*$expectedCommand*"
    ) {
        Write-Host "[FAIL] PID $servicePid in $pidFile does not match the expected service; leaving it untouched."
        $failed = $true
        continue
    }

    Write-Host "[INFO] Stopping $($service.Name) (PID $servicePid)..."
    Stop-Process -Id $servicePid -Force
    $deadline = (Get-Date).AddSeconds(10)
    do {
        Start-Sleep -Milliseconds 250
        $remaining = Get-Process -Id $servicePid -ErrorAction SilentlyContinue
    } while ($remaining -and (Get-Date) -lt $deadline)

    if ($remaining) {
        Write-Host "[FAIL] $($service.Name) (PID $servicePid) is still running."
        $failed = $true
        continue
    }

    Remove-Item -LiteralPath $pidFile -Force
}

if ($failed) {
    Write-Host "[FAIL] Some services could not be stopped safely; see messages above."
    exit 1
}

Write-Host "[OK] All tracked services stopped."
