"""Routes for the names blueprint (docs/decisions/0001-custody-chain.md §10)."""
from flask import jsonify, render_template, request

from app.auth import require_api_key
from app.engagements.routes import _api
from app.names import bp, service


@bp.get("/api/names/tree")
def names_tree():
    return jsonify(service.tree(request.args.get("root") or None))


@bp.get("/api/names/resolve")
def names_resolve():
    name = (request.args.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required", "code": "NAME_REQUIRED"}), 400
    row = service.resolve_name(name)
    if row is None:
        return jsonify({"error": "active ENS agent name not found", "code": "NAME_NOT_ACTIVE",
                        "name": name.lower().rstrip(".")}), 404
    return jsonify(row)


@bp.get("/api/names/available")
@_api
def names_available():
    """Is ``label`` free for an agent name? ``agent_id`` (optional) is the
    listing asking, so its own claim does not count as taken."""
    label = request.args.get("label")
    if label is None:
        return jsonify({"error": "label is required", "code": "LABEL_REQUIRED"}), 400
    agent_id = request.args.get("agent_id", type=int)
    return jsonify(service.check_label(label, agent_id))


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
