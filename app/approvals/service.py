"""Approval lifecycle, identity binding, replay protection, and consumption."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from app.approvals.actions import action_hash, validate_transition
from app.approvals.executors import ApprovalConsumptionError, consume_approval
from app.extensions import db


class ApprovalError(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:20].upper()}"


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def create_approval(*, human_id: str, action_type: str, terms: dict,
                    expires_at: datetime, payload: dict | None = None,
                    device_code: str | None = None):
    from app.models import Approval, ApprovalEvent

    approval_id = _new_id("APR")
    exp = int(_aware(expires_at).timestamp())
    digest = action_hash(approval_id, exp, terms)
    row = Approval(
        id=approval_id, human_id=human_id, action_type=action_type,
        action_hash=digest, state="created", expires_at=expires_at,
        device_code=device_code, payload_json=json.dumps(payload or {}, sort_keys=True),
    )
    db.session.add(row)
    db.session.flush()
    db.session.add(ApprovalEvent(approval_id=approval_id, event="created", actor="service"))
    validate_transition("created", "pending")
    row.state = "pending"
    db.session.add(ApprovalEvent(approval_id=approval_id, event="pending", actor="service"))
    db.session.commit()
    return row


def get_approval(approval_id: str):
    from app.models import Approval
    return db.session.get(Approval, approval_id)


def _transition(row, state: str, *, actor: str, reason: str = ""):
    from app.models import ApprovalEvent
    validate_transition(row.state, state)
    row.state = state
    if reason:
        row.denial_reason = reason[:1000]
    db.session.add(ApprovalEvent(approval_id=row.id, event=state, actor=actor,
                                metadata_json=json.dumps({"reason": reason} if reason else {})))


def record_decision(approval_id: str, *, state: str, actor: str, reason: str = ""):
    row = get_approval(approval_id)
    if not row:
        raise ApprovalError("approval not found")
    if state not in {"denied", "cancelled", "blocked"}:
        raise ApprovalError("decision must be denied, cancelled, or blocked")
    _transition(row, state, actor=actor, reason=reason)
    db.session.commit()
    return row


def bind_device_code(approval_id: str, device_code: str):
    row = get_approval(approval_id)
    if not row:
        raise ApprovalError("approval not found")
    if row.state != "pending":
        raise ApprovalError("device code can only bind to a pending approval")
    row.device_code = device_code
    db.session.commit()
    return row


def _ensure_human(row, claims: dict):
    from app.models import Human
    human = db.session.get(Human, row.human_id)
    if not human or human.world_sub != claims.get("sub"):
        raise ApprovalError("World identity does not match the approval human")
    return human


def approve_from_claims(approval_id: str, claims: dict, *, device_code: str | None = None):
    """Approve only after an already-validated World ID token matches the action."""
    from app.models import UsedIdTokenJti, ApprovalEvent

    row = get_approval(approval_id)
    if not row:
        raise ApprovalError("approval not found")
    if row.state != "pending":
        raise ApprovalError(f"approval is already {row.state}")
    _ensure_human(row, claims)
    nonce = claims.get("nonce")
    if nonce:
        if nonce != row.action_hash:
            raise ApprovalError("World nonce does not match the exact action")
    elif not device_code or device_code != row.device_code:
        raise ApprovalError("device approval is not bound to this action")
    auth_time = int(claims.get("auth_time", 0))
    created = int(_aware(row.created_at).timestamp())
    if auth_time < created:
        raise ApprovalError("World authentication predates the approval action")
    jti = str(claims.get("jti") or "")
    if not jti or db.session.get(UsedIdTokenJti, jti):
        raise ApprovalError("World ID token replay detected")
    expires = _aware(row.expires_at)
    if expires <= _now():
        _transition(row, "expired", actor="world")
        db.session.commit()
        raise ApprovalError("approval expired")
    row.auth_time = datetime.fromtimestamp(auth_time, tz=timezone.utc)
    row.jti = jti
    db.session.add(UsedIdTokenJti(jti=jti, human_id=row.human_id))
    _transition(row, "approved", actor="world")
    db.session.commit()
    return row


def expire_due_approval(approval_id: str):
    row = get_approval(approval_id)
    if not row:
        raise ApprovalError("approval not found")
    if row.state in {"pending", "approved"} and _aware(row.expires_at) <= _now():
        _transition(row, "expired", actor="expiry-worker")
        db.session.commit()
    return row


__all__ = [
    "ApprovalError", "ApprovalConsumptionError", "approve_from_claims", "bind_device_code",
    "consume_approval", "create_approval", "expire_due_approval", "get_approval", "record_decision",
]
