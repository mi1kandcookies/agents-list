"""Routes for the names blueprint (docs/decisions/0001-custody-chain.md §10)."""
from flask import jsonify, render_template, request

from app.auth import require_api_key
from app.names import bp, service


@bp.get("/api/names/tree")
def names_tree():
    return jsonify(service.tree(request.args.get("root") or None))


@bp.post("/api/names/<path:name>/retry")
@require_api_key
def names_retry(name: str):
    row = service.retry(name.strip().lower())
    if row is None:
        return jsonify({"error": "name not found", "code": "NOT_FOUND"}), 404
    return jsonify({"name": row.name, "kind": row.kind, "status": row.status,
                    "tx_hashes": row.tx_hashes or []})


@bp.get("/names")
def names_page():
    root = request.args.get("root") or None
    return render_template("names.html", tree=service.tree(root))
