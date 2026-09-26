"""Canonical action terms and hashes for one-time human approvals."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from chain.payment_policy import canonical_json

APPROVAL_STATES = {
    "created", "pending", "approved", "consumed", "denied", "expired",
    "cancelled", "rejected", "blocked", "failed",
}


@dataclass(frozen=True)
class ActionTerms:
    action_type: str
    engagement_id: str
    payer: str
    payee: str
    amount_atomic: int
    asset: str
    chain_id: int
    screening_verdict_id: str
    expires_at: int
    details: dict

    def to_dict(self) -> dict:
        return {
            "actionType": self.action_type,
            "engagementId": self.engagement_id,
            "payer": self.payer.lower(),
            "payee": self.payee.lower(),
            "amountAtomic": str(int(self.amount_atomic)),
            "asset": self.asset.lower(),
            "chainId": int(self.chain_id),
            "screeningVerdictId": self.screening_verdict_id,
            "expiresAt": int(self.expires_at),
            "details": self.details,
        }


def canonical_action_payload(approval_id: str, expires_at: int, terms: dict) -> dict:
    return {
        "approvalId": str(approval_id),
        "exp": int(expires_at),
        "action": terms,
    }


def action_hash(approval_id: str, expires_at: int, terms: dict) -> str:
    payload = canonical_json(canonical_action_payload(approval_id, expires_at, terms))
    return "0x" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def validate_transition(current: str, target: str) -> None:
    allowed = {
        "created": {"pending", "cancelled", "rejected"},
        "pending": {"approved", "denied", "expired", "cancelled", "rejected", "blocked"},
        "approved": {"consumed", "expired", "cancelled", "blocked", "failed"},
        "consumed": set(),
        "denied": set(), "expired": set(), "cancelled": set(),
        "rejected": set(), "blocked": set(), "failed": set(),
    }
    if target not in APPROVAL_STATES or target not in allowed.get(current, set()):
        raise ValueError(f"invalid approval transition: {current} -> {target}")
