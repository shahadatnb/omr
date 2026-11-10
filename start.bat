@echo off
chcp 65001 >nul
title OMR Engine Dashboard Server

echo =======================================================
echo          OMR Engine Dashboard চালু করা হচ্ছে...
echo =======================================================
echo.

cd /d "%~dp0"

:: venv থাকলে এক্টিভেট করা
if exist "venv\Scripts\activate.bat" (
    call venv\Scripts\activate.bat
)

:: ব্রাউজারে ড্যাশবোর্ড ওপেন করা
start http://127.0.0.1:8080/

echo ওএমআর সার্ভার চলমান হচ্ছে: http://127.0.0.1:8080/
echo সার্ভার বন্ধ করতে এই উইন্ডো বন্ধ করুন অথবা Ctrl + C চাপুন।
echo.

python manage.py runserver 8080

pause
