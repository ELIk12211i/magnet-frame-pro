@echo off
REM -------------------------------------------------------------
REM Quick dev seeder — creates 3 yearly test licenses.
REM -------------------------------------------------------------

setlocal enableextensions

cd /d "%~dp0"

python -m admin.create_license yearly --count 3 --customer-name "Test"

if errorlevel 1 (
    echo.
    echo FAILED — is Python + server requirements installed?
    echo   pip install -r requirements.txt
    pause
    exit /b 1
)

echo.
pause
endlocal
