"""Mandates: scoped spending authority for sub-hires."""
from flask import Blueprint

bp = Blueprint("mandates", __name__, url_prefix="/api/mandates")

from app.mandates import routes  # noqa: E402,F401
