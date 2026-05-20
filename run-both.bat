@echo off
setlocal EnableDelayedExpansion

if "%~1"==":search_loop" goto :search_loop

cd /d "%~dp0"

set PYTHON=%~dp0.venv\Scripts\python.exe

if not exist logs mkdir logs

echo Убиваю старые Chrome-процессы...
taskkill /F /FI "IMAGENAME eq chrome.exe" /FI "WINDOWTITLE eq *" 2>nul

timeout /t 2 /nobreak >nul

if exist user-data-search\SingletonLock  del /f user-data-search\SingletonLock 2>nul
if exist user-data-search\SingletonSocket del /f user-data-search\SingletonSocket 2>nul
if exist user-data-responder\SingletonLock  del /f user-data-responder\SingletonLock 2>nul
if exist user-data-responder\SingletonSocket del /f user-data-responder\SingletonSocket 2>nul
echo Lock-файлы очищены.

:: Responder Agent (background)
start "responder" /B cmd /c "%PYTHON% main.py --check-negotiations --auto-reply --user-data-dir user-data-responder --loop -v >> logs\responder.log 2>&1"
echo Responder agent started.

:: Search Agent (background loop)
start "search" /B "%~f0" :search_loop

goto :wait

:search_loop
:loop_search
cd /d "%~dp0"
set PYTHON=%~dp0.venv\Scripts\python.exe
"%PYTHON%" main.py --user-data-dir user-data-search --no-post-search-responder -v >> logs\search.log 2>&1
echo [%date% %time%] Search batch finished, sleeping 3600s >> logs\search.log
timeout /t 3600 /nobreak >nul
goto :loop_search

:wait
echo Logs: logs\search.log  logs\responder.log
echo.
echo Нажмите Ctrl+C или Ctrl+Break чтобы остановить.
:wait_forever
timeout /t 3600 /nobreak >nul
goto :wait_forever

:end
endlocal
