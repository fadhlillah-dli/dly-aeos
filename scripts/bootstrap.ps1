<#
.SYNOPSIS
  DLY AEOS bootstrap for Windows (master section 07, order 0-1): verifies prerequisites, creates the
  repository virtual environment, installs pinned dependencies (requirements.lock), backs up any
  existing data, builds the Control Center, migrates the local database, runs the Readiness Doctor
  and prints the status of the optional components in bootstrap order.
  It never installs system software silently: missing tools are reported with the official install
  command for you to run.
.EXAMPLE
  .\scripts\bootstrap.ps1
  .\scripts\bootstrap.ps1 -SkipFrontend
  .\scripts\bootstrap.ps1 -NoDoctor -SkipBackup
#>
param(
    [switch]$SkipFrontend,
    [switch]$NoDoctor,
    [switch]$SkipBackup
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
    # Pinned, universal (Windows/macOS/Linux) lock; the project itself is installed without
    # re-resolving dependencies so the locked versions stay in place.
    & .\.venv\Scripts\python.exe -m pip install --quiet -r requirements.lock
    if ($LASTEXITCODE -ne 0) { throw "pip install -r requirements.lock failed" }
    & .\.venv\Scripts\python.exe -m pip install --quiet --no-deps -e .
    if ($LASTEXITCODE -ne 0) { throw "pip install -e . failed" }
    & .\.venv\Scripts\python.exe -m pip check
    if ($LASTEXITCODE -ne 0) { Write-Host "  WARNING  pip check reports a dependency problem; regenerate requirements.lock (docs/OPERATIONS.md)" -ForegroundColor Yellow }
} else {
    & .\.venv\Scripts\python.exe -m pip install --quiet -e ".[dev]"
    if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
}

if (-not $SkipBackup) {
    Write-Host "Step 0b: back up existing data before any migration (master section 07 step 0)"
    & .\.venv\Scripts\aeos.exe ops backup create --label pre-bootstrap --if-exists
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Backup of the existing data failed; nothing was migrated. Investigate, or re-run with -SkipBackup if you accept the risk." -ForegroundColor Red
        exit 1
    }
}

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
# Windows PowerShell 5.1 does not stop on a native non-zero exit even with ErrorActionPreference=Stop:
# every required step checks $LASTEXITCODE explicitly so a failed migration/seed never reports success.
& .\.venv\Scripts\aeos.exe db upgrade
if ($LASTEXITCODE -ne 0) { Write-Host "aeos db upgrade failed (exit $LASTEXITCODE); bootstrap stopped." -ForegroundColor Red; exit 1 }
& .\.venv\Scripts\aeos.exe seed
if ($LASTEXITCODE -ne 0) { Write-Host "aeos seed failed (exit $LASTEXITCODE); bootstrap stopped." -ForegroundColor Red; exit 1 }
# Observe what is actually installed so the first dashboard view is not DESIGN-only (read-only probes).
& .\.venv\Scripts\aeos.exe inventory reconcile
if ($LASTEXITCODE -ne 0) { Write-Host "WARNING: inventory reconcile failed (exit $LASTEXITCODE); the dashboard shows DESIGN data until it runs." -ForegroundColor Yellow }

if (-not $NoDoctor) {
    Write-Host "Step 4: Readiness Doctor"
    & .\.venv\Scripts\aeos.exe doctor
    if ($LASTEXITCODE -ne 0) { Write-Host "WARNING: the Readiness Doctor reported failures (exit $LASTEXITCODE); see the remedies above." -ForegroundColor Yellow }
}

Write-Host ""
Write-Host "Step 5: optional components in bootstrap order (status only, nothing is installed)" -ForegroundColor Cyan
& .\.venv\Scripts\python.exe scripts\component_status.py

Write-Host ""
Write-Host "Next steps:" -ForegroundColor Cyan
Write-Host "  .\aeos.ps1 admin create      # first SUPER_ADMIN (no default credentials)"
Write-Host "  .\aeos.ps1 serve             # http://127.0.0.1:8787"
Write-Host "  .\scripts\dev.ps1            # development: backend --dev + Vite on http://127.0.0.1:5173"
Write-Host "  See docs\SETUP_WINDOWS.md for Claude Code, OpenBao, OpenRouter, Google and GitHub setup."
