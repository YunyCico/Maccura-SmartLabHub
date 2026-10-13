@echo off
setlocal EnableExtensions
chcp 65001 >nul 2>&1
cd /d "%~dp0"

set "PYTHON_EXE=C:\Users\98319\.workbuddy\binaries\python\envs\smartlabhub\Scripts\python.exe"
set "STOPPER=%~dp0platform\tools\stop_platform.py"

if not exist "%PYTHON_EXE%" (
    echo [ERROR] Python runtime not found:
    echo %PYTHON_EXE%
    pause
    exit /b 2
)

if not exist "%STOPPER%" (
    echo [ERROR] Stopper script not found:
    echo %STOPPER%
    pause
    exit /b 2
)

"%PYTHON_EXE%" "%STOPPER%" %*
set "RC=%ERRORLEVEL%"

echo.
pause
exit /b %RC%