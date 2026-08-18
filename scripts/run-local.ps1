[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$runtimeDir = if ($env:XHS_RUNTIME_DIR) {
    [System.IO.Path]::GetFullPath($env:XHS_RUNTIME_DIR)
} else {
    "D:\AI_WORKSPACE_RUNTIME\xhs-intelligence-workbench"
}
$logDir = Join-Path $runtimeDir "logs"
$stateDir = Join-Path $runtimeDir "run"
New-Item -ItemType Directory -Force -Path $logDir, $stateDir | Out-Null

$python = (Get-Command python -ErrorAction Stop).Source
$node = (Get-Command node -ErrorAction Stop).Source
$backendOut = Join-Path $logDir "backend.stdout.log"
$backendErr = Join-Path $logDir "backend.stderr.log"
$frontendOut = Join-Path $logDir "frontend.stdout.log"
$frontendErr = Join-Path $logDir "frontend.stderr.log"
$vite = Join-Path $repoRoot "frontend\node_modules\vite\bin\vite.js"
if (-not (Test-Path -LiteralPath $vite -PathType Leaf)) {
    throw "Frontend dependencies are missing; run npm install in frontend first."
}
$occupiedPorts = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
    Where-Object { $_.LocalPort -in @(8000, 5173) }
if ($occupiedPorts) {
    throw "Ports 8000/5173 must be free; stop the existing listeners before starting."
}

$backend = $null
$frontend = $null
try {
    $backend = Start-Process -FilePath $python -ArgumentList @(
        "-m", "uvicorn", "backend.app.main:app", "--host", "127.0.0.1", "--port", "8000"
    ) -WorkingDirectory $repoRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $backendOut -RedirectStandardError $backendErr
    $frontend = Start-Process -FilePath $node -ArgumentList @(
        $vite, "--host", "127.0.0.1", "--port", "5173"
    ) -WorkingDirectory (Join-Path $repoRoot "frontend") -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $frontendOut -RedirectStandardError $frontendErr

    $ready = $false
    for ($attempt = 0; $attempt -lt 80; $attempt++) {
        $backend.Refresh()
        $frontend.Refresh()
        if ($backend.HasExited -or $frontend.HasExited) { break }
        try {
            $health = Invoke-WebRequest -UseBasicParsing `
                -Uri "http://127.0.0.1:8000/api/v1/health" -TimeoutSec 1
            $dashboard = Invoke-WebRequest -UseBasicParsing `
                -Uri "http://127.0.0.1:5173" -TimeoutSec 1
            if ($health.StatusCode -eq 200 -and $dashboard.StatusCode -eq 200) {
                $ready = $true
                break
            }
        } catch {
            Start-Sleep -Milliseconds 250
        }
    }
    if (-not $ready) {
        throw "Workbench did not become ready; inspect $logDir."
    }
    Start-Sleep -Milliseconds 500
    $backend.Refresh()
    $frontend.Refresh()
    if ($backend.HasExited -or $frontend.HasExited) {
        throw "A workbench process exited during readiness confirmation; inspect $logDir."
    }
} catch {
    foreach ($process in @($backend, $frontend)) {
        if ($null -ne $process) {
            $process.Refresh()
            if (-not $process.HasExited) { Stop-Process -Id $process.Id -Force }
        }
    }
    throw
}

@{
    backend_pid = $backend.Id
    frontend_pid = $frontend.Id
    started_at = (Get-Date).ToString("o")
} | ConvertTo-Json | Set-Content -Encoding utf8 (Join-Path $stateDir "local-processes.json")

Write-Host "Workbench processes started."
Write-Host "Dashboard: http://127.0.0.1:5173"
Write-Host "Health:    http://127.0.0.1:8000/api/v1/health"
Write-Host "Logs:      $logDir"
