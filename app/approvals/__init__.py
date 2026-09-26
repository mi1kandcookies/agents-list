"""Approval API package and approval primitives."""

from flask import Blueprint

bp = Blueprint("approvals", __name__, url_prefix="/api/approvals")

from app.approvals import routes  # noqa: E402,F401
