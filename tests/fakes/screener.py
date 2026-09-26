"""Fake payee screener: returns a configurable verdict per address, in the
verdict shape of docs/decisions/0001-custody-chain.md §4, and records calls."""
from __future__ import annotations

import time

from app.common.ids import new_id

_SCORES = {"PAY": 5, "CAP": 45, "ASK_HUMAN": 70, "REFUSE": 90}


class FakeScreener:
    def __init__(self, default: str = "PAY", cap_micro: int = 10_000_000):
        self.default = default
        self.cap_micro = cap_micro
        self.verdicts: dict[str, dict] = {}   # lowercase address → overrides
        self.calls: list[dict] = []

    def set(self, address: str, verdict: str, **overrides) -> None:
        """Make ``address`` screen as ``verdict``; overrides patch the result
        (e.g. ``fail_closed=True``, ``reasons=[…]``, ``toxic_score=72``)."""
        self.verdicts[address.lower()] = {"verdict": verdict, **overrides}

    def screen(self, hop, *, chain_address, amount_micro, engagement_id=None,
               agent_id=None, typed_data=None) -> dict:
        self.calls.append({"hop": hop, "chain_address": chain_address, "amount_micro": amount_micro,
                           "engagement_id": engagement_id, "agent_id": agent_id,
                           "typed_data": typed_data})
        override = dict(self.verdicts.get(chain_address.lower(), {"verdict": self.default}))
        verdict = override.pop("verdict")
        now = int(time.time())
        result = {
            "id": new_id("SCR"), "hop": hop,
            "subject": {"chain_address": chain_address.lower(), "screened_address": chain_address.lower(),
                        "network": "eip155:1", "agent_id": agent_id},
            "verdict": verdict,
            "cap_micro": self.cap_micro if verdict == "CAP" else None,
            "reasons": [] if verdict == "PAY" else [
                {"code": f"FAKE_{verdict}", "message": f"configured {verdict}", "source": "fake"}],
            "signals": {"toxic_score": _SCORES.get(verdict, 0), "traits": [], "risk_group": None,
                        "token_risks": []},
            "provider": "fake", "fail_closed": False, "latency_ms": 0,
            "created_at": now, "expires_at": now + 300,
        }
        result.update(override)
        return result
