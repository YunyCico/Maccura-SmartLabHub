@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul 2>&1
cd /d "%~dp0"
rem RELEASE mode: backend serves the built frontend, no Node.js needed.
rem Keep pure ASCII + CRLF + thin: non-ASCII becomes mojibake in the
rem OEM code page. Real logic lives in platform\tools\launch_platform.py.
set "PYTHON_EXE="
set "LAUNCHER=%~dp0platform\tools\launch_platform.py"
if defined SLH_PYTHON if exist "%SLH_PYTHON%" set "PYTHON_EXE=%SLH_PYTHON%"
if not defined PYTHON_EXE for /f "delims=" %%i in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do if not defined PYTHON_EXE set "PYTHON_EXE=%%i"
if not defined PYTHON_EXE for /f "delims=" %%i in ('where python 2^>nul') do if not defined PYTHON_EXE (echo %%i|find /i "WindowsApps" >nul||set "PYTHON_EXE=%%i")
if not defined PYTHON_EXE for /f "delims=" %%i in ('dir /b /ad "%LOCALAPPDATA%\Programs\Python\Python3*" 2^>nul') do if not defined PYTHON_EXE if exist "%LOCALAPPDATA%\Programs\Python\%%i\python.exe" set "PYTHON_EXE=%LOCALAPPDATA%\Programs\Python\%%i\python.exe"
if not defined PYTHON_EXE (
    echo [ERROR] No usable Python interpreter found.
    echo   1^) Install Python 3.11+ from https://www.python.org/downloads/
    echo   2^) Tick "Add python.exe to PATH" during install
    echo   3^) Run this file again
    echo   Or: set SLH_PYTHON=C:/path/to/python.exe
    pause
    exit /b 2
)
"%PYTHON_EXE%" "%LAUNCHER%" --release %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (echo.&echo [FAILED] Exit code %RC%. See logs in "logs".&pause)
exit /b %RC%
