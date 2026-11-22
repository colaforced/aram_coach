[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Write-Host ""
Write-Host "========================================"
Write-Host "  LoL Anti-Spoof Launcher" -ForegroundColor Cyan
Write-Host "========================================"
Write-Host ""

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "Requesting admin..." -ForegroundColor Yellow
    Start-Process powershell -ArgumentList "-ExecutionPolicy Bypass -File `"$PSCommandPath`"" -Verb RunAs -Wait
    exit
}

Write-Host "[1/3] Setting power plan..." -ForegroundColor White
$firstGuid = (powercfg /list | Select-String "(\{[0-9a-f\-]+\})" | ForEach-Object { $_.Matches[0].Groups[1].Value } | Select-Object -First 1)
if ($firstGuid) { powercfg /setactive $firstGuid | Out-Null; Write-Host "  Done" -ForegroundColor Green }

Write-Host ""
Write-Host "[2/3] Configuring GPU spoof driver..." -ForegroundColor White
sc.exe stop lci_proxywddm 2>$null | Out-Null
sc.exe config lci_proxywddm start= demand 2>$null | Out-Null
$state = (sc.exe query lci_proxywddm | Select-String "STATE" | ForEach-Object { ($_ -split ":")[-1].Trim() }).Trim()
Write-Host "  Driver state: $state" -ForegroundColor Green
Write-Host "  Start type: DEMAND (will not load after reboot)" -ForegroundColor Green

Write-Host ""
Write-Host "[3/3] Launching LoL WeGame..." -ForegroundColor White
$apps = Get-ChildItem "G:\Games\WeGameApps" -Directory -ErrorAction SilentlyContinue
$lolDir = $null
foreach ($d in $apps) {
    if ($d.Name -match "classic|jingdian") { $lolDir = $d; break }
}
if (-not $lolDir) {
    foreach ($d in $apps) {
        $files = Get-ChildItem $d.FullName -Recurse -Filter "League of Legends.exe" -ErrorAction SilentlyContinue
        if ($files) { $lolDir = $d; break }
    }
}
if ($lolDir) {
    $launcher = Get-ChildItem $lolDir.FullName -Recurse -Filter "launcher.exe" -ErrorAction SilentlyContinue | Where-Object { $_.DirectoryName -match "WeGameLauncher" } | Select-Object -First 1
    if ($launcher) {
        Write-Host "  Found: $($launcher.FullName)" -ForegroundColor Green
        Start-Process $launcher.FullName
    } else {
        Start-Process $lolDir.FullName
    }
} else {
    Write-Host "  LoL dir not found, opening WeGame..." -ForegroundColor Yellow
    Start-Process "shell:AppsFolder\WeGameChina.WeGame_8wekyb3d8bbwe!App" -ErrorAction SilentlyContinue
}

Write-Host ""
Write-Host "========================================"
Write-Host "  LoL launcher opened, enjoy!" -ForegroundColor Cyan
Write-Host "========================================"
Write-Host ""