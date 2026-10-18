# DLY AEOS launcher for Windows PowerShell. Usage: .\aeos.ps1 <command> [args]
# Runs the `aeos` CLI from the repository virtual environment created by scripts\bootstrap.ps1.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$exe = Join-Path $root ".venv\Scripts\aeos.exe"
if (-not (Test-Path $exe)) {
    Write-Host "Virtual environment not found. Run: .\scripts\bootstrap.ps1" -ForegroundColor Yellow
    exit 1
}
& $exe @args
exit $LASTEXITCODE
