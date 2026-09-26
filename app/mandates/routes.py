"""Routes for the mandates blueprint (docs/decisions/0001-custody-chain.md §5, §7)."""
from __future__ import annotations

from flask import jsonify

from app.extensions import db
from app.mandates import bp, service
from app.mandates.tokens import MandateError, decode
from app.services import api_error


@bp.route("/api/mandates/<mandate_id>")
def api_mandate(mandate_id):
    """A mandate's verified claims plus its live status and balances."""
    from app.models import Mandate
    row = db.session.get(Mandate, mandate_id)
    if row is None:
        return api_error("mandate not found", 404, code="MANDATE_INVALID", field="mandate_id")
    try:
        claims = decode(row.token, verify_exp=False)
    except MandateError as exc:
        return api_error(exc.message, 409, code=exc.code)
    chain_error = None
    try:
        service.verify_chain(row.token)
    except MandateError as exc:
        chain_error = exc.code
    return jsonify({
        "mandate_id": row.id,
        "claims": claims,
        "status": service.status_of(row),
        "chain_valid": chain_error is None,
        "chain_error": chain_error,
        "budget_micro": row.budget_micro,
        "spent_micro": row.spent_micro,
        "reserved_micro": service.reserved_micro(row),
        "remaining_micro": service.remaining_micro(row),
        "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
    })


@bp.route("/api/engagements/<engagement_id>/chain")
def api_engagement_chain(engagement_id):
    """The custody chain for an engagement as ``{nodes, edges}``."""
    from app.models import Engagement
    if db.session.get(Engagement, engagement_id) is None:
        return api_error("engagement not found", 404, code="ENGAGEMENT_NOT_FOUND",
                         field="engagement_id")
    return jsonify(service.chain_graph(engagement_id))
