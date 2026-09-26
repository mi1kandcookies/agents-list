"""World identity integration boundary."""

from flask import Blueprint

bp = Blueprint("identity", __name__, url_prefix="/auth/world")

from app.identity import routes  # noqa: E402,F401
