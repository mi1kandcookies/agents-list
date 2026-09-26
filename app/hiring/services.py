"""State transitions for the protected named-agent purchase."""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone

from app.extensions import db
from app.services import is_valid_wallet
from chain.config import get_address, get_chain_config
from chain.ens_v2 import ENSResolutionError, resolve_authorized_agent
from chain.intercepta import quick_scan_address
from chain.payment_policy import canonical_json, sha256_hex, task_hash

PRICE_ATOMIC = 50_000
POLICY_VERSION = "hire-v1"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:20].upper()}"


def _intent_terms(*, owner_id, payer, specialist_name, endpoint, pay_to, chain_id,
                  network, token_address, amount_atomic, task_digest, ens_snapshot,
                  expires_at) -> dict:
    return {
        "ownerId": owner_id,
        "payer": payer.lower(),
        "specialistName": specialist_name,
        "approvedEndpoint": endpoint,
        "payTo": pay_to.lower(),
        "chainId": int(chain_id),
        "network": network,
        "tokenAddress": token_address.lower(),
        "amountAtomic": str(amount_atomic),
        "taskHash": task_digest,
        "ensSnapshot": ens_snapshot,
        "policyVersion": POLICY_VERSION,
        "expiresAt": expires_at.isoformat(),
    }


def create_intent(*, owner_id: str, payer: str, specialist_name: str, task: str,
                  max_usdc: float):
    from app.models import Agent, HireApproval, HireIntent

    owner_id = str(owner_id or "").strip()
    task = str(task or "").strip()
    specialist_name = str(specialist_name or "").strip()
    if not owner_id or len(owner_id) > 160:
        raise ValueError("ownerId is required")
    if not is_valid_wallet(payer):
        raise ValueError("payer must be a 0x-prefixed 40-hex address")
    if not task or len(task) > 40_000:
        raise ValueError("task is required and must be at most 40000 characters")
    try:
        max_atomic = int(round(float(max_usdc) * 1_000_000))
    except (TypeError, ValueError):
        raise ValueError("maxUsdc must be numeric") from None
    if max_atomic < PRICE_ATOMIC:
        raise ValueError("maxUsdc must cover the specialist price of 0.05 USDC")

    try:
        resolved = resolve_authorized_agent(specialist_name)
    except ENSResolutionError:
        raise
    screening = quick_scan_address(resolved.address)
    chain = get_chain_config()
    token = get_address("USDC")
    if not token:
        raise ValueError("USDC_ADDRESS is not configured")
    expires_at = _now() + timedelta(seconds=int(os.environ.get("HIRE_INTENT_TTL_SECONDS", "900")))
    digest = task_hash(task)
    snapshot = resolved.snapshot()
    terms = _intent_terms(
        owner_id=owner_id, payer=payer, specialist_name=resolved.name,
        endpoint=resolved.endpoint, pay_to=resolved.address, chain_id=chain.chain_id,
        network=chain.caip2, token_address=token, amount_atomic=PRICE_ATOMIC,
        task_digest=digest, ens_snapshot=snapshot, expires_at=expires_at,
    )
    listed_agent = Agent.query.filter(Agent.name == resolved.name).first() or Agent.query.first()
    intent = HireIntent(
        id=new_id("HIRE"),
        agent_id=listed_agent.id if listed_agent else None,
        owner_id=owner_id,
        payer=payer,
        specialist_name=resolved.name,
        approved_endpoint=resolved.endpoint,
        pay_to=resolved.address,
        chain_id=chain.chain_id,
        network=chain.caip2,
        token_address=token,
        amount_atomic=PRICE_ATOMIC,
        task=task,
        task_hash=digest,
        ens_snapshot=canonical_json(snapshot),
        canonical_terms=canonical_json(terms),
        policy_version=POLICY_VERSION,
        intent_hash=sha256_hex(canonical_json(terms)),
        status=("awaiting_approval" if screening.decision == "ALLOW"
                else "denied" if screening.decision == "DENY" else "screening_hold"),
        screening=canonical_json(screening.to_dict()),
        expires_at=expires_at,
    )
    db.session.add(intent)
    db.session.flush()
    if screening.decision == "ALLOW":
        approval = HireApproval(
            id=new_id("APPR"), intent_id=intent.id, owner_id=owner_id, payer=payer,
            intent_hash=intent.intent_hash, state="pending",
            action_url=f"/approval/{intent.id}", expires_at=expires_at,
        )
        db.session.add(approval)
    db.session.commit()
    return intent


def get_intent(intent_id: str):
    from app.models import HireIntent
    return db.session.get(HireIntent, intent_id)


def get_validated_approval(intent):
    approval = intent.approval
    if not approval:
        return None, "approval not requested"
    if _aware(approval.expires_at) <= _now() or _aware(intent.expires_at) <= _now():
        if approval.state in ("pending", "approved"):
            approval.state = "expired"
            intent.status = "expired"
            db.session.commit()
        return None, "approval expired"
    if approval.state != "approved":
        return None, f"approval state is {approval.state}"
    if (approval.owner_id != intent.owner_id or approval.payer.lower() != intent.payer.lower()
            or approval.intent_hash != intent.intent_hash):
        return None, "approval does not match the frozen owner, payer, or intent"
    return approval, "approved"


def request_approval(intent):
    from app.models import HireApproval
    if intent.status not in ("awaiting_approval", "approved"):
        return None
    if not intent.approval:
        intent.approval = HireApproval(
            id=new_id("APPR"), intent_id=intent.id, owner_id=intent.owner_id,
            payer=intent.payer, intent_hash=intent.intent_hash, state="pending",
            action_url=f"/approval/{intent.id}", expires_at=intent.expires_at,
        )
        db.session.commit()
    return intent.approval


def record_local_decision(approval, *, owner_id: str, state: str, reason: str = ""):
    if owner_id != approval.owner_id:
        raise PermissionError("approval owner does not match")
    if state not in {"approved", "denied", "cancelled"}:
        raise ValueError("state must be approved, denied, or cancelled")
    if approval.state != "pending":
        raise ValueError(f"approval is already finalized as {approval.state}")
    if _aware(approval.expires_at) <= _now():
        approval.state = "expired"
        approval.intent.status = "expired"
        db.session.commit()
        return approval
    approval.state = state
    approval.denial_reason = str(reason or "")[:500]
    approval.validated_at = _now() if state == "approved" else None
    approval.intent.status = "approved" if state == "approved" else "denied"
    approval.intent.denial_reason = approval.denial_reason
    db.session.commit()
    return approval


def record_validated_approval(approval, *, owner_id: str, payer: str,
                              intent_hash: str, expires_at: datetime, proof_id: str):
    if owner_id != approval.owner_id or payer.lower() != approval.payer.lower():
        raise PermissionError("validated approval owner or payer does not match")
    if intent_hash != approval.intent_hash:
        raise ValueError("validated approval intent hash does not match")
    if approval.state != "pending":
        raise ValueError(f"approval is already finalized as {approval.state}")
    if _aware(expires_at) <= _now() or _aware(approval.intent.expires_at) <= _now():
        raise ValueError("validated approval is expired")
    approval.state = "approved"
    # The identity service may issue a shorter consent window than the hire
    # intent. Preserve that stricter expiry for every later payment check.
    approval.expires_at = min(_aware(approval.expires_at), _aware(expires_at))
    approval.validated_at = _now()
    approval.proof_id = str(proof_id or "")[:255]
    approval.intent.status = "approved"
    db.session.commit()
    return approval


def claim_payment(intent, fingerprint: str):
    row = db.session.get(type(intent), intent.id, with_for_update=True, populate_existing=True)
    if row.status != "approved":
        raise ValueError(f"intent payment attempt already claimed from state {row.status}")
    row.status = "payment_pending"
    row.payment_fingerprint = fingerprint
    row.claimed_at = _now()
    db.session.commit()
    return row


def hold_intent(intent, reason: str, *, status: str = "held"):
    intent.status = status
    intent.denial_reason = str(reason or "")[:1000]
    db.session.commit()


def deliver_intent(intent, receipt: dict, result: dict):
    intent.status = "payment_confirmed"
    intent.receipt = json.dumps(receipt, sort_keys=True)
    db.session.commit()
    intent.status = "delivered"
    intent.result = json.dumps(result, sort_keys=True)
    db.session.commit()


def status_payload(intent) -> dict:
    data = intent.to_dict()
    if intent.approval:
        data["approval"] = intent.approval.to_dict()
    return data
