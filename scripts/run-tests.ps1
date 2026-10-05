[CmdletBinding()]
param(
    [switch]$Coverage,
    [switch]$Lint,
    [switch]$All
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$venvDir = Join-Path $root ".venv"
$python = Join-Path (Join-Path $venvDir "Scripts") "python.exe"

function New-Venv {
    if (-not (Test-Path $python)) {
        Write-Host "Creating virtual environment..."
        & py -3 -m venv $venvDir
    }
}

function Install-Dependencies {
    Write-Host "Installing dependencies..."
    & $python -m pip install --upgrade pip
    & $python -m pip install -r (Join-Path $root "requirements.txt")
    & $python -m pip install ruff coverage
}

function Invoke-Lint {
    Write-Host "Running ruff lint..."
    & $python -m ruff check `
        (Join-Path $root "services") `
        (Join-Path $root "schemas") `
        (Join-Path $root "event_bus") `
        (Join-Path $root "utils") `
        (Join-Path $root "database")
}

function Invoke-Tests {
    param([bool]$withCoverage)
    if ($withCoverage) {
        Write-Host "Running tests with coverage..."
        $coverageDir = Join-Path $root "htmlcov"
        if (Test-Path $coverageDir) { Remove-Item $coverageDir -Recurse -Force }
        & $python -m coverage run `
            --source=(Join-Path $root "services"),(Join-Path $root "schemas"),(Join-Path $root "event_bus"),(Join-Path $root "utils"),(Join-Path $root "database") `
            -m unittest discover -s (Join-Path $root "tests") -v
        if ($LASTEXITCODE -ne 0) { throw "Unit tests failed" }
        & $python -m coverage report --fail-under=80
    } else {
        Write-Host "Running tests..."
        & $python -m unittest discover -s (Join-Path $root "tests") -v
        if ($LASTEXITCODE -ne 0) { throw "Unit tests failed" }
    }
}

New-Venv
Install-Dependencies

if ($All -or $Lint) {
    Invoke-Lint
}

Invoke-Tests -withCoverage:$Coverage

Write-Host "All checks passed."
