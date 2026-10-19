<#
.SYNOPSIS
  DLY AEOS development mode on Windows: starts the control plane with `aeos serve --dev` on
  127.0.0.1:8787 (allows the Vite origin, enables API docs) and the Vite dev server on
  127.0.0.1:5173 (proxies /api to 8787). Ctrl+C stops both. Loopback only (master section 06).
.EXAMPLE
  .\scripts\dev.ps1
  .\scripts\dev.ps1 -Port 8790
#>
param(
    [int]$Port = 8787,
    [int]$VitePort = 5173
)
$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $root

function PortBusy([int]$p) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect("127.0.0.1", $p, $null, $null)
        $ok = $async.AsyncWaitHandle.WaitOne(300)
        if ($ok -and $client.Connected) { return $true }
        return $false
    } catch { return $false } finally { $client.Close() }
}

$aeos = Join-Path $root ".venv\Scripts\aeos.exe"
if (-not (Test-Path $aeos)) {
    Write-Host "Virtual environment not found. Run: .\scripts\bootstrap.ps1" -ForegroundColor Yellow
    exit 1
}
if (-not (Test-Path "frontend\node_modules")) {
    Write-Host "Frontend dependencies missing. Run: cd frontend; npm.cmd ci" -ForegroundColor Yellow
    exit 1
}
foreach ($p in @($Port, $VitePort)) {
    if (PortBusy $p) {
        Write-Host "Port $p on 127.0.0.1 is already in use. Stop the other process (Get-NetTCPConnection -LocalPort $p) or choose another port." -ForegroundColor Red
        exit 2
    }
}
if ($Port -ne 8787) {
    Write-Host "Note: the Vite proxy in frontend/vite.config.ts targets 127.0.0.1:8787; with -Port $Port use the backend URL directly or adjust the proxy." -ForegroundColor Yellow
}
if ($VitePort -ne 5173) {
    Write-Host "Note: the backend allow-lists the Vite origin on AEOS_DEV_FRONTEND_PORT=$VitePort." -ForegroundColor Yellow
}
$env:AEOS_DEV_FRONTEND_PORT = "$VitePort"
# Section 09: a hot-reloading dev server never holds the owner's real credentials or data. Use a
# separate data directory and keyring service unless the caller chose others explicitly.
if (-not $env:AEOS_DATA_DIR) { $env:AEOS_DATA_DIR = Join-Path $env:LOCALAPPDATA "DLY-AEOS-dev" }
if (-not $env:AEOS_KEYRING_SERVICE) { $env:AEOS_KEYRING_SERVICE = "dly-aeos-dev" }
Write-Host "Dev profile: data $($env:AEOS_DATA_DIR), keyring service $($env:AEOS_KEYRING_SERVICE) (create a dev admin with: .\aeos.ps1 admin create, same environment)" -ForegroundColor Yellow

Write-Host "Starting control plane (dev) on http://127.0.0.1:$Port ..." -ForegroundColor Cyan
$backend = Start-Process -FilePath $aeos -ArgumentList @("serve", "--dev", "--port", "$Port") -NoNewWindow -PassThru
try {
    $deadline = (Get-Date).AddSeconds(30)
    while (-not (PortBusy $Port)) {
        if ($backend.HasExited) { throw "backend exited early (exit code $($backend.ExitCode))" }
        if ((Get-Date) -gt $deadline) { throw "backend did not open port $Port within 30s" }
        Start-Sleep -Milliseconds 300
    }
    Write-Host "Starting Vite on http://127.0.0.1:$VitePort (Ctrl+C stops both) ..." -ForegroundColor Cyan
    Push-Location frontend
    try {
        & npm.cmd run dev -- --port $VitePort --strictPort
    } finally { Pop-Location }
} finally {
    if ($backend -and -not $backend.HasExited) {
        Write-Host "Stopping control plane (pid $($backend.Id)) ..."
        & taskkill.exe /PID $backend.Id /T /F | Out-Null
    }
}
