"""Gunicorn entry point; configuration is read once per worker, never generated."""
from portal import create_portal_app

app = create_portal_app()
