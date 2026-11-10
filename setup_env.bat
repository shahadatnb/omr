@echo off
chcp 65001 >nul
title OMR Engine - Environment Setup

echo =======================================================
echo      OMR Engine - ভার্চুয়াল এনভায়রনমেন্ট (.venv) ও সেটআপ
echo =======================================================
echo.

cd /d "%~dp0"

:: ১. পাইথন এক্সিকিউটেবল খুঁজে বের করা
set "PYTHON_CMD="

:: সিস্টেম পাইথন পরীক্ষা
python --version >nul 2>&1
if %errorlevel% equ 0 (
    set "PYTHON_CMD=python"
)

:: যদি সিস্টেমে না থাকে বা সমস্যা করে, Laragon বা লোকাল পাইথন চেক করা
if not defined PYTHON_CMD (
    if exist "D:\laragon\bin\python\python-3.10\python.exe" (
        set "PYTHON_CMD=D:\laragon\bin\python\python-3.10\python.exe"
    ) else if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" (
        set "PYTHON_CMD=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    ) else if exist "%LOCALAPPDATA%\Programs\Python\Python310\python.exe" (
        set "PYTHON_CMD=%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
    )
)

if not defined PYTHON_CMD (
    echo [ERROR] পাইথন ইনস্টলেশন খুঁজে পাওয়া যায়নি!
    echo অনুগ্রহ করে পাইথন ইনস্টল করে সিস্টেমের PATH-এ যুক্ত করুন।
    echo অথবা Laragon-এর পাইথন চালু আছে কিনা নিশ্চিত করুন।
    echo.
    pause
    exit /b 1
)

echo [১/৪] পাইথন সনাক্ত হয়েছে:
"%PYTHON_CMD%" --version
echo.

:: ২. .venv ভার্চুয়াল এনভায়রনমেন্ট তৈরি করা
if not exist ".venv\Scripts\python.exe" (
    echo [২/৪] নতুন ভার্চুয়াল এনভায়রনমেন্ট (.venv) তৈরি করা হচ্ছে...
    echo অনুগ্রহ করে কিছুক্ষণ অপেক্ষা করুন...
    "%PYTHON_CMD%" -m venv .venv
    
    if not exist ".venv\Scripts\python.exe" (
        echo [ERROR] .venv তৈরি করতে ব্যর্থ হয়েছে!
        echo পাইথন 'venv' মডিউল সমর্থন করছে কিনা যাচাই করুন।
        pause
        exit /b 1
    )
    echo [✓] .venv ভার্চুয়াল এনভায়রনমেন্ট সফলভাবে তৈরি হয়েছে।
) else (
    echo [২/৪] .venv ভার্চুয়াল এনভায়রনমেন্ট ইতিপূর্বে তৈরি আছে।
)
echo.

:: ৩. .venv-এর ভেতরে প্রয়োজনীয় লাইব্রেরি ইনস্টল করা
echo [৩/৪] .venv-এর মধ্যে প্রয়োজনীয় লাইব্রেরি ইনস্টল/আপডেট করা হচ্ছে...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
echo.

:: ৪. .env কনফিগারেশন ফাইল তৈরি করা (যদি না থাকে)
if not exist ".env" (
    echo [৪/৪] .env কনফিগারেশন ফাইল তৈরি করা হচ্ছে...
    (
        echo DJANGO_SECRET_KEY=omr-engine-super-secret-key-2026
        echo DEBUG=True
        echo LARAVEL_API_URL=http://127.0.0.1:8000/api/omr
    ) > .env
    echo [✓] .env ফাইল তৈরি হয়েছে।
) else (
    echo [৪/৪] .env কনফিগারেশন ফাইল বিদ্যমান।
)
echo.

:: ডাটাবেজ মাইগ্রেশন চালানো
echo ডাটাবেজ মাইগ্রেশন আপডেট করা হচ্ছে...
".venv\Scripts\python.exe" manage.py migrate

echo.
echo =======================================================
echo   [✓] .venv সেটআপ সফলভাবে সম্পন্ন হয়েছে!
echo   এখন "start.bat" ফাইলটিতে ডাবল ক্লিক করে ওএমআর চালু করুন।
echo =======================================================
echo.
pause
