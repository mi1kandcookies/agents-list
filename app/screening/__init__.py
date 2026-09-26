"""Screening API boundary."""

from flask import Blueprint

bp = Blueprint("screening", __name__, url_prefix="/api/screenings")
