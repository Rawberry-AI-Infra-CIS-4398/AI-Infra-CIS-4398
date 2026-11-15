<#
.SYNOPSIS
Start the RAWBerry API locally, resolving paths relative to this script.
.EXAMPLE
.\run.ps1 -InstallDependencies
.EXAMPLE
& 'C:\path\to\AI-Infra-CIS-4398\run.ps1'
#>
[CmdletBinding()]
param(
    [switch]$InstallDependencies,
    [switch]$NoReload,
    [ValidateRange(1, 65535)]
    [int]$Port = 8000,
    [string]$BindAddress = '127.0.0.1'
)

$ErrorActionPreference = 'Stop'
$apiDirectory = Join-Path $PSScriptRoot 'rawberry-api'
$pythonExecutable = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExecutable)) {
    $pythonExecutable = Join-Path $PSScriptRoot '.venv/bin/python'
}
if (-not (Test-Path -LiteralPath $pythonExecutable)) {
    $pythonExecutable = (Get-Command python -ErrorAction Stop).Source
}

Push-Location -LiteralPath $apiDirectory
try {
    if ($InstallDependencies) {
        & $pythonExecutable -m pip install -r requirements.txt
        if ($LASTEXITCODE -ne 0) {
            throw 'Dependency installation failed.'
        }
    }

    # Existing environment variables are inherited; .env files are not loaded.
    Write-Host "Starting RAWBerry with $pythonExecutable"
    Write-Host "Docs: http://${BindAddress}:$Port/docs"
    Write-Host "Database check: http://${BindAddress}:$Port/health/db"
    Write-Host 'Press Ctrl+C to stop.'
    $uvicornArguments = @('-m', 'uvicorn', 'app.main:app', '--host', $BindAddress, '--port', "$Port")
    if (-not $NoReload) {
        $uvicornArguments += '--reload'
    }
    & $pythonExecutable @uvicornArguments
    if ($LASTEXITCODE -ne 0) {
        throw 'API exited with an error. Check the output above; use -InstallDependencies if packages are missing.'
    }
}
finally {
    Pop-Location
}
