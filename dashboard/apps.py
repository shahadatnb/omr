from django.apps import AppConfig
from django.db.backends.signals import connection_created


def activate_sqlite_optimizations(sender, connection, **kwargs):
    """
    Enables SQLite WAL (Write-Ahead Logging) mode, synchronous=NORMAL,
    and a 30s busy timeout for maximum concurrent read/write performance.
    """
    if connection.vendor == 'sqlite':
        try:
            with connection.cursor() as cursor:
                cursor.execute('PRAGMA journal_mode=WAL;')
                cursor.execute('PRAGMA synchronous=NORMAL;')
                cursor.execute('PRAGMA busy_timeout=30000;')
        except Exception:
            pass


class DashboardConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'dashboard'

    def ready(self):
        connection_created.connect(activate_sqlite_optimizations)

