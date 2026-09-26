"""Human identity: OpenID Connect client for World ID (see app/identity/world.py).

The ``identity`` blueprint will serve /login and /auth/world/callback.
"""
from flask import Blueprint

bp = Blueprint("identity", __name__)

from app.identity import routes  # noqa: E402,F401
