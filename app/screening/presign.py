"""Payer-side Intercepta gate for x402 signing.

The resource server screens again after receiving a payment, but a paying
agent must screen the exact recipient, token and EIP-712 authorization before
calling its signer. This module has no database side effects so it can run in
the buyer process or inside an x402 client hook.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
import re
from typing import Optional

from app.screening import policy
from app.screening.intercepta import MAINNET_CHAIN_ID, InterceptaClient, InterceptaError
from app.screening.service import MAINNET_USDC, load_address_map, mainnet_typed_data

_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")


class PreSignScreeningError(RuntimeError):
    """The buyer must not sign this payment."""

    def __init__(self, code: str, message: str, *, verdict: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.verdict = verdict


@dataclass(frozen=True)
class PreSignVerdict:
    verdict: str
    cap_micro: Optional[int]
    reasons: list[dict]
    signals: dict
    raw: dict
    latency_ms: int

    def as_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "cap_micro": self.cap_micro,
            "reasons": self.reasons,
            "signals": self.signals,
            "raw": self.raw,
            "provider": "intercepta",
            "latency_ms": self.latency_ms,
        }


def screen_before_sign(*, pay_to: str, amount_micro: int, typed_data: dict,
                       screening_address: str | None = None,
                       address_map: dict[str, str] | None = None,
                       client: InterceptaClient | None = None,
                       payment_token: str | None = None,
                       human_acknowledged: bool = False) -> PreSignVerdict:
    """Run address, token and exact-message scans before an x402 signer.

    ``screening_address`` is the provider-supported mainnet representation of
    the Sepolia payee. It is required unless ``address_map`` supplies one;
    silently screening a Sepolia token/address as mainnet is not allowed.
    """
    started = time.monotonic()
    try:
        mapping = {str(k).lower(): str(v).lower()
                   for k, v in (address_map if address_map is not None else load_address_map()).items()}
    except (ValueError, OSError, TypeError) as exc:
        raise PreSignScreeningError("SCREENING_CONFIG_INVALID",
                                    f"screening address map is invalid: {type(exc).__name__}") from exc
    screened = (screening_address or mapping.get(str(pay_to).lower()) or "").lower()
    if not _ADDRESS.match(str(pay_to)) or not _ADDRESS.match(screened):
        raise PreSignScreeningError(
            "UNMAPPED_ADDRESS",
            "no mainnet screening address is configured for the x402 payee",
        )
    if client is None:
        client = InterceptaClient()
    try:
        thresholds = policy.Thresholds.from_env()
    except (ValueError, TypeError) as exc:
        raise PreSignScreeningError("SCREENING_CONFIG_INVALID",
                                    f"screening thresholds are invalid: {type(exc).__name__}") from exc
    findings: list[policy.Finding] = []
    raw: dict = {}
    signals: dict = {}

    try:
        address = client.quick_scan_address(screened)
        raw["quick-scan"] = address
        signals["toxic_score"] = address["toxicScore"]
        signals["traits"] = [trait["name"] for trait in address["traits"]]
        findings.extend(policy.assess_address_scan(address, "quick-scan", thresholds))

        token = client.scan_token(MAINNET_USDC, chain_id=MAINNET_CHAIN_ID)
        raw["scan-token"] = token
        findings.extend(policy.assess_token_scan(token))

        def resolve(address_value: str) -> str | None:
            value = str(address_value).lower()
            if value == str(pay_to).lower():
                return screened
            return mapping.get(value)

        rebuilt = mainnet_typed_data(
            typed_data, resolve=resolve,
            payment_token=payment_token,
        )
        message = rebuilt.get("message") or {}
        owner = message.get("from") or message.get("owner")
        if not owner:
            raise PreSignScreeningError("INVALID_TYPED_DATA", "payment authorization has no signer")
        signature = client.scan_message(
            owner=owner,
            typed_data=rebuilt,
            chain_id=MAINNET_CHAIN_ID,
        )
        raw["scan-message"] = signature
        signals["risk_group"] = signature["riskGroup"]
        findings.extend(policy.assess_signature_scan(signature))
    except PreSignScreeningError:
        raise
    except InterceptaError as exc:
        raise PreSignScreeningError(exc.code, f"Intercepta: {exc.message}") from exc
    except Exception as exc:
        # Provider errors and malformed/unknown responses must never become a
        # green result. Preserve the provider's exception type in the reason
        # without exposing credentials.
        raise PreSignScreeningError(
            "SCREENING_FAILED", f"Intercepta pre-sign screening failed: {type(exc).__name__}",
        ) from exc

    decision = policy.decide(findings, thresholds)
    result = PreSignVerdict(
        verdict=decision.verdict,
        cap_micro=decision.cap_micro,
        reasons=decision.reasons,
        signals=signals,
        raw=raw,
        latency_ms=int((time.monotonic() - started) * 1000),
    )
    verdict = result.as_dict()
    try:
        policy.enforce_verdict(verdict, amount_micro, acknowledged=human_acknowledged)
    except policy.ScreeningBlocked as exc:
        raise PreSignScreeningError(exc.code, str(exc), verdict=verdict) from None
    return result


class InterceptaPreSignGate:
    """Callable adapter for ``x402_v2.sign_payment(before_sign=...)``."""

    def __init__(self, *, pay_to: str, amount_micro: int, screening_address: str | None = None,
                 address_map: dict[str, str] | None = None,
                 client: InterceptaClient | None = None, payment_token: str | None = None,
                 human_acknowledged: bool = False):
        self.pay_to = pay_to
        self.amount_micro = amount_micro
        self.screening_address = screening_address
        self.address_map = address_map
        self.client = client
        self.payment_token = payment_token
        self.human_acknowledged = human_acknowledged
        self.last_verdict: PreSignVerdict | None = None

    def __call__(self, typed_data: dict) -> PreSignVerdict:
        self.last_verdict = screen_before_sign(
            pay_to=self.pay_to,
            amount_micro=self.amount_micro,
            typed_data=typed_data,
            screening_address=self.screening_address,
            address_map=self.address_map,
            client=self.client,
            payment_token=self.payment_token,
            human_acknowledged=self.human_acknowledged,
        )
        return self.last_verdict
