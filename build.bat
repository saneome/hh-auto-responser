@echo off
setlocal enabledelayedexpansion

cd /d "%~dp0"

set VENV=.venv
set PYTHON=%VENV%\Scripts\python.exe
set PI=%VENV%\Scripts\pyinstaller.exe

if not exist "%PYTHON%" (
    echo venv not found at %VENV% — run: python -m venv .venv ^&^& .venv\Scripts\activate ^&^& pip install -r requirements.txt
    exit /b 1
)

"%PYTHON%" -c "import PySide6" 2>nul
if errorlevel 1 (
    echo PySide6 not installed — run: pip install -r requirements.txt
    exit /b 1
)

"%PYTHON%" -c "import PyInstaller" 2>nul
if errorlevel 1 (
    echo pyinstaller not found — installing...
    "%VENV%\Scripts\pip.exe" install pyinstaller
)

echo === building hh-auto-response ===

"%PI%" --clean --noconfirm hh-auto-response.spec

if errorlevel 1 (
    echo.
    echo PyInstaller build FAILED
    exit /b 1
)

echo.
echo === PyInstaller build done ===
echo output: dist\hh-auto-response\
echo.

where iscc >nul 2>nul
if %errorlevel%==0 (
    echo === building installer ===
    iscc setup.iss
    if errorlevel 1 (
        echo InnoSetup build failed — install from https://jrsoftware.org/isdl.php
    ) else (
        echo.
        echo === installer done ===
        echo output: installer-output\hh-auto-response-setup-1.0.0.exe
    )
) else (
    echo.
    echo InnoSetup not found. Install from https://jrsoftware.org/isdl.php
    echo Then run: iscc setup.iss
)

endlocal
