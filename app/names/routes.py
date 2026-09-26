"""Read-only ENSv2 resolution endpoints used by discovery and the UI."""
from __future__ import annotations

from flask import jsonify, request

from app.names import bp
from chain.ens_v2 import ENSResolutionError, resolve_authorized_agent


def _resolve(name: str):
    try:
        return resolve_authorized_agent(name)
    except ENSResolutionError as exc:
        return jsonify({"error": str(exc), "code": "ENS_RESOLUTION_FAILED"}), 409


@bp.get("/resolve")
def resolve_name():
    name = str(request.args.get("name") or "")
    result = _resolve(name)
    if isinstance(result, tuple):
        return result
    return jsonify({"name": result.name, "records": result.snapshot(),
                    "authorization": "ENS records are checked again before payment"})


@bp.get("/<path:name>")
def resolve_name_path(name):
    result = _resolve(name)
    if isinstance(result, tuple):
        return result
    return jsonify(result.snapshot())
