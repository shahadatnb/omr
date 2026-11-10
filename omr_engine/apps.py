"""
omr_engine/apps.py
Django App configuration for OMR Engine.
"""

from django.apps import AppConfig


class OmrEngineConfig(AppConfig):
    """OMR Engine অ্যাপের Django কনফিগারেশন।"""
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'omr_engine'
    verbose_name = 'OMR Engine'  # Admin panel এ এই নামে দেখাবে
