# ============================================================================
#  Installer-Demarrage-Auto.ps1
#  Fait tourner DevLLMA en ARRIERE-PLAN (sans fenetre) et le lance AUTOMATIQUEMENT
#  a chaque ouverture de session Windows. A executer UNE SEULE FOIS.
#  Lancement : powershell -ExecutionPolicy Bypass -File C:\Devllma\Installer-Demarrage-Auto.ps1
# ============================================================================
$ErrorActionPreference = "Stop"
$PYW    = "C:\Users\Admin\AppData\Local\Programs\Python\Python311\pythonw.exe"  # python SANS console
$Script = "C:\Devllma\webui.py"
$Port   = 8080
$Task   = "DevLLMA"

# Elevation UAC (necessaire pour la tache planifiee admin + arret de l'ancien serveur)
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "Elevation (accepte l'invite UAC)..." -ForegroundColor Yellow
    Start-Process powershell -Verb RunAs -ArgumentList @("-ExecutionPolicy","Bypass","-NoExit","-File","`"$PSCommandPath`"")
    exit
}

Write-Host "=== Installation du demarrage automatique DevLLMA ===" -ForegroundColor Cyan

# 1) Arreter un serveur deja lance sur 8080 (fenetre visible ou autre)
$conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($conn) {
    Stop-Process -Id ($conn | Select-Object -First 1).OwningProcess -Force
    Start-Sleep -Seconds 2
    Write-Host "[1/3] Ancien serveur arrete." -ForegroundColor Green
} else {
    Write-Host "[1/3] Aucun serveur a arreter." -ForegroundColor Green
}

# 2) Creer/mettre a jour la tache planifiee : au logon, sans fenetre (pythonw), en admin
$action    = New-ScheduledTaskAction -Execute $PYW -Argument "`"$Script`"" -WorkingDirectory "C:\Devllma"
$trigger   = New-ScheduledTaskTrigger -AtLogOn
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName $Task -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Write-Host "[2/3] Tache '$Task' creee (demarrage au logon, sans fenetre)." -ForegroundColor Green

# 3) Demarrer maintenant en arriere-plan (sans fenetre)
Start-Process $PYW -ArgumentList "`"$Script`"" -WorkingDirectory "C:\Devllma" -WindowStyle Hidden
Write-Host "[3/3] Serveur demarre en arriere-plan." -ForegroundColor Green

# Attendre qu'il reponde
$ok = $false
for ($i=0; $i -lt 45; $i++) {
    Start-Sleep -Seconds 2
    try { if ((Invoke-WebRequest "http://localhost:$Port/" -TimeoutSec 3 -UseBasicParsing).StatusCode -eq 200) { $ok=$true; break } } catch {}
}
if ($ok) {
    Write-Host "`nDevLLMA tourne sur http://localhost:$Port/ SANS fenetre." -ForegroundColor Green
    Write-Host "Il redemarrera tout seul a chaque ouverture de Windows." -ForegroundColor Green
    Write-Host "Tu peux fermer la fenetre visible restante du serveur precedent." -ForegroundColor Yellow
    Start-Process "http://localhost:$Port/"
} else {
    Write-Host "`nLe serveur ne repond pas encore (warmup du modele ?). Patiente 1-2 min." -ForegroundColor Yellow
}
Write-Host "`nPour l'ARRETER : Arreter-Serveur.ps1" -ForegroundColor Cyan
