[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$policyPath = "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU"
$settingsPath = "HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings"
New-Item -Path $policyPath -Force | Out-Null
New-Item -Path $settingsPath -Force | Out-Null

$today = (Get-Date).DayOfWeek
$isSaturday = $today -eq [System.DayOfWeek]::Saturday

# Only allow unattended update installation during the Saturday maintenance window.
$auOptions = if ($isSaturday) { 4 } else { 2 }
New-ItemProperty -Path $policyPath -Name "AUOptions" -Value $auOptions `
    -PropertyType DWord -Force | Out-Null
New-ItemProperty -Path $policyPath -Name "ScheduledInstallDay" -Value 7 `
    -PropertyType DWord -Force | Out-Null
New-ItemProperty -Path $policyPath -Name "ScheduledInstallTime" -Value 3 `
    -PropertyType DWord -Force | Out-Null
New-ItemProperty -Path $policyPath -Name "NoAutoRebootWithLoggedOnUsers" -Value 1 `
    -PropertyType DWord -Force | Out-Null

# Windows active hours are limited to 18 hours; AUOptions=2 prevents automatic
# installation/reboots Sunday-Friday outside that UI-level protection window.
New-ItemProperty -Path $settingsPath -Name "SetActiveHours" -Value 1 `
    -PropertyType DWord -Force | Out-Null
New-ItemProperty -Path $settingsPath -Name "ActiveHoursStart" -Value 0 `
    -PropertyType DWord -Force | Out-Null
New-ItemProperty -Path $settingsPath -Name "ActiveHoursEnd" -Value 18 `
    -PropertyType DWord -Force | Out-Null

Write-Output "Windows Update policy applied for $today (AUOptions=$auOptions)."
