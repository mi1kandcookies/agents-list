"""Intercepta destination screening adapter.

The provider URL and decision field are configuration, not guessed constants.
An unavailable provider or an unrecognised response is always HOLD.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass

import requests


@dataclass(frozen=True)
class ScreeningResult:
    decision: str
    reason: str
    provider: str
    address: str
    checked_at: int
    evidence: dict
    verdict_id: str = ""

    def to_dict(self) -> dict:
        data = asdict(self)
        data["verdictId"] = data.pop("verdict_id")
        data["checkedAt"] = data.pop("checked_at")
        return data


def _result(decision: str, reason: str, address: str, evidence=None) -> ScreeningResult:
    evidence = evidence or {}
    stable = json.dumps({"address": address.lower(), "decision": decision,
                         "evidence": evidence}, sort_keys=True, separators=(",", ":"))
    verdict_id = "0x" + hashlib.sha256(stable.encode()).hexdigest()
    return ScreeningResult(decision, reason, "intercepta", address, int(time.time()), evidence, verdict_id)


def _path_value(payload, path: str):
    current = payload
    for part in (p for p in path.split(".") if p):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _normalise_decision(value) -> str | None:
    value = str(value or "").strip().upper()
    return value if value in {"ALLOW", "HOLD", "DENY"} else None


def quick_scan_address(address: str) -> ScreeningResult:
    mode = os.environ.get("INTERCEPTA_MODE", "live").strip().lower()
    if mode == "fixture":
        deny = os.environ.get("INTERCEPTA_DENY_ADDRESS", "").strip().lower()
        if deny and address.lower() == deny:
            return _result("DENY", "fixture marks the destination as prohibited", address,
                           {"fixture": True, "riskAddress": True})
        decision = _normalise_decision(os.environ.get("INTERCEPTA_FIXTURE_DECISION", "ALLOW"))
        if not decision:
            return _result("HOLD", "fixture decision is invalid", address, {"fixture": True})
        return _result(decision, f"fixture decision: {decision.lower()}", address,
                       {"fixture": True, "decision": decision})

    url = os.environ.get("INTERCEPTA_QUICK_SCAN_URL", "").strip()
    api_key = os.environ.get("INTERCEPTA_API_KEY", "").strip()
    if not url or not api_key:
        return _result("HOLD", "Intercepta Quick Scan Address is not configured", address)
    try:
        response = requests.post(
            url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"address": address},
            timeout=float(os.environ.get("INTERCEPTA_TIMEOUT_SECONDS", "8")),
        )
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        return _result("HOLD", f"Intercepta request failed: {str(exc)[:120]}", address)
    if response.status_code >= 400:
        return _result("HOLD", f"Intercepta returned HTTP {response.status_code}", address,
                       {"httpStatus": response.status_code})
    path = os.environ.get("INTERCEPTA_DECISION_PATH", "decision")
    decision = _normalise_decision(_path_value(payload, path))
    if not decision:
        return _result("HOLD", "Intercepta response did not contain a configured decision", address,
                       {"decisionPath": path, "response": payload})
    reason = str(_path_value(payload, os.environ.get("INTERCEPTA_REASON_PATH", "reason")) or
                 f"Intercepta decision: {decision.lower()}")[:500]
    return _result(decision, reason, address, {"decisionPath": path, "provider": payload})
