import os
import ssl
import sys
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.contrib.staticfiles.handlers import StaticFilesHandler
from django.core.management.base import CommandError
from django.core.management.commands import runserver
from django.core.servers.basehttp import WSGIRequestHandler, ThreadedWSGIServer
from django.utils import translation

try:
    from django.core.servers.basehttp import WSGIServerException
except ImportError:
    from socket import error as WSGIServerException


class SecureHTTPServer(ThreadedWSGIServer):
    """
    Threaded WSGI Server wrapped with standard Python SSLContext.
    Fully compatible with Python 3.10, 3.11, 3.12, 3.13, 3.14, and 3.15+.
    """
    def __init__(self, address, handler_cls, certificate, key, ipv6=False):
        super().__init__(address, handler_cls, ipv6=ipv6)
        
        # Modern SSLContext (replaces deprecated ssl.wrap_socket)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=certificate, keyfile=key)
        self.socket = ctx.wrap_socket(self.socket, server_side=True)


class SecureWSGIRequestHandler(WSGIRequestHandler):
    def get_environ(self):
        env = super().get_environ()
        env['HTTPS'] = 'on'
        return env


def default_ssl_files_dir():
    """Locate the default self-signed development certificates in config/certs/."""
    base_dir = Path(settings.BASE_DIR)
    cert_dir = base_dir / 'config' / 'certs'
    if not cert_dir.exists():
        cert_dir.mkdir(parents=True, exist_ok=True)
    return str(cert_dir)


class Command(runserver.Command):
    help = "Run a Django development server over HTTPS (compatible with Python 3.10 - 3.15+)"

    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument(
            "--certificate",
            default=os.path.join(default_ssl_files_dir(), "development.crt"),
            help="Path to the SSL certificate (.crt)",
        )
        parser.add_argument(
            "--key",
            default=os.path.join(default_ssl_files_dir(), "development.key"),
            help="Path to the SSL private key (.key)",
        )
        parser.add_argument(
            "--nostatic",
            dest='use_static_handler',
            action='store_false',
            default=None,
            help="Do not use internal static file handler",
        )
        parser.add_argument(
            "--static",
            dest='use_static_handler',
            action='store_true',
            help="Use internal static file handler",
        )

    def get_handler(self, *args, **options):
        handler = super().get_handler(*args, **options)
        if self.should_use_static_handler(options):
            return StaticFilesHandler(handler)
        return handler

    def should_use_static_handler(self, options):
        use_static_handler = options.get('use_static_handler')
        if use_static_handler:
            return True
        if (use_static_handler is None and 'django.contrib.staticfiles' in settings.INSTALLED_APPS):
            return True
        return False

    def check_certs(self, key_file, cert_file):
        if not os.path.exists(key_file):
            raise CommandError(f"SSL Key file not found at: {key_file}")
        if not os.path.exists(cert_file):
            raise CommandError(f"SSL Certificate file not found at: {cert_file}")

    def inner_run(self, *args, **options):
        key_file = options.get("key")
        cert_file = options.get("certificate")
        self.check_certs(key_file, cert_file)

        shutdown_message = options.get('shutdown_message', '')
        quit_command = 'CTRL-BREAK' if sys.platform == 'win32' else 'CONTROL-C'

        self.stdout.write("Validating models...\n\n")
        self.check(display_num_errors=True)
        self.stdout.write((
            "%(started_at)s\n"
            "Django version %(version)s, using settings %(settings)r\n"
            "Starting development server at https://%(addr)s:%(port)s/\n"
            "Using SSL certificate: %(cert)s\n"
            "Using SSL key: %(key)s\n"
            "Quit the server with %(quit_command)s.\n"
        ) % {
            "started_at": datetime.now().strftime('%B %d, %Y - %X'),
            "version": self.get_version(),
            "settings": settings.SETTINGS_MODULE,
            "addr": f"[{self.addr}]" if self._raw_ipv6 else self.addr,
            "port": self.port,
            "quit_command": quit_command,
            "cert": cert_file,
            "key": key_file,
        })
        translation.activate(settings.LANGUAGE_CODE)

        try:
            handler = self.get_handler(*args, **options)
            server = SecureHTTPServer(
                (self.addr, int(self.port)),
                SecureWSGIRequestHandler,
                cert_file,
                key_file,
                ipv6=self.use_ipv6,
            )
            server.set_app(handler)
            server.serve_forever()

        except WSGIServerException as e:
            ERRORS = {
                13: "You don't have permission to access that port.",
                98: "That port is already in use.",
                99: "That IP address can't be assigned-to.",
            }
            try:
                error_text = ERRORS[e.args[0].args[0]]
            except (AttributeError, KeyError):
                error_text = str(e)
            self.stderr.write(f"Error: {error_text}")
            os._exit(1)
        except KeyboardInterrupt:
            if shutdown_message:
                self.stdout.write(shutdown_message)
            sys.exit(0)
