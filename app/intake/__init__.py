"""Intake: the guided "Describe your job" flow (/new) and the estimate and
contract review for an existing engagement (/estimate/<engagement_id>)."""
from flask import Blueprint

bp = Blueprint("intake", __name__)

from app.intake import routes  # noqa: E402,F401
