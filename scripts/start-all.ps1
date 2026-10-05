[CmdletBinding()]
param(
    [switch]$Stop,
    [switch]$SkipMT5
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

# ─── helpers ────────────────────────────────────────────────────────────────────
function Write-Info  { Write-Host "[INFO]  $args" -ForegroundColor Cyan }
function Write-Ok    { Write-Host "[OK]    $args" -ForegroundColor Green }
function Write-Fail  { Write-Host "[FAIL]  $args" -ForegroundColor Red }
function Write-Warn  { Write-Host "[WARN]  $args" -ForegroundColor Yellow }

# ─── environment ───────────────────────────────────────────────────────────────
$envFile = Join-Path $root ".env.local"
if (-not (Test-Path $envFile)) {
    $envFile = Join-Path $root ".env"
}
if (Test-Path $envFile) {
    Write-Info "Loading environment from $envFile"
    Get-Content -LiteralPath $envFile | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith("#")) {
            $kv = $line -split "=", 2
            if ($kv.Count -eq 2) {
                $name = $kv[0].Trim()
                $val  = $kv[1].Trim()
                if (-not [string]::IsNullOrWhiteSpace($name)) {
                    [System.Environment]::SetEnvironmentVariable($name, $val, "Process")
                }
            }
        }
    }
} else {
    Write-Warn "No .env or .env.local found - using defaults only"
}

# ─── python / venv ─────────────────────────────────────────────────────────────
$venvDir  = Join-Path $root ".venv"
$python   = Join-Path (Join-Path $venvDir "Scripts") "python.exe"
$uvicorn  = Join-Path (Join-Path $venvDir "Scripts") "uvicorn.exe"

if (-not (Test-Path $python)) {
    Write-Info "Creating virtual environment..."
    & py -3 -m venv $venvDir
    Write-Info "Installing dependencies..."
    & $python -m pip install --upgrade pip
    & $python -m pip install -r (Join-Path $root "requirements.txt")
}

if (-not (Test-Path $uvicorn)) {
    Write-Fail "uvicorn not found in venv. Run .\scripts\run-tests.ps1 first."
    exit 1
}

# ─── dirs ──────────────────────────────────────────────────────────────────────
$logsDir = Join-Path $root "logs"
New-Item -ItemType Directory -Path $logsDir -Force | Out-Null

# ─── config ────────────────────────────────────────────────────────────────────
function Env-OrDefault { param([string]$Name, [string]$Default)
    $v = [System.Environment]::GetEnvironmentVariable($Name, "Process")
    if ([string]::IsNullOrEmpty($v)) { return $Default }
    return $v
}
$API_PORT                = [int](Env-OrDefault "API_PORT" "8000")
$MARKET_DATA_INGEST_PORT = [int](Env-OrDefault "MARKET_DATA_INGEST_PORT" "8020")
$RECONCILIATION_PORT     = [int](Env-OrDefault "RECONCILIATION_PORT" "8010")
$RISK_ENGINE_PORT        = [int](Env-OrDefault "RISK_ENGINE_PORT" "8001")
$PATTERN_ENGINE_PORT     = [int](Env-OrDefault "PATTERN_ENGINE_PORT" "8002")
$AI_ENGINE_PORT          = [int](Env-OrDefault "AI_ENGINE_PORT" "8003")
$EXECUTION_ENGINE_PORT   = [int](Env-OrDefault "EXECUTION_ENGINE_PORT" "8004")
$TELEGRAM_BOT_PORT       = [int](Env-OrDefault "TELEGRAM_BOT_PORT" "8005")
$MARKET_ENGINE_PORT      = [int](Env-OrDefault "MARKET_ENGINE_PORT" "8006")
$TRADE_MONITOR_PORT      = [int](Env-OrDefault "TRADE_MONITOR_PORT" "8007")
$MT5_ADAPTER_PORT        = [int](Env-OrDefault "MT5_ADAPTER_PORT" "8765")
$LOG_LEVEL               = Env-OrDefault "LOG_LEVEL" "info"
$DATABASE_URL            = Env-OrDefault "DATABASE_URL" ""
$REDIS_URL               = Env-OrDefault "REDIS_URL" "redis://localhost:6379/0"
$REDIS_HOST              = Env-OrDefault "REDIS_HOST" "localhost"
$POSTGRES_HOST           = Env-OrDefault "POSTGRES_HOST" "localhost"
$MT5_MARKET_DATA_ENABLED = Env-OrDefault "MT5_MARKET_DATA_ENABLED" "true"
$SECRET_KEY              = Env-OrDefault "SECRET_KEY" ""
$ORDER_SIGNING_SECRET    = Env-OrDefault "ORDER_SIGNING_SECRET" $SECRET_KEY
$RECONCILIATION_API_TOKEN= Env-OrDefault "RECONCILIATION_API_TOKEN" ""

# ─── pid helpers ───────────────────────────────────────────────────────────────
function New-PidFile {
    param([string]$Name, [System.Diagnostics.Process]$Proc)
    $pidFile = Join-Path $logsDir "$Name.pid"
    Set-Content -LiteralPath $pidFile -Value $Proc.Id -NoNewline
}

function Get-ServicePid {
    param([string]$Name)
    $pidFile = Join-Path $logsDir "$Name.pid"
    if (Test-Path $pidFile) {
        return [int](Get-Content -LiteralPath $pidFile -Raw)
    }
    return $null
}

# ─── service stop helper ───────────────────────────────────────────────────────
function Stop-LocalService {
    param([string]$Name)
    $pidFile = Join-Path $logsDir "$Name.pid"
    if (-not (Test-Path $pidFile)) { return }
    $servicePid = [int](Get-Content -LiteralPath $pidFile -Raw)
    $proc = Get-Process -Id $servicePid -ErrorAction SilentlyContinue
    if ($proc) {
        Write-Info "Stopping $Name (PID $servicePid)..."
        Stop-Process -Id $servicePid -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 1
    }
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
}

# ─── stop mode ────────────────────────────────────────────────────────────────
if ($Stop) {
    Write-Info "Stopping all services..."
    @(
        "api-gateway",
        "market-data",
        "reconciliation",
        "risk-engine",
        "pattern-engine",
        "ai-engine",
        "execution-engine",
        "telegram-bot",
        "market-engine",
        "trade-monitor",
        "mt5-host-adapter"
    ) | ForEach-Object { Stop-LocalService -Name $_ }
    Write-Ok "All services stopped."
    exit 0
}

# ─── stop existing ─────────────────────────────────────────────────────────────
Write-Info "Stopping any previously running services..."
@(
    "api-gateway",
    "market-data",
    "reconciliation",
    "risk-engine",
    "pattern-engine",
    "ai-engine",
    "execution-engine",
    "telegram-bot",
    "market-engine",
    "trade-monitor",
    "mt5-host-adapter"
) | ForEach-Object { Stop-LocalService -Name $_ }

# ─── Redis ───────────────────────────────────────────────────────────────────
Write-Info "Using the configured shared Redis-compatible event bus."

# ─── inter-service env vars (localhost defaults) ───────────────────────────────
[System.Environment]::SetEnvironmentVariable("MARKET_DATA_INGEST_URL", "http://127.0.0.1:$MARKET_DATA_INGEST_PORT", "Process")
[System.Environment]::SetEnvironmentVariable("RECONCILIATION_URL", "http://127.0.0.1:$RECONCILIATION_PORT", "Process")
[System.Environment]::SetEnvironmentVariable("MT5_ADAPTER_URL", "http://127.0.0.1:$MT5_ADAPTER_PORT", "Process")

# ─── start services ────────────────────────────────────────────────────────────
function Start-Proc {
    param(
        [string]$Name,
        [string]$Module,
        [int]$Port,
        [hashtable]$EnvVars
    )
    $envVars["LOG_LEVEL"] = $LOG_LEVEL
    $envVars["API_GATEWAY_KEY"] = Env-OrDefault "API_GATEWAY_KEY" ""
    $envVars["MT5_ADAPTER_TOKEN"] = Env-OrDefault "MT5_ADAPTER_TOKEN" ""

    $argsList = @("-m", "uvicorn", $Module, "--host", "0.0.0.0", "--port", "$Port", "--log-level", $LOG_LEVEL)
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $python
    $startInfo.Arguments = $argsList -join " "
    $startInfo.WorkingDirectory = $root
    $startInfo.UseShellExecute = $false
    $startInfo.RedirectStandardOutput = $false
    $startInfo.RedirectStandardError = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
    # ProcessStartInfo can start with an empty environment block on Windows.
    # Copy the current process environment so values loaded from .env.local
    # (notably DATABASE_URL and feature flags) reach every service.
    Get-ChildItem Env: | ForEach-Object {
        $startInfo.Environment[$_.Name] = $_.Value
    }
    foreach ($kv in $envVars.GetEnumerator()) {
        $startInfo.Environment[$kv.Key] = $kv.Value
    }
    $proc = [System.Diagnostics.Process]::Start($startInfo)
    New-PidFile -Name $Name -Proc $proc
    Write-Info "Started $Name (PID $($proc.Id), port $Port)"
    return $proc
}

$services = @(
    @{ Name = "api-gateway";        Module = "services.api_gateway.app.main:app";          Port = $API_PORT;                Env = @{} }
    @{ Name = "risk-engine";        Module = "services.risk_engine.app.main:app";           Port = $RISK_ENGINE_PORT;        Env = @{} }
    @{ Name = "pattern-engine";     Module = "services.pattern_engine.app.main:app";        Port = $PATTERN_ENGINE_PORT;     Env = @{} }
    @{ Name = "ai-engine";          Module = "services.ai_engine.app.main:app";             Port = $AI_ENGINE_PORT;          Env = @{} }
    @{ Name = "execution-engine";   Module = "services.execution_engine.app.main:app";      Port = $EXECUTION_ENGINE_PORT;   Env = @{} }
    @{ Name = "market-data";        Module = "services.market_data.app.main:app";           Port = $MARKET_DATA_INGEST_PORT; Env = @{} }
    @{ Name = "market-engine";      Module = "services.market_engine.app.main:app";         Port = $MARKET_ENGINE_PORT;      Env = @{} }
    @{ Name = "reconciliation";     Module = "services.reconciliation.app.main:app";        Port = $RECONCILIATION_PORT;     Env = @{} }
    @{ Name = "telegram-bot";       Module = "services.telegram_bot.app.main:app";          Port = $TELEGRAM_BOT_PORT;       Env = @{} }
    @{ Name = "trade-monitor";      Module = "services.trade_monitor.app.main:app";         Port = $TRADE_MONITOR_PORT;      Env = @{} }
)

Write-Info "Starting services..."
foreach ($svc in $services) {
    Start-Proc -Name $svc.Name -Module $svc.Module -Port $svc.Port -EnvVars $svc.Env | Out-Null
}

# ─── MT5 host adapter ──────────────────────────────────────────────────────────
if ($MT5_MARKET_DATA_ENABLED -eq "true" -and -not $SkipMT5) {
    $mt5Module = "scripts.mt5_host_adapter:app"
    $mt5Args = @("-m", "uvicorn", $mt5Module, "--host", "127.0.0.1", "--port", "$MT5_ADAPTER_PORT", "--log-level", $LOG_LEVEL)
    $mt5StartInfo = New-Object System.Diagnostics.ProcessStartInfo
    $mt5StartInfo.FileName = $python
    $mt5StartInfo.Arguments = $mt5Args -join " "
    $mt5StartInfo.WorkingDirectory = $root
    $mt5StartInfo.UseShellExecute = $false
    $mt5StartInfo.CreateNoWindow = $true
    $mt5StartInfo.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
    Get-ChildItem Env: | ForEach-Object {
        $mt5StartInfo.Environment[$_.Name] = $_.Value
    }
    $mt5Proc = [System.Diagnostics.Process]::Start($mt5StartInfo)
    New-PidFile -Name "mt5-host-adapter" -Proc $mt5Proc
    Write-Info "Started MT5 host adapter (PID $($mt5Proc.Id), port $MT5_ADAPTER_PORT)"
}

# ─── wait for health ───────────────────────────────────────────────────────────
function Wait-For-Health {
    param([string]$Name, [string]$Url, [int]$TimeoutSec = 60)
    $end = (Get-Date).AddSeconds($TimeoutSec)
    Write-Info "Waiting for $Name at $Url ..."
    while ((Get-Date) -lt $end) {
        try {
            $r = Invoke-RestMethod -Uri $Url -TimeoutSec 3 -ErrorAction SilentlyContinue
            if ($r.status -eq "ok") {
                Write-Ok "$Name is healthy"
                return $true
            }
            if ($SkipMT5 -and $r.status -eq "degraded") {
                Write-Warn "$Name is responding in safe local mode (MT5-dependent capability unavailable)"
                return $true
            }
            if ($Name -eq "api-gateway" -and $r.status -eq "degraded" -and $r.services) {
                $telegram = $null
                $otherServicesHealthy = $true
                foreach ($service in $r.services.PSObject.Properties) {
                    if ($service.Name -eq "telegram-bot") {
                        $telegram = $service.Value
                    } elseif ($service.Value.status -ne "ok") {
                        $otherServicesHealthy = $false
                    }
                }
                if (
                    $telegram -and
                    $telegram.status -eq "degraded" -and
                    $telegram.telegram -eq "disabled" -and
                    $otherServicesHealthy
                ) {
                    Write-Warn "$Name is healthy; optional Telegram integration is disabled"
                    return $true
                }
            }
        } catch { }
        Start-Sleep -Milliseconds 500
    }
    Write-Warn "$Name did not become healthy within ${TimeoutSec}s"
    return $false
}

Write-Info "Waiting for services to become healthy..."
$allHealthy = $true
$allHealthy = $allHealthy -and (Wait-For-Health -Name "api-gateway"    -Url "http://127.0.0.1:$API_PORT/health"        -TimeoutSec 60)
$allHealthy = $allHealthy -and (Wait-For-Health -Name "market-data"     -Url "http://127.0.0.1:$MARKET_DATA_INGEST_PORT/health" -TimeoutSec 60)
$allHealthy = $allHealthy -and (Wait-For-Health -Name "risk-engine"     -Url "http://127.0.0.1:$RISK_ENGINE_PORT/health"         -TimeoutSec 60)
$allHealthy = $allHealthy -and (Wait-For-Health -Name "pattern-engine"  -Url "http://127.0.0.1:$PATTERN_ENGINE_PORT/health"      -TimeoutSec 60)
$allHealthy = $allHealthy -and (Wait-For-Health -Name "ai-engine"       -Url "http://127.0.0.1:$AI_ENGINE_PORT/health"           -TimeoutSec 60)
$allHealthy = $allHealthy -and (Wait-For-Health -Name "execution-engine"-Url "http://127.0.0.1:$EXECUTION_ENGINE_PORT/health"    -TimeoutSec 60)
$allHealthy = $allHealthy -and (Wait-For-Health -Name "reconciliation"  -Url "http://127.0.0.1:$RECONCILIATION_PORT/health"      -TimeoutSec 90)
$allHealthy = $allHealthy -and (Wait-For-Health -Name "telegram-bot"    -Url "http://127.0.0.1:$TELEGRAM_BOT_PORT/health"        -TimeoutSec 60)
$allHealthy = $allHealthy -and (Wait-For-Health -Name "market-engine"   -Url "http://127.0.0.1:$MARKET_ENGINE_PORT/health"       -TimeoutSec 90)
$allHealthy = $allHealthy -and (Wait-For-Health -Name "trade-monitor"   -Url "http://127.0.0.1:$TRADE_MONITOR_PORT/health"       -TimeoutSec 60)

if ($allHealthy) {
    if ($SkipMT5) {
        Write-Ok "All local services are running (MT5-dependent services may be safely degraded)."
    } else {
        Write-Ok "All services are healthy."
    }
    Write-Host ""
    Write-Host "  api-gateway       http://127.0.0.1:$API_PORT"
    Write-Host "  market-data       http://127.0.0.1:$MARKET_DATA_INGEST_PORT"
    Write-Host "  reconciliation    http://127.0.0.1:$RECONCILIATION_PORT"
    Write-Host "  risk-engine       http://127.0.0.1:$RISK_ENGINE_PORT"
    Write-Host "  pattern-engine    http://127.0.0.1:$PATTERN_ENGINE_PORT"
    Write-Host "  ai-engine         http://127.0.0.1:$AI_ENGINE_PORT"
    Write-Host "  execution-engine  http://127.0.0.1:$EXECUTION_ENGINE_PORT"
    Write-Host "  telegram-bot      http://127.0.0.1:$TELEGRAM_BOT_PORT"
    Write-Host "  market-engine     http://127.0.0.1:$MARKET_ENGINE_PORT"
    Write-Host "  trade-monitor     http://127.0.0.1:$TRADE_MONITOR_PORT"
    if ($MT5_MARKET_DATA_ENABLED -eq "true" -and -not $SkipMT5) {
        Write-Host "  mt5-host-adapter  http://127.0.0.1:$MT5_ADAPTER_PORT"
    }
    Write-Host ""
    Write-Host "Stop with: .\scripts\stop-all.ps1"
} else {
    Write-Warn "Some services did not become healthy - check logs/ for details."
    exit 1
}
