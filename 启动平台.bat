@echo off
setlocal EnableExtensions
chcp 65001 >nul 2>&1
cd /d "%~dp0"

set "PYTHON_EXE=C:\Users\98319\.workbuddy\binaries\python\envs\smartlabhub\Scripts\python.exe"
set "LAUNCHER=%~dp0platform\tools\launch_platform.py"

if not exist "%PYTHON_EXE%" (
    echo [ERROR] Python runtime not found:
    echo %PYTHON_EXE%
    echo.
    echo Install Python deps or update PYTHON_EXE in this file.
    pause
    exit /b 2
)

if not exist "%LAUNCHER%" (
    echo [ERROR] Launcher script not found:
    echo %LAUNCHER%
    pause
    exit /b 2
)

"%PYTHON_EXE%" "%LAUNCHER%" %*
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
    echo.
    echo [FAILED] Exit code %RC%. See logs in the "logs" folder.
    pause
)

exit /b %RC%