[CmdletBinding()]
param(
    [string]$ProjectDirectory = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [string]$DockerServiceName = "docker",
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

try {
    $dockerService = Get-Service -Name $DockerServiceName -ErrorAction Stop
    Write-Check `
        -Name "Docker service" `
        -Passed ($dockerService.Status -eq "Running") `
        -Message ("{0} is {1}" -f $DockerServiceName, $dockerService.Status)
} catch {
    Write-Check -Name "Docker service" -Passed $false -Message $_.Exception.Message
}

$adapterPort = 8765
$envFile = Join-Path $ProjectDirectory ".env"
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

$envFile = Join-Path $ProjectDirectory ".env"
$composeFile = Join-Path $ProjectDirectory "docker-compose.prod.yml"
if ((Test-Path -LiteralPath $envFile) -and (Test-Path -LiteralPath $composeFile)) {
    try {
        $composeOutput = & docker compose `
            --project-directory $ProjectDirectory `
            --env-file $envFile `
            -f $composeFile `
            ps --all --format json 2>&1
        if ($LASTEXITCODE -ne 0) {
            throw ($composeOutput | Out-String)
        }
        $rawCompose = $composeOutput | Out-String
        $containers = @()
        if (-not [string]::IsNullOrWhiteSpace($rawCompose)) {
            try {
                $parsed = ConvertFrom-Json -InputObject $rawCompose -ErrorAction Stop
                if ($parsed -is [array]) {
                    $containers = @($parsed)
                } else {
                    $containers = @($parsed)
                }
            } catch {
                foreach ($line in $rawCompose -split "`r?`n") {
                    if (-not [string]::IsNullOrWhiteSpace($line)) {
                        $containers += ConvertFrom-Json -InputObject $line -ErrorAction Stop
                    }
                }
            }
        }
        $servicesOutput = & docker compose `
            --project-directory $ProjectDirectory `
            --env-file $envFile `
            -f $composeFile `
            config --services
        if ($LASTEXITCODE -ne 0) {
            throw "Could not list services in the production Compose configuration."
        }
        $missing = @()
        $unhealthy = @()
        foreach ($service in $servicesOutput) {
            $matches = @($containers | Where-Object { $_.Service -eq $service })
            if ($matches.Count -eq 0 -or @($matches | Where-Object {
                $_.State -notin @("running", "up")
            }).Count -gt 0) {
                $missing += $service
            } elseif (@($matches | Where-Object {
                $_.Health -eq "unhealthy" -or $_.Status -match "\(unhealthy\)"
            }).Count -gt 0) {
                $unhealthy += $service
            }
        }
        $composeHealthy = $missing.Count -eq 0 -and $unhealthy.Count -eq 0
        $composeMessage = if ($composeHealthy) {
            "all $($servicesOutput.Count) configured services are running"
        } else {
            "stopped/missing=[$($missing -join ',')], unhealthy=[$($unhealthy -join ',')]"
        }
        Write-Check -Name "Docker containers" -Passed $composeHealthy -Message $composeMessage
    } catch {
        Write-Check -Name "Docker containers" -Passed $false -Message $_.Exception.Message
    }
} else {
    Write-Check `
        -Name "Docker containers" `
        -Passed $false `
        -Message "Missing production .env or docker-compose.prod.yml."
}

$reconciliationUri = "http://127.0.0.1:8010/health"
try {
    $reconciliation = Invoke-RestMethod -Uri $reconciliationUri -TimeoutSec 5
    $reconciliationOk = $reconciliation.status -eq "ok" -and
        $reconciliation.safe_mode -eq "disabled"
    Write-Check `
        -Name "Reconciliation / SAFE_MODE" `
        -Passed $reconciliationOk `
        -Message ("status={0}, safe_mode={1}" -f $reconciliation.status, $reconciliation.safe_mode)
} catch {
    Write-Check -Name "Reconciliation API" -Passed $false -Message $_.Exception.Message
}

if ($failed) {
    exit 1
}
exit 0
