[CmdletBinding()]
param(
    [string]$ProjectDirectory = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [string]$PythonPath
)

$ErrorActionPreference = "Stop"
$envFile = Join-Path $ProjectDirectory ".env"
if (-not (Test-Path -LiteralPath $envFile)) {
    throw "Missing deployment environment file: $envFile"
}

foreach ($line in Get-Content -LiteralPath $envFile) {
    if ($line -match "^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$") {
        $name = $Matches[1]
        $value = $Matches[2].Trim()
        if (($value.StartsWith('"') -and $value.EndsWith('"')) -or
            ($value.StartsWith("'") -and $value.EndsWith("'"))) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        [Environment]::SetEnvironmentVariable($name, $value, "Process")
    }
}

if (-not $env:MT5_ADAPTER_TOKEN -or $env:MT5_ADAPTER_TOKEN.Length -lt 32) {
    throw "MT5_ADAPTER_TOKEN in .env must contain at least 32 characters."
}
if (-not $env:ORDER_SIGNING_SECRET -or $env:ORDER_SIGNING_SECRET.Length -lt 32) {
    $env:ORDER_SIGNING_SECRET = $env:SECRET_KEY
}
if (-not $env:ORDER_SIGNING_SECRET -or $env:ORDER_SIGNING_SECRET.Length -lt 32) {
    throw "Set ORDER_SIGNING_SECRET (or SECRET_KEY) to 32+ characters in .env."
}
if (-not $PythonPath) {
    $PythonPath = (& py -3 -c "import sys; print(sys.executable)" | Select-Object -First 1)
}
$PythonPath = (Resolve-Path -LiteralPath $PythonPath).Path
$adapterPort = 8765
if ($env:MT5_ADAPTER_PORT) {
    $adapterPort = [int]$env:MT5_ADAPTER_PORT
}
if ($adapterPort -lt 1 -or $adapterPort -gt 65535) {
    throw "MT5_ADAPTER_PORT must be between 1 and 65535."
}

Set-Location -LiteralPath $ProjectDirectory
& $PythonPath -m uvicorn scripts.mt5_host_adapter:app --host 0.0.0.0 --port $adapterPort
if ($LASTEXITCODE -ne 0) {
    throw "MT5 host adapter exited with code $LASTEXITCODE."
}
