@echo off
chcp 65001 >nul 2>&1
setlocal enabledelayedexpansion
title Resume AI Workbench - Local Service
cd /d "%~dp0"

if not defined RW_PORT set "RW_PORT=8420"

echo.
echo   Resume AI Workbench  -  Local Service
echo   ======================================================
echo   (Chinese output is printed by the service below)
echo.

REM ============================================================
REM  1. find a working Python (skip the Microsoft Store stub)
REM     Override with:  set RW_PYTHON=D:\Python\python 3.11\python.exe
REM ============================================================
set "PY="
if defined RW_PYTHON if exist "%RW_PYTHON%" (
    "%RW_PYTHON%" -c "print(1)" >nul 2>&1
    if not errorlevel 1 set "PY=%RW_PYTHON%"
)
if not defined PY (
    for /f "delims=" %%i in ('where python 2^>nul') do (
        if not defined PY (
            "%%i" -c "print(1)" >nul 2>&1
            if not errorlevel 1 set "PY=%%i"
        )
    )
)

if not defined PY (
    for %%P in (
        "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
        "C:\Python313\python.exe"
        "C:\Python312\python.exe"
        "C:\Python311\python.exe"
        "C:\Python310\python.exe"
        "D:\Python\python 3.11\python.exe"
        "%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\python.exe"
        "D:\WorkBuddy_System\.workbuddy\binaries\python\versions\3.13.12\python.exe"
    ) do (
        if not defined PY if exist %%P (
            "%%~P" -c "print(1)" >nul 2>&1
            if not errorlevel 1 set "PY=%%~P"
        )
    )
)

if not defined PY (
    echo   [ERROR] Python not found.
    echo.
    echo   This service needs Python 3.9 or newer.
    echo   Download:  https://www.python.org/downloads/
    echo   During setup, tick "Add Python to PATH", then run start.bat again.
    echo.
    pause
    exit /b 1
)

echo   Python exe  %PY%

REM ============================================================
REM  2. is the port already in use?
REM ============================================================
set "BUSY="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":!RW_PORT! " ^| findstr LISTENING') do set "BUSY=%%p"
if defined BUSY (
    echo.
    echo   [NOTE] Port !RW_PORT! is already in use ^(PID !BUSY!^).
    echo.
    echo   If this service is already running, just open the page below.
    echo   Otherwise pick another port:
    echo       set RW_PORT=8531 ^&^& start.bat
    echo.
    start "" "http://127.0.0.1:!RW_PORT!/"
    timeout /t 4 >nul
    exit /b 0
)

REM ============================================================
REM  3. optional dependency check (not blocking)
REM     libs\ ships with the project, so any Python can read PDFs.
REM ============================================================
"%PY%" -c "import sys;sys.path.insert(0,r'%~dp0libs');import pypdf" >nul 2>&1
if errorlevel 1 (
    echo   pypdf       NOT AVAILABLE  ^(PDF import off; use "paste text" instead^)
    echo               put pypdf into:  %~dp0libs
) else (
    echo   pypdf       OK  ^(bundled or installed^)
)

REM ============================================================
REM  4. start the service, then open the browser
REM ============================================================
echo.
echo   ------------------------------------------------------
echo   Starting ... browser opens in 3 seconds.
echo   Close this window to stop the service (Ctrl+C works too).
echo   ------------------------------------------------------
echo.

start "" cmd /c "timeout /t 3 >nul && start "" http://127.0.0.1:!RW_PORT!/"

"%PY%" -u "server\app.py"
set "CODE=%ERRORLEVEL%"

echo.
if not "%CODE%"=="0" (
    echo   [ERROR] Service exited with code %CODE%
    echo   Common causes: port in use, missing server folder, Python too old.
    echo   Run it manually to see the traceback:
    echo       "%PY%" -u "server\app.py"
) else (
    echo   Service stopped.
)
echo.
pause
