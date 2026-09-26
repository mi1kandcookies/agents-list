"""Mandates: scoped spending authority for sub-hires.

Routes: ``/api/mandates/<id>`` and ``/api/engagements/<id>/chain`` (no
url_prefix, since the chain view lives under the engagement path)."""
from flask import Blueprint

bp = Blueprint("mandates", __name__)

from app.mandates import routes  # noqa: E402,F401
