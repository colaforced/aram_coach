[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Write-Host ""
Write-Host "========================================"
Write-Host "  ARAM Coach - 大乱斗实时指导" -ForegroundColor Cyan
Write-Host "========================================"
Write-Host ""

$ErrorActionPreference = "Stop"
$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectDir

$port = 8765
try {
    $cfg = Get-Content (Join-Path $projectDir "config.json") -Raw | ConvertFrom-Json
    $port = $cfg.web_port
} catch {}

Write-Host "[1/4] Checking Python..." -ForegroundColor White
$pyCmd = $null
foreach ($c in @("python", "python3", "py")) {
    try {
        $v = & $c --version 2>&1
        if ($LASTEXITCODE -eq 0) { $pyCmd = $c; break }
    } catch {}
}
if (-not $pyCmd) {
    Write-Host "  Python not found!" -ForegroundColor Red
    Write-Host "  Install from https://www.python.org/downloads/" -ForegroundColor Yellow
    pause
    exit 1
}
Write-Host "  Found: $pyCmd" -ForegroundColor Green

Write-Host ""
Write-Host "[2/4] Checking dependencies..." -ForegroundColor White
$needed = $false
try {
    & $pyCmd -c "import fastapi, uvicorn, httpx" 2>$null
    if ($LASTEXITCODE -ne 0) { $needed = $true }
} catch { $needed = $true }

if ($needed) {
    Write-Host "  Installing requirements..." -ForegroundColor Yellow
    & $pyCmd -m pip install -r (Join-Path $projectDir "requirements.txt")
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  Failed to install deps" -ForegroundColor Red
        pause
        exit 1
    }
} else {
    Write-Host "  OK" -ForegroundColor Green
}

Write-Host ""
Write-Host "[3/4] Starting ARAM Coach..." -ForegroundColor White
Write-Host "  Web UI: http://127.0.0.1:$port/" -ForegroundColor Cyan
Write-Host ""

$proc = Start-Process -FilePath $pyCmd -ArgumentList "run.py" -WorkingDirectory $projectDir -NoNewWindow -PassThru
Start-Sleep -Seconds 2

Write-Host "[4/4] Opening browser..." -ForegroundColor White
Start-Process "http://127.0.0.1:$port/"

Write-Host ""
Write-Host "========================================"
Write-Host "  ARAM Coach is running!" -ForegroundColor Green
Write-Host "  Keep this window open" -ForegroundColor Yellow
Write-Host "  Press Ctrl+C to stop" -ForegroundColor Yellow
Write-Host "========================================"
Write-Host ""

try {
    while ($true) { Start-Sleep -Seconds 1 }
} finally {
    Write-Host "Stopping ARAM Coach..." -ForegroundColor Yellow
    if ($proc -and -not $proc.HasExited) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue }
}
