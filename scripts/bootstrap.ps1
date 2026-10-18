<#
.SYNOPSIS
  DLY AEOS bootstrap for Windows (master §07 order 0-1): verifies prerequisites, creates the
  repository virtual environment, installs pinned dependencies, builds the Control Center and runs
  the Readiness Doctor. It never installs system software silently: missing tools are reported with
  the official install command for you to run.
.EXAMPLE
  .\scripts\bootstrap.ps1
  .\scripts\bootstrap.ps1 -SkipFrontend
#>
param(
    [switch]$SkipFrontend,
    [switch]$NoDoctor
)
$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $root

function Need($name, $hint) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    if (-not $cmd) {
        Write-Host "  MISSING  $name" -ForegroundColor Red
        Write-Host "           install: $hint" -ForegroundColor Yellow
        return $null
    }
    Write-Host "  OK       $name -> $($cmd.Source)" -ForegroundColor Green
    return $cmd
}

Write-Host "DLY AEOS bootstrap (Windows)" -ForegroundColor Cyan
Write-Host "Step 0: prerequisites"
$missing = $false
if (-not (Need "git" "winget install --id Git.Git -e")) { $missing = $true }
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { $py = Get-Command py -ErrorAction SilentlyContinue }
if (-not $py) {
    Write-Host "  MISSING  python (>=3.11)" -ForegroundColor Red
    Write-Host "           install: winget install --id Python.Python.3.13 -e" -ForegroundColor Yellow
    $missing = $true
} else {
    $ver = & $py.Source -c "import sys;print('%d.%d' % sys.version_info[:2])"
    if ([version]$ver -lt [version]"3.11") {
        Write-Host "  TOO OLD  python $ver (need >=3.11)" -ForegroundColor Red; $missing = $true
    } else { Write-Host "  OK       python $ver" -ForegroundColor Green }
}
if (-not $SkipFrontend) {
    $node = Need "node" "winget install --id OpenJS.NodeJS.LTS -e"
    if (-not $node) { $missing = $true } else {
        $nv = (& node --version).TrimStart("v")
        if ([version]($nv.Split("-")[0]) -lt [version]"20.0") { Write-Host "  TOO OLD  node $nv (need >=20)" -ForegroundColor Red; $missing = $true }
    }
}
if ($missing) {
    Write-Host "Install the missing prerequisites above, open a new terminal, and re-run this script." -ForegroundColor Yellow
    exit 1
}

Write-Host "Step 1: Python virtual environment (.venv)"
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    & $py.Source -m venv .venv
}
& .\.venv\Scripts\python.exe -m pip install --upgrade pip --quiet
if (Test-Path "requirements.lock") {
    & .\.venv\Scripts\python.exe -m pip install --quiet -r requirements.lock
}
& .\.venv\Scripts\python.exe -m pip install --quiet -e ".[dev]"
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

if (-not $SkipFrontend) {
    Write-Host "Step 2: Control Center frontend (npm ci + build)"
    Push-Location frontend
    try {
        & npm.cmd ci --no-audit --no-fund
        if ($LASTEXITCODE -ne 0) { throw "npm ci failed" }
        & npm.cmd run build
        if ($LASTEXITCODE -ne 0) { throw "frontend build failed" }
    } finally { Pop-Location }
}

Write-Host "Step 3: local database"
& .\.venv\Scripts\aeos.exe db upgrade
& .\.venv\Scripts\aeos.exe seed

if (-not $NoDoctor) {
    Write-Host "Step 4: Readiness Doctor"
    & .\.venv\Scripts\aeos.exe doctor
}

Write-Host ""
Write-Host "Next steps:" -ForegroundColor Cyan
Write-Host "  .\aeos.ps1 admin create      # first SUPER_ADMIN (no default credentials)"
Write-Host "  .\aeos.ps1 serve             # http://127.0.0.1:8787"
