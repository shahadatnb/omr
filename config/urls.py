"""
config/urls.py
Main URL configuration for OMR Engine project.

এই ফাইলে প্রজেক্টের সব URL routing সংজ্ঞায়িত করা হয়েছে।
"""

from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.shortcuts import redirect

urlpatterns = [
    # Django admin panel
    path('admin/', admin.site.urls),

    # REST API endpoints — api/ prefix এর অধীনে
    path('api/', include('api.urls')),

    # Dashboard URLs — dashboard/ prefix এর অধীনে
    path('dashboard/', include('dashboard.urls')),

    # Root URL — dashboard redirect
    path('', lambda request: redirect('dashboard:batch_list')),
]

# Development এ media files serve করো
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
