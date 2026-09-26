"""Routes for the screening blueprint (docs/decisions/0001-custody-chain.md §4)."""
from flask import jsonify

from app.extensions import db
from app.models import Screening
from app.screening import bp
from app.screening.service import verdict_from_row


@bp.get("/<screening_id>")
def get_screening(screening_id: str):
    """GET /api/screening/<SCR-…>: the stored verdict."""
    row = db.session.get(Screening, screening_id)
    if row is None:
        return jsonify({"error": "screening not found", "code": "NOT_FOUND",
                        "field": "screening_id"}), 404
    return jsonify(verdict_from_row(row))
