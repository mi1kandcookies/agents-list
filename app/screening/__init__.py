"""Screening: payment-hop risk verdicts."""
from flask import Blueprint

bp = Blueprint("screening", __name__, url_prefix="/api/screening")

from app.screening import routes  # noqa: E402,F401
