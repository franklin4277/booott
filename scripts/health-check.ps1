[CmdletBinding()]
param(
    [string]$ProjectDirectory = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [string]$NetworkHost = "1.1.1.1",
    [int]$NetworkPort = 443,
    [string]$HeartbeatPath,
    [int]$MinimumFreeMemoryMB = 1024,
    [int]$MaximumCpuPercent = 95
)

$ErrorActionPreference = "Continue"
$failed = $false

function Write-Check {
    param(
        [string]$Name,
        [bool]$Passed,
        [string]$Message,
        [switch]$Warning
    )
    if ($Passed) {
        Write-Host "[PASS] $Name`: $Message" -ForegroundColor Green
        return
    }
    if ($Warning) {
        Write-Host "[WARN] $Name`: $Message" -ForegroundColor Yellow
        return
    }
    Write-Host "[FAIL] $Name`: $Message" -ForegroundColor Red
    $script:failed = $true
}

$operatingSystem = Get-CimInstance Win32_OperatingSystem
$freeMemoryMB = [math]::Round($operatingSystem.FreePhysicalMemory / 1024)
$totalMemoryMB = [math]::Round($operatingSystem.TotalVisibleMemorySize / 1024)
Write-Check `
    -Name "RAM" `
    -Passed ($freeMemoryMB -ge $MinimumFreeMemoryMB) `
    -Message ("{0:N0} MB free / {1:N0} MB total" -f $freeMemoryMB, $totalMemoryMB)

$processors = @(Get-CimInstance Win32_Processor)
$cpuPercent = [math]::Round(
    ($processors | Measure-Object -Property LoadPercentage -Average).Average
)
Write-Check `
    -Name "CPU" `
    -Passed ($cpuPercent -lt $MaximumCpuPercent) `
    -Message ("{0}% average load (limit {1}%)" -f $cpuPercent, $MaximumCpuPercent)

try {
    $networkResult = Test-NetConnection `
        -ComputerName $NetworkHost `
        -Port $NetworkPort `
        -InformationLevel Quiet `
        -WarningAction SilentlyContinue
    Write-Check `
        -Name "Network" `
        -Passed ([bool]$networkResult) `
        -Message ("TCP connectivity to {0}:{1}" -f $NetworkHost, $NetworkPort)
} catch {
    Write-Check -Name "Network" -Passed $false -Message $_.Exception.Message
}

$terminal = Get-Process -Name "terminal64" -ErrorAction SilentlyContinue |
    Select-Object -First 1
Write-Check `
    -Name "MT5 process" `
    -Passed ($null -ne $terminal) `
    -Message $(if ($terminal) { "PID $($terminal.Id)" } else { "terminal64.exe is not running" })

if (-not $HeartbeatPath) {
    $HeartbeatPath = Join-Path $env:APPDATA "MetaQuotes\Terminal\Common\Files\mt5_watchdog_heartbeat.txt"
}
$heartbeatFresh = $false
if (Test-Path -LiteralPath $HeartbeatPath) {
    $heartbeatAge = (Get-Date) - (Get-Item -LiteralPath $HeartbeatPath).LastWriteTime
    $heartbeatFresh = $heartbeatAge.TotalSeconds -le 45
}
Write-Check `
    -Name "MT5 heartbeat" `
    -Passed $heartbeatFresh `
    -Message $(if ($heartbeatFresh) {
        "updated within 45 seconds"
    } else {
        "missing or older than 45 seconds: $HeartbeatPath"
    })

$adapterPort = 8765
$envFile = Join-Path $ProjectDirectory ".env.local"
if (-not (Test-Path $envFile)) { $envFile = Join-Path $ProjectDirectory ".env" }
if (Test-Path -LiteralPath $envFile) {
    $portLine = Get-Content -LiteralPath $envFile | Where-Object {
        $_ -match "^\s*MT5_ADAPTER_PORT\s*="
    } | Select-Object -First 1
    if ($portLine) {
        $adapterPort = [int](($portLine -split "=", 2)[1].Trim())
    }
}
try {
    $adapterResult = Test-NetConnection `
        -ComputerName "127.0.0.1" `
        -Port $adapterPort `
        -InformationLevel Quiet `
        -WarningAction SilentlyContinue
    Write-Check `
        -Name "MT5 host adapter" `
        -Passed ([bool]$adapterResult) `
        -Message ("TCP listener on 127.0.0.1:{0}" -f $adapterPort)
} catch {
    Write-Check -Name "MT5 host adapter" -Passed $false -Message $_.Exception.Message
}

$services = @(
    @{ Name = "Reconciliation / SAFE_MODE"; Uri = "http://127.0.0.1:8010/health"; SafeMode = $true }
    @{ Name = "API Gateway";               Uri = "http://127.0.0.1:8000/health"; SafeMode = $false }
    @{ Name = "Market Engine";              Uri = "http://127.0.0.1:8006/health"; SafeMode = $false }
    @{ Name = "Trade Monitor";              Uri = "http://127.0.0.1:8007/health"; SafeMode = $false }
)

foreach ($svc in $services) {
    try {
        $resp = Invoke-RestMethod -Uri $svc.Uri -TimeoutSec 5
        $ok = $resp.status -eq "ok"
        if ($svc.SafeMode) {
            $ok = $ok -and $resp.safe_mode -eq "disabled"
            Write-Check `
                -Name $svc.Name `
                -Passed $ok `
                -Message ("status={0}, safe_mode={1}" -f $resp.status, $resp.safe_mode)
        } else {
            Write-Check `
                -Name $svc.Name `
                -Passed $ok `
                -Message ("status={0}" -f $resp.status)
        }
    } catch {
        Write-Check -Name $svc.Name -Passed $false -Message $_.Exception.Message
    }
}

if ($failed) {
    exit 1
}
exit 0
