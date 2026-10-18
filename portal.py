"""Production portal factory. No schema creation, secrets rotation or device I/O."""
import os
import sqlite3
from pathlib import Path

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from werkzeug.middleware.proxy_fix import ProxyFix

from app import create_app
from demo_safety import disable_device_transports
from models import db

PRODUCTION_DATABASE = Path('/opt/ingecon-control/instance/production/ingecon.sqlite')
PORTAL_HOSTS = ['monitoring.local', '192.168.10.234', 'localhost', '127.0.0.1']


def validate_database(uri):
    try:
        url = make_url(uri)
        path = Path(url.database or '')
        if (url.drivername != 'sqlite' or url.query or not path.is_absolute() or
                path != PRODUCTION_DATABASE or path.is_symlink() or not path.is_file()):
            raise ValueError
        with sqlite3.connect(path.as_uri()+'?mode=ro', uri=True) as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'demo_metadata' in tables or not set(db.metadata.tables) <= tables:
            raise ValueError
    except (ArgumentError, TypeError, ValueError, sqlite3.Error):
        raise RuntimeError('Portal requires the existing, prepared production SQLite database; no automatic migrations are allowed.') from None


def create_portal_app():
    uri = os.environ.get('INGECON_DATABASE_URI', '')
    validate_database(uri)
    application = create_app({
        'SQLALCHEMY_DATABASE_URI': uri,
        'SQLALCHEMY_ENGINE_OPTIONS': {'connect_args': {'timeout': 5}},
        'TRUSTED_HOSTS': PORTAL_HOSTS,
        'MODBUS_WRITES_ENABLED': False,
    })
    # Gunicorn is loopback-only. Nginx overwrites X-Forwarded-For with its real
    # peer address; never trust client-supplied host/proto/prefix headers.
    original = application.wsgi_app
    fixed = ProxyFix(original, x_for=1, x_proto=0, x_host=0, x_port=0, x_prefix=0)
    def loopback_proxy(environ, start_response):
        return (fixed if environ.get('REMOTE_ADDR') == '127.0.0.1' else original)(environ, start_response)
    application.wsgi_app = loopback_proxy
    disable_device_transports()
    return application
