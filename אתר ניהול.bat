@echo off
chcp 65001 >nul
title Magnet Frame PRO - Open Admin Panel
cd /d "%~dp0server"

REM Check if the admin server (port 8001) is already running.
netstat -ano | findstr ":8001 " | findstr "LISTENING" >nul
if errorlevel 1 (
    echo [start] Admin server not running. Launching on port 8001...
    start "Magnet Frame PRO - Admin Server (8001)" cmd /k "python -m uvicorn app.admin_main:app --host 127.0.0.1 --port 8001"
    REM Give uvicorn ~4 seconds to bind the port and load the app.
    timeout /t 4 /nobreak >nul
) else (
    echo [info] Admin server already running on port 8001.
)

REM Open the admin login page in the default browser.
start "" "http://127.0.0.1:8001/admin/login"
exit
