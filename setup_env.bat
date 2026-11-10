@echo off
chcp 65001 >nul
title OMR Engine - Environment Setup

echo =======================================================
echo      OMR Engine - ভার্চুয়াল এনভায়রনমেন্ট ও সেটআপ
echo =======================================================
echo.

cd /d "%~dp0"

:: ১. পাইথন চেক করা
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] পাইথন ইনস্টল পাওয়া যায়নি! দয়া করে Python ইনস্টল করুন এবং PATH-এ যুক্ত করুন।
    pause
    exit /b 1
)

echo [১/৪] পাইথন পাওয়া গেছে...
python --version

:: ২. ভার্চুয়াল এনভায়রনমেন্ট (venv) তৈরি করা (যদি না থাকে)
if not exist "venv" (
    echo [২/৪] নতুন ভার্চুয়াল এনভায়রনমেন্ট (venv) তৈরি করা হচ্ছে...
    python -m venv venv
) else (
    echo [২/৪] ভার্চুয়াল এনভায়রনমেন্ট (venv) ইতিপূর্বে তৈরি করা আছে।
)

:: ভেনভ এক্টিভেট করা
call venv\Scripts\activate.bat

:: ৩. ডিপেন্ডেন্সি প্যাকেজসমূহ ইনস্টল বা আপডেট করা
echo [৩/৪] প্রয়োজনীয় লাইব্রেরি ইনস্টল করা হচ্ছে (pip install)...
python -m pip install --upgrade pip
pip install -r requirements.txt

:: ৪. .env ফাইল তৈরি করা (যদি না থাকে)
if not exist ".env" (
    echo [৪/৪] .env কনফিগারেশন ফাইল তৈরি করা হচ্ছে...
    (
        echo DJANGO_SECRET_KEY=omr-engine-super-secret-key-2026
        echo DEBUG=True
        echo LARAVEL_API_URL=http://127.0.0.1:8000/api/omr
    ) > .env
) else (
    echo [৪/৪] .env কনফিগারেশন ফাইল বিদ্যমান।
)

:: ডাটাবেজ মাইগ্রেশন চালানো
echo ডাটাবেজ মাইগ্রেশন আপডেট করা হচ্ছে...
python manage.py migrate

echo.
echo =======================================================
echo   সেটআপ সফলভাবে সম্পন্ন হয়েছে!
echo   এখন "start.bat" ফাইলটিতে ডাবল ক্লিক করে OMR চালু করুন।
echo =======================================================
echo.
pause
