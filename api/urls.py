from django.urls import path
from .views import HealthCheckView, ProcessSheetView

urlpatterns = [
    path('health/', HealthCheckView.as_view(), name='api-health'),
    path('process/', ProcessSheetView.as_view(), name='api-process'),
]
