"""
chain/x402_v2.py - x402 v2 ``exact`` scheme on Ethereum Sepolia with Circle USDC.

    from chain import x402_v2

    req = x402_v2.build_requirements(pay_to=payee, amount_micro=50_000)
    body = x402_v2.payment_required(req, resource_url="/api/agents/AGT-…/tasks")
    # → HTTP 402, JSON body, and header PAYMENT-REQUIRED: encode_header(body)

    # Paying side (an agent with its own key):
    payload = x402_v2.sign_payment(account, req, expected=x402_v2.Expectation(
        pay_to=payee, amount_micro=50_000))
    # → retry with header X-PAYMENT: encode_header(payload)

    # Resource side:
    verified = x402_v2.verify_payment(decode_header(value), req)
    tx = x402_v2.settle(verified, escrow=escrow_service, ref=verified.nonce)

Wire format (x402 v2, exact scheme for EVM):

    PaymentRequirements {scheme:"exact", network:"eip155:11155111", amount:"<atomic>",
                         asset:<USDC>, payTo, maxTimeoutSeconds, extra:{name, version}}
    PaymentRequired     {x402Version:2, error, resource:{url, description, mimeType}, accepts:[…]}
    PaymentPayload      {x402Version:2, resource, accepted:<requirements>,
                         payload:{signature, authorization:{from, to, value, validAfter,
                                                            validBefore, nonce}}}
    SettleResponse      {success, transaction, network, payer}

Headers are base64 (standard alphabet) of the compact JSON. ``X-PAYMENT`` and
``PAYMENT-SIGNATURE`` carry the payload; ``PAYMENT-REQUIRED`` and
``PAYMENT-RESPONSE`` / ``X-PAYMENT-RESPONSE`` carry the other two.

Both sides check the same things. Before signing, ``preflight`` compares the
requirements with what the payer expects (recipient, amount, asset, network,
validity window) and with the EIP-712 domain read from the token contract
(chain/usdc.py), and ``GuardedSigner`` re-checks the exact typed data it is
about to sign. On receipt, ``verify_payment`` recovers the signer against the
server's own domain. Settlement goes through the escrow service's facilitator
path (simulated without keys).

No Flask imports.
"""
from __future__ import annotations

import base64
import binascii
import json
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Callable

X402_VERSION = 2
SCHEME = "exact"
SEPOLIA_NETWORK = "eip155:11155111"
SEPOLIA_CHAIN_ID = 11155111
DEFAULT_TIMEOUT_SECONDS = 300
MAX_TIMEOUT_SECONDS = 3600
CLOCK_SKEW_SECONDS = 30
MAX_HEADER_BYTES = 8192

PAYMENT_HEADERS = ("X-PAYMENT", "PAYMENT-SIGNATURE")
REQUIRED_HEADER = "PAYMENT-REQUIRED"
RESPONSE_HEADERS = ("PAYMENT-RESPONSE", "X-PAYMENT-RESPONSE")

_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_NONCE_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
_SIG_RE = re.compile(r"^0x[0-9a-fA-F]{130}$")
_UINT_RE = re.compile(r"^[0-9]{1,78}$")


class X402Error(Exception):
    """A refused payment. ``code`` is machine-readable."""

    def __init__(self, code: str, message: str | None = None):
        super().__init__(message or code)
        self.code = code
        self.message = message or code


# ── requirements ────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class PaymentRequirements:
    network: str
    amount: str                  # atomic units (micro-USDC) as a decimal string
    asset: str                   # token contract
    pay_to: str
    max_timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    extra: dict = field(default_factory=dict)   # EIP-712 domain {name, version}
    scheme: str = SCHEME

    @property
    def amount_micro(self) -> int:
        return int(self.amount)

    def to_dict(self) -> dict:
        return {"scheme": self.scheme, "network": self.network, "amount": self.amount,
                "asset": self.asset, "payTo": self.pay_to,
                "maxTimeoutSeconds": self.max_timeout_seconds, "extra": dict(self.extra)}

    @classmethod
    def from_dict(cls, data) -> "PaymentRequirements":
        if not isinstance(data, dict):
            raise X402Error("MALFORMED_PAYMENT", "requirements must be an object")
        try:
            timeout = data.get("maxTimeoutSeconds", DEFAULT_TIMEOUT_SECONDS)
            if isinstance(timeout, bool) or not isinstance(timeout, int):
                raise TypeError
            return cls(scheme=str(data["scheme"]), network=str(data["network"]),
                       amount=str(data["amount"]), asset=str(data["asset"]),
                       pay_to=str(data["payTo"]), max_timeout_seconds=timeout,
                       extra=dict(data.get("extra") or {}))
        except (KeyError, TypeError, ValueError):
            raise X402Error("MALFORMED_PAYMENT", "requirements are incomplete") from None


def _domain(domain: dict | None) -> dict:
    if domain is not None:
        return domain
    from chain.usdc import get_usdc_domain
    return get_usdc_domain()


def build_requirements(*, pay_to: str, amount_micro: int,
                       max_timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
                       domain: dict | None = None, chain=None,
                       asset: str | None = None) -> PaymentRequirements:
    """Requirements for paying ``amount_micro`` USDC to ``pay_to`` on Sepolia.
    ``domain`` defaults to the one read from the token contract."""
    from chain.config import get_address, get_chain_config
    chain = chain or get_chain_config()
    if chain.chain_id != SEPOLIA_CHAIN_ID:
        raise X402Error("NETWORK_UNSUPPORTED", "x402 payments run on Ethereum Sepolia only")
    asset = asset or get_address("USDC")
    if not asset or not _ADDRESS_RE.match(asset):
        raise X402Error("NOT_CONFIGURED", "USDC_ADDRESS is not configured")
    if not isinstance(pay_to, str) or not _ADDRESS_RE.match(pay_to):
        raise X402Error("INVALID_PAYEE", "payTo must be a 0x-prefixed 20-byte address")
    if isinstance(amount_micro, bool) or not isinstance(amount_micro, int) or amount_micro <= 0:
        raise X402Error("INVALID_AMOUNT", "amount must be a positive integer (micro-USDC)")
    if not 0 < int(max_timeout_seconds) <= MAX_TIMEOUT_SECONDS:
        raise X402Error("INVALID_TIMEOUT", "maxTimeoutSeconds is out of range")
    d = _domain(domain)
    return PaymentRequirements(network=chain.caip2, amount=str(amount_micro), asset=asset,
                               pay_to=pay_to.lower(), max_timeout_seconds=int(max_timeout_seconds),
                               extra={"name": d["name"], "version": d["version"]})


def payment_required(requirements: PaymentRequirements, *, resource_url: str,
                     description: str = "", mime_type: str = "application/json",
                     error: str = "X-PAYMENT header is required") -> dict:
    return {"x402Version": X402_VERSION, "error": error,
            "resource": {"url": resource_url, "description": description, "mimeType": mime_type},
            "accepts": [requirements.to_dict()]}


# ── header encoding ─────────────────────────────────────────────────────────
def encode_header(obj: dict) -> str:
    """base64 of the compact, key-sorted JSON."""
    raw = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return base64.b64encode(raw.encode("ascii")).decode("ascii")


def decode_header(value: str) -> dict:
    """Inverse of ``encode_header``; also accepts the URL-safe alphabet and
    missing padding. Raises X402Error MALFORMED_PAYMENT."""
    if not isinstance(value, str) or not value.strip():
        raise X402Error("MALFORMED_PAYMENT", "payment header is empty")
    value = value.strip()
    if len(value) > MAX_HEADER_BYTES:
        raise X402Error("MALFORMED_PAYMENT", "payment header is too large")
    padded = value + "=" * (-len(value) % 4)
    try:
        raw = base64.b64decode(padded.replace("-", "+").replace("_", "/"), validate=True)
        obj = json.loads(raw.decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise X402Error("MALFORMED_PAYMENT", "payment header is not base64 JSON") from None
    if not isinstance(obj, dict):
        raise X402Error("MALFORMED_PAYMENT", "payment header must encode an object")
    return obj


# ── paying side: preflight + guarded signer ─────────────────────────────────
@dataclass(frozen=True)
class Expectation:
    """What the payer agreed to pay, independent of what the server asks for."""
    pay_to: str
    amount_micro: int
    asset: str | None = None            # default: configured USDC
    network: str = SEPOLIA_NETWORK
    max_timeout_seconds: int = MAX_TIMEOUT_SECONDS


def preflight(requirements: PaymentRequirements, expected: Expectation, *,
              domain: dict | None = None) -> dict:
    """Refuse to sign requirements that differ from ``expected`` or whose
    EIP-712 domain differs from the token contract's. Returns the domain to
    sign against."""
    from chain.config import get_address
    if requirements.scheme != SCHEME:
        raise X402Error("SCHEME_MISMATCH", f"scheme {requirements.scheme!r} is not 'exact'")
    if requirements.network != expected.network or expected.network != SEPOLIA_NETWORK:
        raise X402Error("NETWORK_MISMATCH", f"network {requirements.network!r} is not {SEPOLIA_NETWORK}")
    asset = expected.asset or get_address("USDC") or ""
    if requirements.asset.lower() != asset.lower():
        raise X402Error("ASSET_MISMATCH", "payment asset is not the configured USDC contract")
    if requirements.pay_to.lower() != expected.pay_to.lower():
        raise X402Error("RECIPIENT_MISMATCH", "payTo is not the expected recipient")
    if not _UINT_RE.match(requirements.amount) or int(requirements.amount) != expected.amount_micro:
        raise X402Error("AMOUNT_MISMATCH", "amount is not the expected amount")
    if not 0 < requirements.max_timeout_seconds <= expected.max_timeout_seconds:
        raise X402Error("VALIDITY_WINDOW", "maxTimeoutSeconds is outside the allowed window")
    d = _domain(domain)
    extra = requirements.extra or {}
    if extra.get("name") != d["name"] or extra.get("version") != d["version"]:
        raise X402Error("DOMAIN_MISMATCH",
                        "EIP-712 name/version differ from the token contract's")
    if str(d.get("verifyingContract") or "").lower() != asset.lower():
        raise X402Error("DOMAIN_MISMATCH", "EIP-712 verifyingContract is not the payment asset")
    if int(d.get("chainId") or 0) != SEPOLIA_CHAIN_ID:
        raise X402Error("DOMAIN_MISMATCH", "EIP-712 chainId is not Sepolia")
    return d


class GuardedSigner:
    """Wraps an eth_account ``LocalAccount``; signs only a
    TransferWithAuthorization that matches the expectation exactly."""

    def __init__(self, account, expected: Expectation, domain: dict):
        self._account = account
        self._expected = expected
        self._domain = domain

    @property
    def address(self) -> str:
        return self._account.address

    def sign_typed_data(self, full_message: dict, *, now: int | None = None):
        from eth_account.messages import encode_typed_data
        now = int(time.time()) if now is None else now
        exp, dom = self._expected, self._domain
        if full_message.get("primaryType") != "TransferWithAuthorization":
            raise X402Error("SIGNER_REFUSED", "unexpected typed-data primary type")
        d = full_message.get("domain") or {}
        if (d.get("name") != dom["name"] or d.get("version") != dom["version"]
                or int(d.get("chainId") or 0) != SEPOLIA_CHAIN_ID
                or str(d.get("verifyingContract") or "").lower()
                != str(dom["verifyingContract"]).lower()):
            raise X402Error("SIGNER_REFUSED", "unexpected EIP-712 domain")
        m = full_message.get("message") or {}
        if str(m.get("from", "")).lower() != self.address.lower():
            raise X402Error("SIGNER_REFUSED", "authorization is not from this signer")
        if str(m.get("to", "")).lower() != exp.pay_to.lower():
            raise X402Error("SIGNER_REFUSED", "unexpected recipient")
        if int(m.get("value", -1)) != exp.amount_micro:
            raise X402Error("SIGNER_REFUSED", "unexpected amount")
        valid_after, valid_before = int(m.get("validAfter", -1)), int(m.get("validBefore", 0))
        if valid_after < 0 or valid_after > now or valid_before <= now \
                or valid_before - now > exp.max_timeout_seconds:
            raise X402Error("SIGNER_REFUSED", "authorization validity window is out of bounds")
        return self._account.sign_message(encode_typed_data(full_message=full_message))


def sign_payment(account, requirements: PaymentRequirements, *, expected: Expectation,
                 domain: dict | None = None, now: int | None = None,
                 nonce: str | None = None, resource: dict | None = None,
                 before_sign: Callable[[dict], object] | None = None) -> dict:
    """Preflight, run the optional risk gate, then sign the exact payload.

    ``before_sign`` receives the exact EIP-712 typed data that will be signed.
    A screening or approval exception from the hook aborts before the account
    signer is called.
    """
    from chain.usdc import authorization_typed_data
    d = preflight(requirements, expected, domain=domain)
    now = int(time.time()) if now is None else now
    authorization = {
        "from": account.address,
        "to": requirements.pay_to,
        "value": requirements.amount,
        "validAfter": "0",
        "validBefore": str(now + requirements.max_timeout_seconds),
        "nonce": nonce or "0x" + secrets.token_hex(32),
    }
    typed = authorization_typed_data(authorization, d)
    if before_sign is not None:
        before_sign(typed)
    signed = GuardedSigner(account, expected, d).sign_typed_data(typed, now=now)
    signature = "0x" + bytes(signed.signature).hex()
    out = {"x402Version": X402_VERSION, "accepted": requirements.to_dict(),
           "payload": {"signature": signature, "authorization": authorization}}
    if resource is not None:
        out["resource"] = resource
    return out


# ── resource side: verify + settle ──────────────────────────────────────────
@dataclass(frozen=True)
class VerifiedPayment:
    payer: str
    pay_to: str
    amount_micro: int
    nonce: str
    network: str
    permit: dict          # EIP-3009 fields + v, r, s, for settlement
    typed_data: dict      # exact EIP-712 payload reconstructed from the permit


def _uint(value, name: str) -> int:
    s = str(value)
    if isinstance(value, bool) or not _UINT_RE.match(s):
        raise X402Error("MALFORMED_PAYMENT", f"{name} must be an unsigned integer")
    return int(s)


def verify_payment(payload: dict, requirements: PaymentRequirements, *,
                   domain: dict | None = None, now: int | None = None) -> VerifiedPayment:
    """Check a decoded PaymentPayload against the server's own requirements
    and the token's EIP-712 domain. Raises X402Error."""
    if payload.get("x402Version") != X402_VERSION:
        raise X402Error("UNSUPPORTED_VERSION", "x402Version must be 2")
    accepted = PaymentRequirements.from_dict(payload.get("accepted"))
    for name in ("scheme", "network", "amount", "max_timeout_seconds"):
        if getattr(accepted, name) != getattr(requirements, name):
            raise X402Error("REQUIREMENTS_MISMATCH", f"accepted {name} does not match")
    for name in ("asset", "pay_to"):
        if getattr(accepted, name).lower() != getattr(requirements, name).lower():
            raise X402Error("REQUIREMENTS_MISMATCH", f"accepted {name} does not match")
    if accepted.extra != requirements.extra:
        raise X402Error("REQUIREMENTS_MISMATCH", "accepted EIP-712 domain does not match")

    body = payload.get("payload")
    auth = body.get("authorization") if isinstance(body, dict) else None
    signature = body.get("signature") if isinstance(body, dict) else None
    if not isinstance(auth, dict) or not isinstance(signature, str) or not _SIG_RE.match(signature):
        raise X402Error("MALFORMED_PAYMENT", "payload needs authorization and a 65-byte signature")
    payer, to, nonce = auth.get("from"), auth.get("to"), auth.get("nonce")
    if not all(isinstance(a, str) and _ADDRESS_RE.match(a) for a in (payer, to)):
        raise X402Error("MALFORMED_PAYMENT", "authorization from/to must be addresses")
    if not isinstance(nonce, str) or not _NONCE_RE.match(nonce):
        raise X402Error("MALFORMED_PAYMENT", "authorization nonce must be 32 bytes of hex")
    value = _uint(auth.get("value"), "value")
    valid_after = _uint(auth.get("validAfter", 0), "validAfter")
    valid_before = _uint(auth.get("validBefore"), "validBefore")

    if to.lower() != requirements.pay_to.lower():
        raise X402Error("RECIPIENT_MISMATCH", "authorization recipient is not payTo")
    if value != requirements.amount_micro:
        raise X402Error("AMOUNT_MISMATCH", "authorization value is not the required amount")
    now = int(time.time()) if now is None else now
    if valid_after > now + CLOCK_SKEW_SECONDS:
        raise X402Error("NOT_YET_VALID", "authorization is not valid yet")
    if valid_before <= now:
        raise X402Error("EXPIRED", "authorization has expired")
    if valid_before - now > requirements.max_timeout_seconds + CLOCK_SKEW_SECONDS:
        raise X402Error("VALIDITY_WINDOW", "authorization is valid for longer than maxTimeoutSeconds")

    from chain.usdc import authorization_typed_data, recover_authorization_signer
    d = dict(_domain(domain))
    if (d.get("name"), d.get("version")) != (requirements.extra.get("name"),
                                              requirements.extra.get("version")):
        raise X402Error("DOMAIN_MISMATCH", "requirements domain is not the token's")
    d["verifyingContract"] = requirements.asset
    d["chainId"] = SEPOLIA_CHAIN_ID
    sig = bytes.fromhex(signature[2:])
    v = sig[64] if sig[64] >= 27 else sig[64] + 27
    permit = {"from": payer, "to": to, "value": str(value), "validAfter": valid_after,
              "validBefore": valid_before, "nonce": nonce.lower(), "v": v,
              "r": "0x" + sig[:32].hex(), "s": "0x" + sig[32:64].hex()}
    typed_data = authorization_typed_data(permit, d)
    try:
        signer = recover_authorization_signer(permit, d)
    except Exception:
        signer = ""
    if signer.lower() != payer.lower():
        raise X402Error("INVALID_SIGNATURE", "signature does not match authorization.from")
    return VerifiedPayment(payer=payer.lower(), pay_to=to.lower(), amount_micro=value,
                           nonce=nonce.lower(), network=requirements.network, permit=permit,
                           typed_data=typed_data)


def settle(verified: VerifiedPayment, *, escrow, ref: str | None = None):
    """Submit the authorization through the escrow service's facilitator path
    (``EscrowService.settle_authorization``). Returns its TxResult."""
    return escrow.settle_authorization(verified.permit, pay_to=verified.pay_to, ref=ref)


def settle_response(verified: VerifiedPayment, tx) -> dict:
    return {"success": True, "transaction": tx.tx_hash, "network": verified.network,
            "payer": verified.payer}
