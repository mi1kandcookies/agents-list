"""Deterministic policy checks shared by the buyer and resource server."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from chain.ens_v2 import AgentResolution, ENSResolutionError, resolve_authorized_agent
from chain.intercepta import ScreeningResult, quick_scan_address


class PaymentPolicyError(RuntimeError):
    def __init__(self, reason: str, code: str = "POLICY_HOLD"):
        super().__init__(reason)
        self.code = code


def canonical_json(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_hex(value: str | bytes) -> str:
    raw = value.encode() if isinstance(value, str) else value
    return "0x" + hashlib.sha256(raw).hexdigest()


def task_hash(task: str) -> str:
    return sha256_hex(task)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def check_snapshot(intent, resolved: AgentResolution) -> None:
    frozen = json.loads(intent.ens_snapshot or "{}")
    current = resolved.snapshot()
    critical = ("name", "address", "endpoint", "enabled", "resolver")
    if any(frozen.get(k) != current.get(k) for k in critical):
        raise PaymentPolicyError("ENS authorization-critical record changed after approval",
                                 "ENS_CHANGED")


def screen_and_validate(intent) -> ScreeningResult:
    try:
        resolved = resolve_authorized_agent(intent.specialist_name)
    except ENSResolutionError as exc:
        raise PaymentPolicyError(str(exc), "ENS_UNAVAILABLE") from exc
    check_snapshot(intent, resolved)
    screening = quick_scan_address(intent.pay_to)
    if screening.decision == "DENY":
        raise PaymentPolicyError(screening.reason, "INTERCEPTA_DENY")
    if screening.decision != "ALLOW":
        raise PaymentPolicyError(screening.reason, "INTERCEPTA_HOLD")
    return screening


def expected_terms(intent) -> dict:
    return {
        "scheme": "exact",
        "network": intent.network,
        "chainId": int(intent.chain_id),
        "asset": intent.token_address,
        "payTo": intent.pay_to,
        "amount": str(intent.amount_atomic),
        "domain": {"name": "USDC", "version": "2"},
    }


def validate_requirements(intent, requirements) -> None:
    fields = {
        "scheme": getattr(requirements, "scheme", None),
        "network": getattr(requirements, "network", None),
        "asset": getattr(requirements, "asset", None),
        "payTo": getattr(requirements, "pay_to", getattr(requirements, "payTo", None)),
        "amount": str(getattr(requirements, "amount", "")),
    }
    expected = expected_terms(intent)
    for key in ("scheme", "network", "asset", "payTo", "amount"):
        if str(fields[key]).lower() != str(expected[key]).lower():
            raise PaymentPolicyError(f"payment {key} does not match the approved intent",
                                     "PAYMENT_TERMS_CHANGED")
    extra = getattr(requirements, "extra", {}) or {}
    if extra.get("name") not in (None, "USDC") or extra.get("version") not in (None, "2"):
        raise PaymentPolicyError("payment token domain is not the approved USDC domain",
                                 "PAYMENT_DOMAIN_CHANGED")


def validate_payer(intent, payer: str) -> None:
    if str(payer or "").lower() != intent.payer.lower():
        raise PaymentPolicyError("payment signer is not the approved payer", "PAYER_CHANGED")
