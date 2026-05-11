@echo off
REM -------------------------------------------------------------
REM Magnet Frame Pro — License Server — Windows launcher
REM Installs requirements on first run, then starts uvicorn.
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
echo [run] Starting license server on http://127.0.0.1:8000
echo       Admin UI: http://127.0.0.1:8000/admin/
echo       (Ctrl+C to stop)
echo.

python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

endlocal
