$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$EnvFile = Join-Path $Root ".env"
$ExampleFile = Join-Path $Root ".env.example"

if (-not (Test-Path $EnvFile)) {
    Copy-Item $ExampleFile $EnvFile
    Write-Host "Created .env from .env.example. Review its credentials before use."
}

docker compose --env-file $EnvFile -f (Join-Path $Root "docker-compose.dev.yml") up --build -d
if ($LASTEXITCODE -ne 0) {
    throw "docker compose failed with exit code $LASTEXITCODE"
}
