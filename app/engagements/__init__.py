"""Engagement and milestone API boundary."""

from flask import Blueprint

bp = Blueprint("engagements", __name__, url_prefix="/api/engagements")

from app.engagements import routes  # noqa: E402,F401
