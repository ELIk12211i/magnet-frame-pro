@echo off
REM -------------------------------------------------------------
REM Magnet Frame Pro — ADMIN Server — Windows launcher
REM Runs ONLY the admin dashboard (no public site, no /checkout).
REM
REM Port 8001 so it can run side-by-side with the public server
REM (run_server.bat, port 8000). Both share licenses.db.
REM -------------------------------------------------------------

setlocal enableextensions

cd /d "%~dp0"

REM Is uvicorn available? If not, install the requirements.
python -c "import uvicorn" >nul 2>&1
if errorlevel 1 (
    echo [setup] Installing requirements...
    python -m pip install -r "%~dp0requirements.txt"
    if errorlevel 1 (
        echo [setup] FAILED — please check pip / Python.
        pause
        exit /b 1
    )
)

echo.
echo [run] Starting ADMIN dashboard on http://127.0.0.1:8001
echo       Login:    http://127.0.0.1:8001/admin/login
echo       (Ctrl+C to stop)
echo.

python -m uvicorn app.admin_main:app --host 127.0.0.1 --port 8001

endlocal
