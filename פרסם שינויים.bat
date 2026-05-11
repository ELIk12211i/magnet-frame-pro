@echo off
chcp 65001 >nul
title Magnet Frame PRO - Publish Changes to Live Site
cd /d "%~dp0"

echo ============================================================
echo  Magnet Frame PRO - Publish Changes
echo ============================================================
echo.

REM Step 1: sync site/ -> server/site/ (Railway reads from server/site/)
echo [1/4] Syncing website files...
if exist "site" (
    xcopy /E /Y /I /Q "site\index.html"  "server\site\" >nul 2>&1
    xcopy /E /Y /I /Q "site\logo.png"    "server\site\" >nul 2>&1
    xcopy /E /Y /I /Q "site\screenshot.png" "server\site\" >nul 2>&1
)
echo       Done.
echo.

REM Step 2: stage everything
echo [2/4] Staging changes...
git add -A >nul 2>&1
echo       Done.
echo.

REM Step 3: commit with timestamp
echo [3/4] Committing changes...
for /f "tokens=2 delims==" %%I in ('wmic os get localdatetime /value ^| find "="') do set datetime=%%I
set "stamp=%datetime:~0,4%-%datetime:~4,2%-%datetime:~6,2% %datetime:~8,2%:%datetime:~10,2%"
git commit -m "Update %stamp%" 2>nul
if errorlevel 1 (
    echo       No changes to commit. Skipping push.
    echo.
    echo ============================================================
    echo  Nothing changed - your live site is already up to date.
    echo ============================================================
    pause
    exit /b 0
)
echo       Done.
echo.

REM Step 4: push to GitHub - Railway auto-deploys
echo [4/4] Publishing to live site...
git push
if errorlevel 1 (
    echo.
    echo ============================================================
    echo  ERROR - Push failed. Check the messages above.
    echo ============================================================
    pause
    exit /b 1
)
echo.
echo ============================================================
echo  Success! Changes will be live in 1-2 minutes.
echo  Check: https://www.magnetframepro.co.il
echo  ^(or temporary URL until domain is connected^)
echo ============================================================
echo.
pause
