# Gardien DevLLMA : verifie que l'interface repond reellement (pas juste que le
# process existe -- un process peut etre "vivant" mais bloque avant d'ouvrir le
# port, comme constate le 05/07/2026 : 9h de blocage silencieux sans redemarrage
# automatique car Task Scheduler ne considere pas ca comme un echec).
# Execute toutes les 5 min par la tache planifiee DevLLMAWatchdog (SYSTEM).

$log = "C:\Devllma\logs\watchdog.log"
$ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"

function Write-Log($msg) {
    "[$ts] $msg" | Out-File -FilePath $log -Append -Encoding utf8
}

try {
    $resp = Invoke-WebRequest -Uri "http://127.0.0.1:8080/" -UseBasicParsing -TimeoutSec 8
    if ($resp.StatusCode -eq 200) {
        # Silencieux si tout va bien (pas de bruit dans le log a chaque cycle sain) --
        # sauf log periodique leger pour prouver que le gardien tourne bien.
        if ((Get-Date).Minute % 30 -eq 0) { Write-Log "OK (verification periodique)" }
        exit 0
    }
    Write-Log "REPONSE ANORMALE : HTTP $($resp.StatusCode) -- redemarrage"
} catch {
    Write-Log "INJOIGNABLE ($($_.Exception.Message)) -- redemarrage"
}

# Ici : le site ne repond pas. Nettoyer tout process python.exe lie a webui.py
# AVANT de relancer la tache (un process fige qui tient encore le port empecherait
# le nouveau process de demarrer correctement).
Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue | ForEach-Object {
    if ($_.CommandLine -and $_.CommandLine -match "webui\.py") {
        Write-Log "Arret du process fige PID $($_.ProcessId)"
        taskkill /F /T /PID $_.ProcessId 2>&1 | Out-Null
    }
}

Stop-ScheduledTask -TaskName "DevLLMAWeb" -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2
Start-ScheduledTask -TaskName "DevLLMAWeb"
Write-Log "Tache DevLLMAWeb relancee"

Start-Sleep -Seconds 5
try {
    $verif = Invoke-WebRequest -Uri "http://127.0.0.1:8080/" -UseBasicParsing -TimeoutSec 8
    Write-Log "Verification post-relance : HTTP $($verif.StatusCode)"
} catch {
    Write-Log "ECHEC post-relance : $($_.Exception.Message) -- reessai au prochain cycle (5 min)"
}
