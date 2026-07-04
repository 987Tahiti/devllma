@echo off
title DevLLMA Web UI
color 0A
echo.
echo  DevLLMA - Interface Web
echo  Ouverture sur http://localhost:8080
echo  (Ferme cette fenetre pour arreter)
echo.

:: Verifier Ollama
curl -s http://localhost:11434/api/tags >nul 2>&1
if %errorlevel% NEQ 0 (
    echo Demarrage Ollama...
    start "" "C:\Users\Admin\AppData\Local\Programs\Ollama\ollama.exe" serve
    timeout /t 4 /nobreak >nul
)

:: Lancer le serveur web en arriere plan et ouvrir le navigateur
timeout /t 2 /nobreak >nul
start "" "http://localhost:8080"

:: Lancer le serveur
"C:\Users\Admin\AppData\Local\Programs\Python\Python311\python.exe" C:\Devllma\webui.py
pause