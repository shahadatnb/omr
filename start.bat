@echo off
chcp 65001 >nul
title OMR Engine Dashboard Server

echo =======================================================
echo          OMR Engine Dashboard Server
echo =======================================================
echo.

cd /d "%~dp0"

REM 1. Clear conflicting Python environment variables
set PYTHONHOME=
set PYTHONPATH=

REM 2. Determine Python runner
set PY_RUN=
if exist ".venv\Scripts\python.exe" (
    set "PY_RUN=.venv\Scripts\python.exe"
) else if exist "venv\Scripts\python.exe" (
    set "PY_RUN=venv\Scripts\python.exe"
) else (
    set "PY_RUN=python"
)

REM 3. Launch default browser
start http://127.0.0.1:8080/

echo OMR Server running at: http://127.0.0.1:8080/
echo To stop the server, press Ctrl + C or close this window.
echo.

"%PY_RUN%" manage.py runserver 8080

pause
