"""Single-use approval consumption boundary."""
from __future__ import annotations

from datetime import datetime, timezone

from app.extensions import db
from app.approvals.actions import validate_transition


class ApprovalConsumptionError(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def consume_approval(approval_id: str, expected_action_hash: str, *, human_id: str | None = None):
    """Atomically consume one approved action after matching its full hash."""
    from app.models import Approval, ApprovalEvent

    row = db.session.get(Approval, approval_id, with_for_update=True, populate_existing=True)
    if not row:
        raise ApprovalConsumptionError("approval not found")
    if row.state != "approved":
        raise ApprovalConsumptionError(f"approval is {row.state}, not approved")
    if row.action_hash != expected_action_hash:
        raise ApprovalConsumptionError("approval action hash does not match")
    if human_id and row.human_id != human_id:
        raise ApprovalConsumptionError("approval human does not match")
    expires = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=timezone.utc)
    if expires <= _now():
        validate_transition(row.state, "expired")
        row.state = "expired"
        db.session.add(ApprovalEvent(approval_id=row.id, event="expired", actor="executor"))
        db.session.commit()
        raise ApprovalConsumptionError("approval expired")
    validate_transition(row.state, "consumed")
    row.state = "consumed"
    row.consumed_at = _now()
    db.session.add(ApprovalEvent(approval_id=row.id, event="consumed", actor="executor"))
    db.session.commit()
    return row
