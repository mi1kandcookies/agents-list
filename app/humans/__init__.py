"""Human identity and owner API boundary."""

from flask import Blueprint

bp = Blueprint("humans", __name__, url_prefix="/api/humans")
