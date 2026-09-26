"""
x402.py — HTTP 402 Payment Required protocol implementation.

The x402 protocol revives the long-dormant HTTP 402 status code for
machine-to-machine stablecoin payments:

  Client → GET /resource                       (no payment)
  Server ← HTTP 402 Payment Required
           X-Payment-Challenge: <json>          (price + permit template)

  Client → GET /resource
           X-Payment: <signed EIP-3009 permit>
  Server ← HTTP 200 OK
           X-Payment-Receipt: <tx hash + explorer link>
           <actual response body>

Without this, our app merely *has* an x402 endpoint; with this, our app
*implements* the protocol — any x402-aware client auto-discovers our paid
endpoints and negotiates payment.

Usage:
    from chain.x402 import require_x402

    @app.route("/api/agents/<int:agent_id>/execute")
    @require_x402(price_per_call_usdc=0.01, recipient_resolver=lambda req, kw: escrow_addr)
    def paid_endpoint(agent_id):
        return {"result": "done"}

If the caller attaches a valid X-Payment header, the decorator:
  1. Parses the permit
  2. Validates signature + nonce + expiry
  3. Executes USDC.transferWithAuthorization (facilitator pays gas)
  4. Injects an X-Payment-Receipt response header with the tx hash
  5. Calls the wrapped function and returns its body with 200

Otherwise it returns 402 with a challenge header describing what's expected.

Note: this is the homegrown `x402/eip-3009` format inherited from AgentHire.
Moving to the current x402 spec (`exact` scheme, CAIP-2 network ids) with the
official SDK is on the roadmap.
"""
from __future__ import annotations
import json
import time
from functools import wraps
from typing import Callable

from flask import request, jsonify, g


X402_SCHEME = "x402/eip-3009"
X402_VERSION = "1"


def _rand_nonce() -> str:
    """32-byte random nonce for the EIP-3009 permit."""
    import secrets
    return "0x" + secrets.token_hex(32)


def build_challenge(price_usdc: float, recipient: str, resource_id: str,
                    *, valid_seconds: int = 3600, usdc_address: str = "",
                    chain=None, domain: dict | None = None, notes: str = "") -> dict:
    """Construct the X-Payment-Challenge body for the active chain."""
    from chain.config import get_chain_config
    chain = chain or get_chain_config()
    chain_id = chain.chain_id
    if domain is None:
        from chain.usdc import fallback_domain
        domain = fallback_domain(usdc_address or None, chain_id)
    now = int(time.time())
    value_micro = int(price_usdc * 1_000_000)
    return {
        "scheme": X402_SCHEME,
        "version": X402_VERSION,
        "resourceId": resource_id,
        "chain": {
            "chainId": chain_id,
            "network": chain.caip2,
            "name": chain.name,
        },
        "token": {
            "address": usdc_address,
            "symbol": "USDC",
            "decimals": 6,
        },
        "price": {
            "amountUSDC": price_usdc,
            "amountMicro": value_micro,
            "perCall": True,
        },
        "recipient": recipient,
        "permit": {
            "type": "EIP-3009/transferWithAuthorization",
            "domain": {
                "name": domain["name"],
                "version": domain["version"],
                "chainId": chain_id,
                "verifyingContract": usdc_address,
            },
            "template": {
                "from": "<buyer-address>",
                "to": recipient,
                "value": str(value_micro),
                "validAfter": 0,
                "validBefore": now + valid_seconds,
                "nonce": _rand_nonce(),
            },
        },
        "retry": {
            "headerName": "X-Payment",
            "format": "x402/eip-3009+v1",
            "example": "0x<r|s|v><from|to|value|validAfter|validBefore|nonce>",
        },
        "notes": notes or "Sign the EIP-3009 permit, attach as X-Payment, retry.",
    }


def parse_payment_header(header_value: str) -> dict | None:
    """Parse an X-Payment header. Accepts either our JSON format or a
    raw hex blob. Returns the permit dict or None if malformed."""
    if not header_value:
        return None
    header_value = header_value.strip()
    # JSON format (used by our own clients)
    if header_value.startswith("{"):
        try:
            return json.loads(header_value)
        except Exception:
            return None
    # Base64 JSON
    if header_value.startswith("eyJ"):
        try:
            import base64
            return json.loads(base64.b64decode(header_value))
        except Exception:
            return None
    return None


def execute_payment(permit: dict, *, expected_recipient: str | None = None) -> dict:
    """Submit a buyer-signed permit on-chain via the facilitator.

    The EIP-3009 signature covers `to`, so the permit must already name the
    expected recipient; it is never rewritten here.
    """
    if expected_recipient and str(permit.get("to", "")).lower() != expected_recipient.lower():
        return {"ok": False, "error": "permit recipient does not match the challenge"}
    try:
        from chain.client import OnChain
        oc = OnChain.from_env()
    except Exception as e:
        return {"ok": False, "error": f"chain client unavailable: {e}"}
    if not oc.facilitator:
        return {"ok": False, "error": "facilitator not configured"}
    try:
        result = oc.x402_execute({
            "from": permit["from"], "to": permit["to"], "value": permit["value"],
            "validAfter": int(permit.get("validAfter", 0)),
            "validBefore": int(permit["validBefore"]),
            "nonce": permit["nonce"], "v": int(permit["v"]), "r": permit["r"], "s": permit["s"],
            "agentId": int(permit.get("agentId", 0)),
        })
        return {"ok": True, **result}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def require_x402(
    price_per_call_usdc: float,
    resource_id: Callable[..., str] | str,
    *,
    recipient_resolver: Callable[..., str] = None,
    notes: str = "",
):
    """Decorator that gates a Flask route behind an x402 payment.

    Args:
      price_per_call_usdc: what to charge per call, in USDC (e.g. 0.01)
      resource_id: string or callable(req, kwargs) -> string; uniquely
                   identifies what's being paid for (e.g. f"agent-{id}").
      recipient_resolver: callable(req, kwargs) -> 0x address; where the
                          USDC should go. Defaults to PAYMENT_RECIPIENT.
      notes: human-readable message for the 402 challenge body.
    """
    def wrap(view):
        @wraps(view)
        def inner(*args, **kwargs):
            from chain.config import get_address, get_chain_config, payment_recipient
            from chain.usdc import get_usdc_domain
            chain = get_chain_config()
            usdc = get_address("USDC") or ""
            default_recipient = payment_recipient() or ""
            recipient = default_recipient
            if recipient_resolver:
                try:
                    recipient = recipient_resolver(request, kwargs) or default_recipient
                except Exception:
                    pass
            rid = resource_id(request, kwargs) if callable(resource_id) else resource_id

            payment_header = request.headers.get("X-Payment") or request.headers.get("x-payment")
            if not payment_header:
                # No payment — issue the challenge
                challenge = build_challenge(
                    price_usdc=price_per_call_usdc,
                    recipient=recipient,
                    resource_id=rid,
                    usdc_address=usdc,
                    chain=chain,
                    domain=get_usdc_domain(),
                    notes=notes,
                )
                resp = jsonify({
                    "error": "Payment required",
                    "scheme": X402_SCHEME,
                    "challenge": challenge,
                })
                resp.status_code = 402
                resp.headers["X-Payment-Challenge"] = json.dumps(challenge)
                resp.headers["WWW-Authenticate"] = f'{X402_SCHEME} price={price_per_call_usdc} usdc={usdc} chain={chain.caip2}'
                return resp

            # Payment attached — parse + execute
            permit = parse_payment_header(payment_header)
            if not permit:
                return jsonify({"error": "malformed X-Payment header"}), 400

            # Required permit fields
            required = ("from", "to", "value", "validBefore", "nonce", "v", "r", "s")
            missing = [f for f in required if f not in permit]
            if missing:
                return jsonify({"error": f"permit missing fields: {missing}"}), 400

            if not recipient:
                return jsonify({"error": "payment recipient not configured"}), 503
            receipt = execute_payment(permit, expected_recipient=recipient)
            if not receipt.get("ok"):
                # Payment attempt failed — refuse the service
                resp = jsonify({"error": "payment failed", "detail": receipt.get("error")})
                resp.status_code = 402  # retry possible with a new permit
                return resp

            # Attach receipt to the environment so the view can see it if it wants
            g.x402_receipt = receipt

            # Call the view
            result = view(*args, **kwargs)

            # Attach receipt to response headers
            if hasattr(result, "headers"):
                result.headers["X-Payment-Receipt"] = json.dumps({
                    "sessionId": receipt.get("sessionId"),
                    "txHashes": receipt.get("txHashes"),
                    "explorer": receipt.get("explorer"),
                })
            return result
        return inner
    return wrap
