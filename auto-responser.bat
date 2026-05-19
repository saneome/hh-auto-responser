@echo off
setlocal EnableDelayedExpansion

cd /d "%~dp0"

:: ── Check venv ──
if not exist ".venv\Scripts\activate.bat" (
    echo ERROR: venv not found. Run: python -m venv .venv ^&^& pip install -r requirements.txt
    pause
    exit /b 1
)
call .venv\Scripts\activate.bat

:: ── Cleanup stale Chrome locks ──
echo [cleanup] Removing stale locks...
taskkill /F /FI "IMAGENAME eq chrome.exe" 2>nul
timeout /t 2 /nobreak >nul

if exist "user-data-search\SingletonLock"  del /f "user-data-search\SingletonLock" 2>nul
if exist "user-data-search\SingletonCookie" del /f "user-data-search\SingletonCookie" 2>nul
if exist "user-data-search\SingletonSocket" del /f "user-data-search\SingletonSocket" 2>nul
if exist "user-data-responder\SingletonLock"  del /f "user-data-responder\SingletonLock" 2>nul
if exist "user-data-responder\SingletonCookie" del /f "user-data-responder\SingletonCookie" 2>nul
if exist "user-data-responder\SingletonSocket" del /f "user-data-responder\SingletonSocket" 2>nul
echo [cleanup] Done.

:: ── Ensure logs dir ──
if not exist logs mkdir logs

:: ── Launch GUI with live output + log ──
echo [%time%] Starting GUI...
powershell -NoProfile -Command "python main.py --gui | Tee-Object -FilePath logs\gui.log -Append"

set EXIT_CODE=%ERRORLEVEL%
echo [%time%] GUI finished (code %EXIT_CODE%)
pause
endlocal
