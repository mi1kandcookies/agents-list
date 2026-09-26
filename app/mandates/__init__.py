"""Delegated mandate API boundary."""

from flask import Blueprint

bp = Blueprint("mandates", __name__, url_prefix="/api/mandates")
