"""Names: agent and job names issued by the names sidecar."""
from flask import Blueprint

bp = Blueprint("names", __name__, url_prefix="/api/names")

from app.names import routes  # noqa: E402,F401
