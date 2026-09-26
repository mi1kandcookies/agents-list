"""Deterministic provider fakes shared by custody-chain tests."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class FakeWorldIdP:
    issuer: str = "https://world.test"
    audience: str = "agents-list-test"

    def issue_id_token(self, *, sub: str, jti: str, auth_time: int, nonce: str,
                       wallet: str = "") -> dict:
        return {
            "iss": self.issuer,
            "aud": self.audience,
            "sub": sub,
            "jti": jti,
            "auth_time": auth_time,
            "nonce": nonce,
            "wallet": wallet,
        }


@dataclass
class FakeScreener:
    decision: str = "PAY"
    verdict_id: str = "fixture-verdict-1"

    def screen(self, address: str, *, action_type: str, amount_atomic: int) -> dict:
        return {
            "decision": self.decision,
            "verdictId": self.verdict_id,
            "address": address,
            "actionType": action_type,
            "amountAtomic": str(amount_atomic),
        }


@dataclass
class FakeEscrowService:
    mode: str = "simulated"

    def fund(self, *, engagement_id: str, milestone_id: str, amount_atomic: int) -> dict:
        return {"status": "confirmed", "action": "fund", "engagementId": engagement_id,
                "milestoneId": milestone_id, "amountAtomic": str(amount_atomic),
                "mode": self.mode}

    def release(self, *, engagement_id: str, milestone_id: str, amount_atomic: int) -> dict:
        return {"status": "confirmed", "action": "release", "engagementId": engagement_id,
                "milestoneId": milestone_id, "amountAtomic": str(amount_atomic),
                "mode": self.mode}
