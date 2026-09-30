[CmdletBinding()]
param(
    [string]$TerminalPath,
    [string]$RunAsUser,
    [string]$DockerServiceName = "docker",
    [string]$PythonPath,
    [switch]$SkipComposeDeploy
)

$ErrorActionPreference = "Stop"

function Assert-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Run install-production.ps1 from an elevated PowerShell session."
    }
}

function Read-DotEnvValue {
    param([string]$Path, [string]$Name)
    $line = Get-Content -LiteralPath $Path | Where-Object {
        $_ -match "^\s*$([Regex]::Escape($Name))\s*="
    } | Select-Object -First 1
    if (-not $line) {
        return $null
    }
    $value = ($line -split "=", 2)[1].Trim()
    if (($value.StartsWith('"') -and $value.EndsWith('"')) -or
        ($value.StartsWith("'") -and $value.EndsWith("'"))) {
        $value = $value.Substring(1, $value.Length - 2)
    }
    return $value
}

Assert-Administrator
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$envFile = Join-Path $root ".env"
$composeFile = Join-Path $root "docker-compose.prod.yml"
$startupConfig = Join-Path $PSScriptRoot "mt5_startup.ini"
$watchdogScript = Join-Path $PSScriptRoot "host_watchdog.py"
$updateScript = Join-Path $PSScriptRoot "configure-windows-update.ps1"
$hostAdapterScript = Join-Path $PSScriptRoot "run_mt5_host_adapter.ps1"

if (-not (Test-Path -LiteralPath $envFile)) {
    throw "Missing $envFile. Create it from .env.example and set production secrets."
}
if (-not (Test-Path -LiteralPath $composeFile)) {
    throw "Missing production Compose file: $composeFile"
}
foreach ($requiredPath in @($startupConfig, $watchdogScript, $updateScript, $hostAdapterScript)) {
    if (-not (Test-Path -LiteralPath $requiredPath)) {
        throw "Required deployment file is missing: $requiredPath"
    }
}

if (-not $TerminalPath) {
    $TerminalPath = Read-DotEnvValue -Path $envFile -Name "MT5_TERMINAL_PATH"
}
if (-not $TerminalPath) {
    $TerminalPath = Join-Path ${env:ProgramFiles} "MetaTrader 5\terminal64.exe"
}
$TerminalPath = (Resolve-Path -LiteralPath $TerminalPath).Path
if ((Split-Path -Leaf $TerminalPath) -ne "terminal64.exe") {
    throw "TerminalPath must identify terminal64.exe."
}

if (-not $PythonPath) {
    $PythonPath = (& py -3 -c "import sys; print(sys.executable)" | Select-Object -First 1)
}
$PythonPath = (Resolve-Path -LiteralPath $PythonPath).Path
$pythonCheck = & $PythonPath -c "import MetaTrader5, psutil, fastapi, uvicorn, prometheus_client"
if ($LASTEXITCODE -ne 0) {
    throw "Install host dependencies with '$PythonPath -m pip install -r requirements-mt5-host.txt' before deployment."
}

$iniText = Get-Content -LiteralPath $startupConfig -Raw
foreach ($placeholder in @(
    "REPLACE_WITH_MT5_ACCOUNT_NUMBER",
    "REPLACE_WITH_MT5_PASSWORD",
    "REPLACE_WITH_BROKER_SERVER:PORT"
)) {
    if ($iniText.Contains($placeholder)) {
        throw "Replace $placeholder in scripts\mt5_startup.ini before production install."
    }
}

$adapterToken = Read-DotEnvValue -Path $envFile -Name "MT5_ADAPTER_TOKEN"
$reconciliationToken = Read-DotEnvValue -Path $envFile -Name "RECONCILIATION_API_TOKEN"
if (-not $adapterToken -or $adapterToken.Length -lt 32 -or $adapterToken -match "(?i)replace|change-me") {
    throw "MT5_ADAPTER_TOKEN in .env must contain at least 32 characters."
}
if (-not $reconciliationToken -or $reconciliationToken.Length -lt 32 -or $reconciliationToken -match "(?i)replace|change-me") {
    throw "RECONCILIATION_API_TOKEN in .env must contain at least 32 characters."
}
if ($adapterToken -ceq $reconciliationToken) {
    throw "MT5_ADAPTER_TOKEN and RECONCILIATION_API_TOKEN must be different secrets."
}
foreach ($setting in @("POSTGRES_PASSWORD", "REDIS_PASSWORD", "ORDER_SIGNING_SECRET", "GRAFANA_ADMIN_PASSWORD")) {
    $value = Read-DotEnvValue -Path $envFile -Name $setting
    if (-not $value -or $value -match "(?i)replace|change-me") {
        throw "$setting in .env must be replaced with a production value."
    }
}
$orderSigningSecret = Read-DotEnvValue -Path $envFile -Name "ORDER_SIGNING_SECRET"
if ($orderSigningSecret.Length -lt 32) {
    throw "ORDER_SIGNING_SECRET in .env must contain at least 32 characters."
}

$adapterPort = Read-DotEnvValue -Path $envFile -Name "MT5_ADAPTER_PORT"
if (-not $adapterPort) {
    $adapterPort = "8765"
}
$watchdogPoll = Read-DotEnvValue -Path $envFile -Name "HOST_WATCHDOG_POLL_SECONDS"
if (-not $watchdogPoll) { $watchdogPoll = "10" }
$heartbeatTimeout = Read-DotEnvValue -Path $envFile -Name "MT5_WATCHDOG_HEARTBEAT_TIMEOUT_SECONDS"
if (-not $heartbeatTimeout) { $heartbeatTimeout = "45" }
$startupGrace = Read-DotEnvValue -Path $envFile -Name "MT5_WATCHDOG_STARTUP_GRACE_SECONDS"
if (-not $startupGrace) { $startupGrace = "120" }
$restartGrace = Read-DotEnvValue -Path $envFile -Name "MT5_WATCHDOG_RESTART_GRACE_SECONDS"
if (-not $restartGrace) { $restartGrace = "20" }
$dockerCooldown = Read-DotEnvValue -Path $envFile -Name "DOCKER_RESTART_COOLDOWN_SECONDS"
if (-not $dockerCooldown) { $dockerCooldown = "60" }
$terminalDirectory = Split-Path -Parent $TerminalPath

if (-not $RunAsUser) {
    $RunAsUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
}
$runAsCredential = Get-Credential -UserName $RunAsUser `
    -Message "Credentials for the Windows account that owns the MT5 terminal profile"
if ($runAsCredential.UserName -ne $RunAsUser) {
    throw "Credential username does not match RunAsUser."
}
$runAsAccount = New-Object System.Security.Principal.NTAccount($runAsCredential.UserName)
$runAsSid = $runAsAccount.Translate(
    [System.Security.Principal.SecurityIdentifier]
).Value
$runAsProfile = Get-CimInstance Win32_UserProfile |
    Where-Object { $_.SID -eq $runAsSid } |
    Select-Object -First 1
if (-not $runAsProfile) {
    throw "The MT5 run-as account must have logged on once so Windows creates its user profile."
}
$heartbeatPath = Join-Path $runAsProfile.LocalPath "AppData\Roaming\MetaQuotes\Terminal\Common\Files\mt5_watchdog_heartbeat.txt"

$dockerService = Get-Service -Name $DockerServiceName -ErrorAction Stop
Set-Service -Name $DockerServiceName -StartupType Automatic
if ($dockerService.Status -ne "Running") {
    Start-Service -Name $DockerServiceName
    (Get-Service -Name $DockerServiceName).WaitForStatus(
        [System.ServiceProcess.ServiceControllerStatus]::Running,
        [TimeSpan]::FromSeconds(60)
    )
}
$containerOs = & docker info --format "{{.OSType}}"
if ($LASTEXITCODE -ne 0) {
    throw "Could not query Docker Engine. Verify the configured Docker service/context."
}
if ($containerOs.Trim() -ne "linux") {
    throw "This Compose stack requires a Linux Docker Engine; the selected engine reports '$containerOs'. On Windows Server, use a supported Linux host/VM or Docker context and configure that engine's autostart separately."
}

# Keep broker credentials readable only by the terminal account, SYSTEM and Administrators.
$iniAcl = New-Object System.Security.AccessControl.FileSecurity
$iniAcl.SetAccessRuleProtection($true, $false)
$adminSid = New-Object System.Security.Principal.SecurityIdentifier("S-1-5-32-544")
$systemSid = New-Object System.Security.Principal.SecurityIdentifier("S-1-5-18")
$userSid = $runAsCredential.UserName
try {
    $userSid = $runAsAccount.Translate(
        [System.Security.Principal.SecurityIdentifier]
    )
} catch {
    throw "Could not resolve deployment account $($runAsCredential.UserName) to a Windows SID."
}
$readWrite = [System.Security.AccessControl.FileSystemRights]::Modify
$inheritance = [System.Security.AccessControl.InheritanceFlags]::None
$propagation = [System.Security.AccessControl.PropagationFlags]::None
$allow = [System.Security.AccessControl.AccessControlType]::Allow
foreach ($sid in @($adminSid, $systemSid, $userSid)) {
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
        $sid, $readWrite, $inheritance, $propagation, $allow
    )
    $iniAcl.AddAccessRule($rule)
}
Set-Acl -LiteralPath $startupConfig -AclObject $iniAcl

$terminalAction = New-ScheduledTaskAction `
    -Execute $TerminalPath `
    -Argument ('/config:"{0}"' -f $startupConfig) `
    -WorkingDirectory $terminalDirectory
$terminalTrigger = New-ScheduledTaskTrigger -AtStartup
$taskSettings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)
$terminalPrincipal = New-ScheduledTaskPrincipal `
    -UserId $runAsCredential.UserName `
    -LogonType Password `
    -RunLevel Highest
$terminalTask = New-ScheduledTask `
    -Action $terminalAction `
    -Trigger $terminalTrigger `
    -Settings $taskSettings `
    -Principal $terminalPrincipal
Register-ScheduledTask `
    -TaskName "MT5-Terminal-Autostart" `
    -InputObject $terminalTask `
    -User $runAsCredential.UserName `
    -Password $runAsCredential.GetNetworkCredential().Password `
    -Force | Out-Null

$watchdogArguments = @(
    "-u",
    ('"{0}"' -f $watchdogScript),
    "--terminal-path",
    ('"{0}"' -f $TerminalPath),
    "--startup-config",
    ('"{0}"' -f $startupConfig),
    "--heartbeat-path",
    ('"{0}"' -f $heartbeatPath),
    "--project-directory",
    ('"{0}"' -f $root),
    "--compose-file",
    ('"{0}"' -f $composeFile),
    "--poll-seconds",
    $watchdogPoll,
    "--initial-delay-seconds",
    "30",
    "--heartbeat-timeout-seconds",
    $heartbeatTimeout,
    "--startup-grace-seconds",
    $startupGrace,
    "--restart-grace-seconds",
    $restartGrace,
    "--compose-cooldown-seconds",
    $dockerCooldown,
    "--log-file",
    ('"{0}"' -f (Join-Path $root "logs\host_watchdog.log"))
) -join " "
$watchdogAction = New-ScheduledTaskAction `
    -Execute $PythonPath `
    -Argument $watchdogArguments `
    -WorkingDirectory $root
$watchdogPrincipal = New-ScheduledTaskPrincipal `
    -UserId $runAsCredential.UserName `
    -LogonType Password `
    -RunLevel Highest
$watchdogTask = New-ScheduledTask `
    -Action $watchdogAction `
    -Trigger $terminalTrigger `
    -Settings $taskSettings `
    -Principal $watchdogPrincipal
Register-ScheduledTask `
    -TaskName "MT5-Docker-Host-Watchdog" `
    -InputObject $watchdogTask `
    -User $runAsCredential.UserName `
    -Password $runAsCredential.GetNetworkCredential().Password `
    -Force | Out-Null

$adapterPortNumber = [int]$adapterPort
if ($adapterPortNumber -lt 1 -or $adapterPortNumber -gt 65535) {
    throw "MT5_ADAPTER_PORT must be between 1 and 65535."
}
if (-not (Get-NetFirewallRule -DisplayName "MT5 Host Adapter - Private Docker Access" -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule `
        -DisplayName "MT5 Host Adapter - Private Docker Access" `
        -Direction Inbound `
        -Action Allow `
        -Protocol TCP `
        -LocalPort $adapterPortNumber `
        -RemoteAddress LocalSubnet `
        -Profile Domain,Private | Out-Null
}
$adapterAction = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument ('-NoProfile -ExecutionPolicy Bypass -File "{0}" -ProjectDirectory "{1}" -PythonPath "{2}"' -f $hostAdapterScript, $root, $PythonPath) `
    -WorkingDirectory $root
$adapterTask = New-ScheduledTask `
    -Action $adapterAction `
    -Trigger $terminalTrigger `
    -Settings $taskSettings `
    -Principal $terminalPrincipal
Register-ScheduledTask `
    -TaskName "MT5-Host-State-Adapter" `
    -InputObject $adapterTask `
    -User $runAsCredential.UserName `
    -Password $runAsCredential.GetNetworkCredential().Password `
    -Force | Out-Null

# Configure now and re-apply after boot and hourly, since domain policy may overwrite local settings.
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $updateScript
$updateTrigger = New-ScheduledTaskTrigger -AtStartup
$updateDailyTrigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(5) `
    -RepetitionInterval (New-TimeSpan -Hours 1) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$updateAction = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument ('-NoProfile -ExecutionPolicy Bypass -File "{0}"' -f $updateScript)
$systemPrincipal = New-ScheduledTaskPrincipal `
    -UserId "SYSTEM" `
    -LogonType ServiceAccount `
    -RunLevel Highest
$updateTask = New-ScheduledTask `
    -Action $updateAction `
    -Trigger @($updateTrigger, $updateDailyTrigger) `
    -Principal $systemPrincipal `
    -Settings $taskSettings
Register-ScheduledTask `
    -TaskName "MT5-WindowsUpdate-Policy" `
    -InputObject $updateTask `
    -Force | Out-Null

if (-not $SkipComposeDeploy) {
    & docker compose --project-directory $root --env-file $envFile -f $composeFile config --quiet
    if ($LASTEXITCODE -ne 0) {
        throw "Production Compose configuration validation failed."
    }
    & docker compose --project-directory $root --env-file $envFile -f $composeFile up --build -d
    if ($LASTEXITCODE -ne 0) {
        throw "Production Compose deployment failed with exit code $LASTEXITCODE."
    }
}

Start-ScheduledTask -TaskName "MT5-Host-State-Adapter"
Start-ScheduledTask -TaskName "MT5-Terminal-Autostart"
Start-ScheduledTask -TaskName "MT5-Docker-Host-Watchdog"

Write-Output "Production autostart tasks and Windows Update policy are installed."
