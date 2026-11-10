@echo off
chcp 65001 >nul
title OMR Engine - Environment Setup

echo =======================================================
echo      OMR Engine - Environment Setup
echo =======================================================
echo.

cd /d "%~dp0"

REM 1. Clear conflicting Python environment variables
set PYTHONHOME=
set PYTHONPATH=

REM 2. Detect Python executable
set PYTHON_CMD=
python --version >nul 2>&1
if %errorlevel% equ 0 (
    set PYTHON_CMD=python
)

if not defined PYTHON_CMD (
    py --version >nul 2>&1
    if %errorlevel% equ 0 (
        set PYTHON_CMD=py
    )
)

if not defined PYTHON_CMD (
    if exist "%LOCALAPPDATA%\Programs\Python\Python315\python.exe" (
        set PYTHON_CMD=%LOCALAPPDATA%\Programs\Python\Python315\python.exe"
    ) else if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
        set PYTHON_CMD=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    ) else if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" (
        set PYTHON_CMD=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    ) else if exist "D:\laragon\bin\python\python-3.10\python.exe" (
        set PYTHON_CMD=D:\laragon\bin\python\python-3.10\python.exe"
    )
)

if not defined PYTHON_CMD (
    echo [ERROR] Python installation not found!
    echo Please make sure Python is installed and added to PATH.
    echo.
    pause
    exit /b 1
)

echo [1/4] Detected Python:
"%PYTHON_CMD%" --version
echo.

REM 3. Virtual environment setup
if exist ".venv\Scripts\python.exe" (
    echo [2/4] Virtual environment .venv already exists:
    ".venv\Scripts\python.exe" --version
) else (
    echo [2/4] Creating new virtual environment .venv...
    "%PYTHON_CMD%" -m venv .venv
    if not exist ".venv\Scripts\python.exe" (
        echo [ERROR] Failed to create .venv!
        pause
        exit /b 1
    )
    echo [OK] Virtual environment .venv created successfully.
)
echo.

REM 4. Install dependencies
echo [3/4] Installing / Updating libraries in .venv...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
echo.

REM 5. Create .env if not exists
if not exist ".env" (
    echo [4/4] Creating default .env configuration...
    echo DJANGO_SECRET_KEY=omr-engine-super-secret-key-2026 > .env
    echo DEBUG=True >> .env
    echo LARAVEL_API_URL=http://127.0.0.1:8000/api/omr >> .env
    echo [OK] .env created.
) else (
    echo [4/4] .env already exists.
)
echo.

REM 6. Run database migrations
echo Updating database migrations...
".venv\Scripts\python.exe" manage.py migrate

echo.
echo =======================================================
echo   [OK] Setup complete!
echo   Run "start.bat" to launch OMR Engine Dashboard.
echo =======================================================
echo.
pause
