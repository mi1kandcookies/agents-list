"""Read-only approval status and explicit non-identity decisions."""
from __future__ import annotations

from flask import jsonify, request

from app.approvals import bp
from app.approvals.service import ApprovalError, get_approval, record_decision


@bp.route("/<approval_id>")
def approval_status(approval_id):
    row = get_approval(approval_id)
    if not row:
        return jsonify({"error": "approval not found", "code": "APPROVAL_NOT_FOUND"}), 404
    return jsonify({
        "approvalId": row.id, "humanId": row.human_id, "actionType": row.action_type,
        "actionHash": row.action_hash, "state": row.state,
        "expiresAt": row.expires_at.isoformat(), "consumedAt": row.consumed_at.isoformat() if row.consumed_at else None,
    })


@bp.route("/<approval_id>/decision", methods=["POST"])
def approval_decision(approval_id):
    body = request.get_json(silent=True) or {}
    try:
        row = record_decision(approval_id, state=str(body.get("state") or ""),
                              actor=str(body.get("actor") or "api"),
                              reason=str(body.get("reason") or ""))
    except ApprovalError as exc:
        return jsonify({"error": str(exc), "code": "APPROVAL_REJECTED"}), 400
    return jsonify({"approvalId": row.id, "state": row.state, "reason": row.denial_reason})
