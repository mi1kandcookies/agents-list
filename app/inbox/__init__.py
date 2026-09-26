"""Inbox: completed work delivered to the buyer (/inbox)."""
from flask import Blueprint

bp = Blueprint("inbox", __name__)

from app.inbox import routes  # noqa: E402,F401
