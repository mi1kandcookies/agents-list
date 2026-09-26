"""ENS name-tree API boundary."""

from flask import Blueprint

bp = Blueprint("names", __name__, url_prefix="/api/names")
