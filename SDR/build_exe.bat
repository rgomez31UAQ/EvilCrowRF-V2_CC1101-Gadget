@echo off
REM EvilCrow SDR Builder — Quick launcher
REM Works from any directory; auto-detects Python and venv.

setlocal enabledelayedexpansion

REM Get the directory of this script
set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

REM Try venv first, then system Python
if exist ".venv\Scripts\python.exe" (
    echo [*] Using local .venv Python
    set "PYTHON=.venv\Scripts\python.exe"
) else if exist "%SCRIPT_DIR%..\.venv\Scripts\python.exe" (
    echo [*] Using project .venv Python
    set "PYTHON=%SCRIPT_DIR%..\.venv\Scripts\python.exe"
) else (
    where python >nul 2>&1
    if errorlevel 1 (
        echo ERROR: Python not found in PATH or local .venv
        echo Please install Python 3.8+ or create a venv.
        pause
        exit /b 1
    )
    set "PYTHON=python"
)

echo [*] Starting EvilCrow SDR Builder...
"%PYTHON%" build_exe.py %*

if errorlevel 1 (
    echo [!] Build tool exited with an error.
    pause
    exit /b 1
)
