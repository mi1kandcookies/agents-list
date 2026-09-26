"""Engagements: engagement pages and the JSON API under /api/engagements."""
from flask import Blueprint

bp = Blueprint("engagements", __name__)

from app.engagements import routes  # noqa: E402,F401
