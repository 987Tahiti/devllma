# ============================================================================
#  Reboot-Prod.ps1 - Redemarre le serveur DevLLMA (port 8080)
#  Arrete le process en cours, relance webui.py (avec warmup), verifie la reprise.
#  Lancement : powershell -ExecutionPolicy Bypass -File C:\Devllma\Reboot-Prod.ps1
# ============================================================================

$ErrorActionPreference = "Stop"
$Python = "C:\Users\Admin\AppData\Local\Programs\Python\Python311\python.exe"
$Script = "C:\Devllma\webui.py"
$Ollama = "C:\Users\Admin\AppData\Local\Programs\Ollama\ollama.exe"
$Port   = 8080

# Elevation UAC UNIQUEMENT si necessaire : DEMARRER le serveur ne demande pas les droits
# admin ; seul ARRETER une prod deja lancee en admin en a besoin (sinon "Acces refuse").
# Donc on n'eleve QUE s'il y a un process a tuer sur le port 8080 et qu'on n'est pas admin.
# (Avant, l'elevation etait systematique -> si l'UAC etait refuse, rien ne demarrait.)
$needKill = [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
$isAdmin  = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if ($needKill -and -not $isAdmin) {
    Write-Host "Une prod tourne deja -> elevation pour l'arreter (accepte l'invite UAC)..." -ForegroundColor Yellow
    Start-Process powershell -Verb RunAs -ArgumentList @("-ExecutionPolicy","Bypass","-NoExit","-File","`"$PSCommandPath`"")
    exit
}

Write-Host "=== Redemarrage DevLLMA ===" -ForegroundColor Cyan

# 1) Verifier Ollama (le modele en depend) ; le demarrer sinon
try {
    Invoke-WebRequest "http://localhost:11434/api/tags" -TimeoutSec 4 -UseBasicParsing | Out-Null
    Write-Host "[1/4] Ollama : OK" -ForegroundColor Green
} catch {
    Write-Host "[1/4] Ollama injoignable, demarrage..." -ForegroundColor Yellow
    Start-Process $Ollama -ArgumentList "serve" -WindowStyle Hidden
    Start-Sleep -Seconds 4
}

# 2) Arreter le process qui ecoute sur le port 8080 (ancienne prod)
$conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($conn) {
    $procId = ($conn | Select-Object -First 1).OwningProcess
    Write-Host "[2/4] Arret de la prod (PID $procId)..." -ForegroundColor Yellow
    Stop-Process -Id $procId -Force
    for ($i = 0; $i -lt 10; $i++) {
        Start-Sleep -Seconds 1
        if (-not (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)) { break }
    }
    Write-Host "      port $Port libere." -ForegroundColor Green
} else {
    Write-Host "[2/4] Aucune prod sur le port $Port." -ForegroundColor Green
}

# 3) Relancer webui.py dans sa propre fenetre (reste ouvert = serveur vivant)
Write-Host "[3/4] Lancement de webui.py (warmup en cours, patiente 1-2 min)..." -ForegroundColor Yellow
Start-Process $Python -ArgumentList $Script -WorkingDirectory "C:\Devllma"

# 4) Attendre que le serveur reponde (max 90 s)
$ok = $false
for ($i = 0; $i -lt 45; $i++) {
    Start-Sleep -Seconds 2
    try {
        $r = Invoke-WebRequest "http://localhost:$Port/" -TimeoutSec 3 -UseBasicParsing
        if ($r.StatusCode -eq 200) { $ok = $true; break }
    } catch { }
}
if ($ok) {
    Write-Host "[4/4] DevLLMA repond sur http://localhost:$Port/ (warmup du modele en arriere-plan)" -ForegroundColor Green
    Start-Process "http://localhost:$Port/"
} else {
    Write-Host "[4/4] Pas de reponse apres 90 s. Regarde la fenetre webui.py pour les erreurs." -ForegroundColor Red
}
