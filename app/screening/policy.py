"""
policy.py - fail-closed screening policy (docs/decisions/0001-custody-chain.md §4).

Turns Intercepta scan results into findings, then into one verdict:

    REFUSE > ASK_HUMAN > CAP > PAY      (the most severe finding wins)

Address scans (Quick / Deep Scan): the traits drive the decision. The vendor
documents the trait names but publishes no toxicScore threshold, so the score
is a secondary signal with thresholds we chose (env-tunable):
    toxicScore >= SCREENING_REFUSE_SCORE (80) -> REFUSE
    toxicScore >= SCREENING_ASK_SCORE    (60) -> ASK_HUMAN
    toxicScore >= SCREENING_CAP_SCORE    (30) -> CAP at SCREENING_CAP_USDC (10)
Token scans follow the vendor's recommended `action`: block -> REFUSE,
warn -> ASK_HUMAN, info -> no finding. Signature scans (Scan Message) map
riskGroup High -> ASK_HUMAN, Medium -> CAP, Low -> none; drainer / malicious
detectors -> REFUSE. Anything we do not recognise (a new trait, action or
riskGroup) asks a human rather than paying.

Errors never pay: fail_closed() findings are REFUSE with fail_closed=true.
enforce_verdict() is the gate a money-moving executor calls right before
sending.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Optional

PAY, CAP, ASK_HUMAN, REFUSE = "PAY", "CAP", "ASK_HUMAN", "REFUSE"
VERDICTS = (PAY, CAP, ASK_HUMAN, REFUSE)
_SEVERITY = {v: i for i, v in enumerate(VERDICTS)}

# Documented Quick/Deep Scan trait names (ToxicScoreTraitV2.name).
REFUSE_TRAITS = frozenset({
    "sanction_address", "known_scammer", "initiator_scam_transactions",
    "fake_phishing_transfer", "fake_phishing_contract_communication", "blacklist", "rug_pull",
})
ASK_TRAITS = frozenset({
    "sanction_address_communication", "mixer_transfers", "non_kyc_transfers",
    "suspicious_deployer", "suspicious_dex_pair_deployer", "rug_pull_trader",
    "attack_money_target", "zero_address_risk",
})

# Scan Message detector codes that refuse outright (drainers, known bad actors).
REFUSE_SIGNATURE_DETECTORS = frozenset({
    "WALLET_DRAINER", "KNOWN_MALICIOUS", "SCAM_ADDRESS", "BLOCKLIST_SITE",
    "POISONING_ATTACK", "INITIATOR_SCAM_TRANSACTIONS",
})
RISK_GROUPS = {"low": None, "medium": CAP, "high": ASK_HUMAN}
TOKEN_ACTIONS = {"block": REFUSE, "warn": ASK_HUMAN, "info": None}


class ScreeningBlocked(RuntimeError):
    """enforce_verdict() refused to let a payment through."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Thresholds:
    refuse_score: int = 80
    ask_score: int = 60
    cap_score: int = 30
    cap_micro: int = 10_000_000

    @classmethod
    def from_env(cls) -> "Thresholds":
        """Read SCREENING_*; a malformed value raises ValueError (callers fail closed)."""
        def num(name, default):
            raw = os.environ.get(name, "").strip()
            return int(raw) if raw else default
        cap_usdc = os.environ.get("SCREENING_CAP_USDC", "").strip()
        th = cls(refuse_score=num("SCREENING_REFUSE_SCORE", cls.refuse_score),
                 ask_score=num("SCREENING_ASK_SCORE", cls.ask_score),
                 cap_score=num("SCREENING_CAP_SCORE", cls.cap_score),
                 cap_micro=round(float(cap_usdc) * 1_000_000) if cap_usdc else cls.cap_micro)
        if not th.cap_score <= th.ask_score <= th.refuse_score or th.cap_micro < 0:
            raise ValueError("screening thresholds must satisfy CAP <= ASK <= REFUSE")
        return th


@dataclass(frozen=True)
class Finding:
    verdict: str
    code: str
    message: str
    source: str
    fail_closed: bool = False

    def reason(self) -> dict:
        return {"code": self.code, "message": self.message, "source": self.source}


def fail_closed(code: str, message: str, source: str) -> Finding:
    return Finding(REFUSE, code, message, source, fail_closed=True)


# ── per-scan assessment ──────────────────────────────────────────────────────

def assess_address_scan(body: dict, source: str, th: Thresholds) -> list[Finding]:
    findings = []
    for trait in body["traits"]:
        name = trait["name"]
        label = trait.get("description") or name
        if name in REFUSE_TRAITS:
            findings.append(Finding(REFUSE, f"TRAIT_{name.upper()}", label, source))
        elif name in ASK_TRAITS:
            findings.append(Finding(ASK_HUMAN, f"TRAIT_{name.upper()}", label, source))
        else:
            findings.append(Finding(ASK_HUMAN, "TRAIT_UNRECOGNISED",
                                    f"Unrecognised trait {name!r}", source))
    score = body["toxicScore"]
    if score >= th.refuse_score:
        findings.append(Finding(REFUSE, "TOXIC_SCORE_CRITICAL",
                                f"Toxic score {score:g} ≥ {th.refuse_score}", source))
    elif score >= th.ask_score:
        findings.append(Finding(ASK_HUMAN, "TOXIC_SCORE_HIGH",
                                f"Toxic score {score:g} ≥ {th.ask_score}", source))
    elif score >= th.cap_score:
        findings.append(Finding(CAP, "TOXIC_SCORE_ELEVATED",
                                f"Toxic score {score:g} ≥ {th.cap_score}", source))
    return findings


def assess_token_scan(body: dict, source: str = "scan-token") -> list[Finding]:
    action = body["action"].lower()
    symbol = (body.get("token") or {}).get("symbol") or "token"
    codes = ", ".join(d.get("code", "?") for d in body.get("detectors", []) if isinstance(d, dict))
    detail = f" ({codes})" if codes else ""
    if action not in TOKEN_ACTIONS:
        return [Finding(ASK_HUMAN, "TOKEN_ACTION_UNRECOGNISED",
                        f"Unrecognised token action {body['action']!r} for {symbol}", source)]
    verdict = TOKEN_ACTIONS[action]
    if verdict is None:
        return []
    return [Finding(verdict, f"TOKEN_{action.upper()}",
                    f"{symbol}: provider recommends {action}{detail}", source)]


def assess_signature_scan(body: dict, source: str = "scan-message") -> list[Finding]:
    findings = []
    codes = {d.get("code") for d in body["detectors"] if isinstance(d, dict)}
    for entry in body["addresses"]:
        if isinstance(entry, dict):
            codes.update(c for c in entry.get("detectors") or [] if isinstance(c, str))
    for code in sorted(c for c in codes if c in REFUSE_SIGNATURE_DETECTORS):
        findings.append(Finding(REFUSE, f"SIGNATURE_{code}", f"Signature detector {code}", source))
    group = body["riskGroup"]
    if group.lower() not in RISK_GROUPS:
        findings.append(Finding(ASK_HUMAN, "RISK_GROUP_UNRECOGNISED",
                                f"Unrecognised signature risk group {group!r}", source))
    elif RISK_GROUPS[group.lower()] is not None:
        findings.append(Finding(RISK_GROUPS[group.lower()], f"RISK_GROUP_{group.upper()}",
                                f"Signature risk group {group}", source))
    return findings


# ── decision ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Decision:
    verdict: str
    cap_micro: Optional[int]
    reasons: list
    fail_closed: bool


def decide(findings: list[Finding], th: Thresholds) -> Decision:
    """Most severe finding wins; reasons are listed most severe first."""
    ordered = sorted(findings, key=lambda f: -_SEVERITY[f.verdict])
    verdict = ordered[0].verdict if ordered else PAY
    return Decision(verdict=verdict,
                    cap_micro=th.cap_micro if verdict == CAP else None,
                    reasons=[f.reason() for f in ordered],
                    fail_closed=any(f.fail_closed for f in findings))


def enforce_verdict(verdict: dict, amount_micro: int, *, acknowledged: bool = False,
                    now: Optional[int] = None) -> None:
    """Raise ScreeningBlocked unless ``verdict`` lets ``amount_micro`` through.
    ASK_HUMAN passes only when a human acknowledged the warning."""
    current = int(now if now is not None else time.time())
    expires_at = verdict.get("expires_at")
    if expires_at and current >= int(expires_at):
        raise ScreeningBlocked("SCREENING_EXPIRED", "screening verdict expired")
    decision = verdict.get("verdict")
    if decision == REFUSE:
        first = (verdict.get("reasons") or [{}])[0]
        raise ScreeningBlocked("SCREENING_REFUSED", first.get("message") or "screening refused")
    if decision == ASK_HUMAN and not acknowledged:
        raise ScreeningBlocked("SCREENING_ASK_HUMAN", "screening requires explicit human review")
    if decision == CAP:
        cap = verdict.get("cap_micro")
        if cap is None:
            raise ScreeningBlocked("SCREENING_CAP_MISSING", "screening cap is missing")
        if int(amount_micro) > int(cap):
            raise ScreeningBlocked("SCREENING_CAP_EXCEEDED", "amount exceeds the screening cap")
    if decision not in VERDICTS:
        raise ScreeningBlocked("SCREENING_INVALID", "screening did not produce a payable verdict")
