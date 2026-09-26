"""WSGI entrypoint: `gunicorn wsgi:app` (or `flask --app wsgi run`)."""
from app import create_app

app = create_app()
