"""Approvals: approval pages (/approvals/<id>) and their JSON API (/api/approvals/<id>)."""
from flask import Blueprint

bp = Blueprint("approvals", __name__)

from app.approvals import routes  # noqa: E402,F401
