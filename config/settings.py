"""
config/settings.py
Django settings for OMR Engine project.

এই ফাইলে প্রজেক্টের সব কনফিগারেশন থাকে।
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# .env ফাইল থেকে environment variables লোড করো
load_dotenv()

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = os.getenv('DJANGO_SECRET_KEY', 'django-insecure-omr-engine-dev-key-change-in-production')

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = os.getenv('DEBUG', 'True') == 'True'

ALLOWED_HOSTS = ['*']

# Application definition — ইন্সটল করা অ্যাপসমূহ
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    # Third-party apps
    'rest_framework',
    'corsheaders',
    # Local apps
    'omr_engine.apps.OmrEngineConfig',
    'api.apps.ApiConfig',
    'dashboard.apps.DashboardConfig',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'corsheaders.middleware.CorsMiddleware',  # CORS middleware — অন্যদের আগে রাখতে হবে
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        # dashboard/templates ফোল্ডার থেকে template লোড হবে
        'DIRS': [BASE_DIR / 'dashboard' / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

# Database — SQLite (development এর জন্য)
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# Internationalization
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Asia/Dhaka'  # বাংলাদেশ সময় অঞ্চল
USE_I18N = True
USE_TZ = True

# Static files (CSS, JavaScript, Images)
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

# Media files — uploaded OMR sheet images এখানে রাখা হবে
MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

# Default primary key field type
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# CORS settings — development এ সব origin থেকে request গ্রহণ করো
CORS_ALLOW_ALL_ORIGINS = True

# Django REST Framework settings
REST_FRAMEWORK = {
    'DEFAULT_RENDERER_CLASSES': [
        'rest_framework.renderers.JSONRenderer',
    ],
    'DEFAULT_PARSER_CLASSES': [
        'rest_framework.parsers.JSONParser',
        'rest_framework.parsers.MultiPartParser',  # file upload এর জন্য
        'rest_framework.parsers.FormParser',
    ],
}

# Laravel Integration settings — .env থেকে লোড হবে
LARAVEL_API_URL = os.getenv('LARAVEL_API_URL', 'http://localhost/laravel/laravel-7-admissionchalange/api')
LARAVEL_API_TOKEN = os.getenv('LARAVEL_API_TOKEN', '')

# Celery Configuration — background tasks এর জন্য
CELERY_BROKER_URL = os.getenv('CELERY_BROKER_URL', 'redis://localhost:6379/0')
CELERY_RESULT_BACKEND = os.getenv('CELERY_RESULT_BACKEND', 'redis://localhost:6379/0')
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TIMEZONE = 'Asia/Dhaka'

# OMR Engine settings
OMR_TEMPLATES_DIR = BASE_DIR / 'templates_store'
OMR_OUTPUT_SIZE = (794, 1123)  # A4 resolution (pixels)

# Large Batch File Upload Settings
# Allow uploading folders with thousands of scanned sheets at once
DATA_UPLOAD_MAX_NUMBER_FILES = 10000          # Allow up to 10,000 files in a single request
DATA_UPLOAD_MAX_MEMORY_SIZE = 1024 * 1024 * 1024  # 1 GB max request payload
FILE_UPLOAD_MAX_MEMORY_SIZE = 500 * 1024 * 1024   # 500 MB max in-memory file size
