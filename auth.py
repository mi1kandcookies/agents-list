"""
auth.py - Authentication scaffolding for AgentHire.

require_api_key
---------------
Decorator that enforces X-Api-Key header authentication on sensitive routes
(admin mutations, seller write routes).

Behavior:
  - If API_KEY env var is NOT set: decorator is a no-op (local dev works without config).
  - If API_KEY IS set: request must include `X-Api-Key: <key>` header with matching value.
  - On failure: returns 401 JSON with a descriptive error.

Future extension points (TODO):
  - Swap API_KEY check for JWT verification (add PyJWT dep, decode bearer token).
  - Add role-based access (buyer / seller / admin roles) as a second decorator argument.
  - Integrate Flask-Login for session-based auth on UI routes.

Usage:
    from auth import require_api_key

    @app.route("/admin/payouts/<id>/release", methods=["POST"])
    @require_api_key
    def admin_release_payout(id):
        ...
"""
import os
import functools
from flask import request, jsonify, current_app


def require_api_key(f):
    """
    Decorator - enforce X-Api-Key header if API_KEY env var is configured.
    Falls through (no-op) when API_KEY is not set.
    """
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        api_key = current_app.config.get("API_KEY") or os.environ.get("API_KEY")
        if not api_key:
            # Auth not configured - allow all requests (local dev mode)
            return f(*args, **kwargs)

        provided = request.headers.get("X-Api-Key", "").strip()
        if not provided:
            return jsonify({
                "error": "missing X-Api-Key header",
                "hint": "include X-Api-Key: <your_key> in the request",
            }), 401
        if provided != api_key:
            return jsonify({"error": "invalid API key"}), 401

        return f(*args, **kwargs)
    return decorated
