[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Write-Host ""
Write-Host "========================================"
Write-Host "  Delta Force Spoof Launcher" -ForegroundColor Magenta
Write-Host "========================================"
Write-Host ""

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "Requesting admin..." -ForegroundColor Yellow
    Start-Process powershell -ArgumentList "-ExecutionPolicy Bypass -File `"$PSCommandPath`"" -Verb RunAs -Wait
    exit
}

Write-Host "[1/2] Set spoof driver to boot-start..." -ForegroundColor White
sc.exe config lci_proxywddm start= boot | Out-Null
Write-Host "  Done" -ForegroundColor Green

Write-Host ""
Write-Host "[2/2] Loading spoof driver..." -ForegroundColor White
sc.exe start lci_proxywddm 2>$null | Out-Null
$state = (sc.exe query lci_proxywddm | Select-String "STATE" | ForEach-Object { ($_ -split ":")[-1].Trim() }).Trim()

Write-Host ""
if ($state -match "RUNNING") {
    Write-Host "Spoof active! Start Delta Force now." -ForegroundColor Green
} else {
    Write-Host "State: $state (WDDM driver may need reboot)" -ForegroundColor Yellow
}
Write-Host ""