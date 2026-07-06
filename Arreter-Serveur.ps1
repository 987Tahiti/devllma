# ============================================================================
#  Arreter-Serveur.ps1 — Arrete le serveur DevLLMA (port 8080), avec ou sans fenetre.
#  Ne desactive PAS le demarrage automatique (il repartira au prochain logon).
#  Lancement : powershell -ExecutionPolicy Bypass -File C:\Devllma\Arreter-Serveur.ps1
# ============================================================================
$ErrorActionPreference = "SilentlyContinue"
$Port = 8080
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Start-Process powershell -Verb RunAs -ArgumentList @("-ExecutionPolicy","Bypass","-NoExit","-File","`"$PSCommandPath`"")
    exit
}
$conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($conn) {
    Stop-Process -Id ($conn | Select-Object -First 1).OwningProcess -Force
    Write-Host "Serveur DevLLMA (port $Port) arrete." -ForegroundColor Green
} else {
    Write-Host "Aucun serveur DevLLMA en cours sur le port $Port." -ForegroundColor Yellow
}
Write-Host "(Le demarrage auto reste actif : il repartira au prochain login. Pour le desactiver :" -ForegroundColor Cyan
Write-Host " Unregister-ScheduledTask -TaskName DevLLMA -Confirm:`$false )" -ForegroundColor Cyan
