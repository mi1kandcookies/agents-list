"""Fail-closed action screening policy shared by every money-moving hop."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone


class ScreeningBlocked(RuntimeError):
    pass


@dataclass(frozen=True)
class ScreeningVerdict:
    decision: str
    verdict_id: str
    provider: str
    address: str
    reason: str
    cap_atomic: int | None = None
    checked_at: int = 0
    expires_at: int | None = None

    def to_dict(self) -> dict:
        return {
            "decision": self.decision, "verdictId": self.verdict_id,
            "provider": self.provider, "address": self.address,
            "reason": self.reason, "capAtomic": self.cap_atomic,
            "checkedAt": self.checked_at, "expiresAt": self.expires_at,
        }


def normalize_verdict(payload: dict, *, address: str, provider: str,
                      now: int | None = None) -> ScreeningVerdict:
    """Normalize provider output; unknown/missing decisions become ASK_HUMAN."""
    raw = str(payload.get("decision") or payload.get("status") or "").upper()
    mapping = {"ALLOW": "PAY", "PAY": "PAY", "CAP": "CAP", "DENY": "REFUSE",
               "REFUSE": "REFUSE", "HOLD": "ASK_HUMAN", "ASK_HUMAN": "ASK_HUMAN"}
    decision = mapping.get(raw, "ASK_HUMAN")
    verdict_id = str(payload.get("verdictId") or payload.get("verdict_id") or payload.get("id") or "")
    if not verdict_id:
        # Provider timestamps are evidence metadata, not authorization terms.
        # Excluding them keeps an otherwise identical recheck bound to the same
        # provider decision while still retaining the fresh checked_at value.
        stable_payload = {key: value for key, value in payload.items()
                          if key not in {"checkedAt", "checked_at"}}
        verdict_id = "0x" + hashlib.sha256(json.dumps(stable_payload, sort_keys=True).encode()).hexdigest()
    cap = payload.get("capAtomic")
    try:
        cap = int(cap) if cap is not None else None
    except (TypeError, ValueError):
        decision, cap = "ASK_HUMAN", None
    return ScreeningVerdict(
        decision=decision, verdict_id=verdict_id, provider=provider,
        address=address, reason=str(payload.get("reason") or f"screening decision: {decision}"),
        cap_atomic=cap, checked_at=int(now if now is not None else datetime.now(timezone.utc).timestamp()),
        expires_at=int(payload["expiresAt"]) if payload.get("expiresAt") else None,
    )


def enforce_verdict(verdict: ScreeningVerdict, amount_atomic: int, *, now: int | None = None) -> None:
    current = int(now if now is not None else datetime.now(timezone.utc).timestamp())
    if verdict.expires_at is not None and current >= verdict.expires_at:
        raise ScreeningBlocked("screening verdict expired")
    if verdict.decision == "REFUSE":
        raise ScreeningBlocked(verdict.reason)
    if verdict.decision == "ASK_HUMAN":
        raise ScreeningBlocked("screening requires explicit human review")
    if verdict.decision == "CAP" and verdict.cap_atomic is None:
        raise ScreeningBlocked("screening cap is missing")
    if verdict.decision == "CAP" and int(amount_atomic) > verdict.cap_atomic:
        raise ScreeningBlocked("action exceeds the screening cap")
    if verdict.decision not in {"PAY", "CAP"}:
        raise ScreeningBlocked("screening did not produce a payable decision")
