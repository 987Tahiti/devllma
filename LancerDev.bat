@echo off
title DevLLMA — Demarrage...
color 0A

echo.
echo  ======================================
echo   DevLLMA - Environnement IA local
echo  ======================================
echo.

:: --- Ollama ---
echo [1/3] Verification Ollama...
curl -s http://localhost:11434/api/tags >nul 2>&1
if %errorlevel% NEQ 0 (
    echo  Ollama arrete - demarrage en cours...
    start "" "C:\Users\Admin\AppData\Local\Programs\Ollama\ollama.exe" serve
    timeout /t 4 /nobreak >nul
    echo  Ollama demarre.
) else (
    echo  Ollama deja actif.
)

:: --- VS Code ---
echo [2/3] Ouverture VS Code...
start "" "C:\Users\Admin\AppData\Local\Programs\Microsoft VS Code\Code.exe" "C:\Devllma"
timeout /t 2 /nobreak >nul

:: --- DevLLMA Brain ---
echo [3/3] Lancement DevLLMA Brain...
echo.
echo  ======================================
echo   DevLLMA pret ! Tape ta question.
echo   Commandes : ask code debug review
echo               test db sec ops arch
echo               agents history quit
echo  ======================================
echo.
cd /d C:\Devllma
"C:\Users\Admin\AppData\Local\Programs\Python\Python311\python.exe" start.py
pause