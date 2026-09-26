"""Humans: verified approvers (caps, bans)."""
from flask import Blueprint

bp = Blueprint("humans", __name__, url_prefix="/humans")

from app.humans import routes  # noqa: E402,F401
