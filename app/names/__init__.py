"""Names: agent and job names issued by the names sidecar.

Routes: GET /api/names/tree, GET /api/names/resolve, POST /api/names/<name>/retry,
and the /names page.
"""
from flask import Blueprint

bp = Blueprint("names", __name__)

from app.names import routes  # noqa: E402,F401
