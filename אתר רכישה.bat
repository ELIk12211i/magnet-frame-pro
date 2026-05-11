@echo off
chcp 65001 >nul
title Magnet Frame PRO - Open Purchase Site
cd /d "%~dp0server"

REM Check if the public server (port 8000) is already running.
netstat -ano | findstr ":8000 " | findstr "LISTENING" >nul
if errorlevel 1 (
    echo [start] Public server not running. Launching on port 8000...
    start "Magnet Frame PRO - Public Server (8000)" cmd /k "python -m uvicorn app.main:app --host 127.0.0.1 --port 8000"
    REM Give uvicorn ~4 seconds to bind the port and load the app.
    timeout /t 4 /nobreak >nul
) else (
    echo [info] Public server already running on port 8000.
)

REM Open the purchase site in the default browser.
start "" "http://127.0.0.1:8000/site/"
exit
