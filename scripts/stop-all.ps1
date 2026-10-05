[CmdletBinding()]
param(
    [switch]$Force
)

$ErrorActionPreference = "Continue"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$logsDir = Join-Path $root "logs"

function Write-Info { Write-Host "[INFO] $args" -ForegroundColor Cyan }
function Write-Ok   { Write-Host "[OK]   $args" -ForegroundColor Green }

function Stop-ProcessByPidFile {
    param([string]$Name)
    $pidFile = Join-Path $logsDir "$name.pid"
    if (-not (Test-Path $pidFile)) { return $false }
    $raw = Get-Content -LiteralPath $pidFile -Raw
    $pid = 0
    if (-not [int]::TryParse($raw, [ref]$pid)) { return $false }
    $proc = Get-Process -Id $pid -ErrorAction SilentlyContinue
    if ($proc) {
        Write-Info "Stopping $Name (PID $pid)..."
        Stop-Process -Id $pid -Force:$Force -ErrorAction SilentlyContinue
        Start-Sleep -Milliseconds 500
    }
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
    return $true
}

function Stop-ProcessByName {
    param([string]$ProcessName)
    $procs = Get-Process -Name $ProcessName -ErrorAction SilentlyContinue
    if ($procs) {
        foreach ($p in $procs) {
            Write-Info "Stopping $ProcessName (PID $($p.Id))..."
            Stop-Process -Id $p.Id -Force:$Force -ErrorAction SilentlyContinue
        }
    }
}

$stopped = $false

$serviceNames = @(
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
)

Write-Info "Stopping services from PID files..."
foreach ($name in $serviceNames) {
    if (Stop-ProcessByPidFile -Name $name) { $stopped = $true }
}

if (-not $stopped) {
    Write-Info "No PID files found — falling back to process-name scan..."
    Stop-ProcessByName -ProcessName "uvicorn"
    Stop-ProcessByName -ProcessName "python"
}

Write-Ok "All services stopped."
