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
           X-Payment-Receipt: <tx hash + snowtrace>
           <actual response body>

Without this, our app merely *has* an x402 endpoint; with this, our app
*implements* the protocol — any x402-aware client auto-discovers our paid
endpoints and negotiates payment.

Usage:
    from x402 import require_x402

    @app.route("/api/agents/<int:agent_id>/execute")
    @require_x402(price_per_call_usdc=0.01, recipient_resolver=lambda req, kw: escrow_addr)
    def paid_endpoint(agent_id):
        return {"result": "done"}

If the caller attaches a valid X-Payment header, the decorator:
  1. Parses the permit
  2. Validates signature + nonce + expiry
  3. Executes via MockUSDC.transferWithAuthorization (through facilitator)
  4. Injects an X-Payment-Receipt response header with the tx hash
  5. Calls the wrapped function and returns its body with 200

Otherwise it returns 402 with a challenge header describing what's expected.
"""
from __future__ import annotations
import json
import time
import hashlib
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
                    chain_id: int = 43113, notes: str = "") -> dict:
    """Construct the X-Payment-Challenge body."""
    now = int(time.time())
    value_micro = int(price_usdc * 1_000_000)
    return {
        "scheme": X402_SCHEME,
        "version": X402_VERSION,
        "resourceId": resource_id,
        "chain": {
            "chainId": chain_id,
            "name": "Avalanche Fuji" if chain_id == 43113 else f"chain-{chain_id}",
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
                "name": "Mock USDC",
                "version": "1",
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


def execute_payment(permit: dict, *, recipient_override: str = None) -> dict:
    """Submit the permit on-chain via facilitator. Returns receipt.
    Lazy-imports onchain so this module has no hard deps on web3."""
    try:
        from onchain import OnChain
        oc = OnChain.from_env()
    except Exception as e:
        return {"ok": False, "error": f"onchain not configured: {e}"}
    if not oc or not oc.facilitator:
        return {"ok": False, "error": "facilitator not configured"}
    try:
        # Map the permit fields to what oc.x402_execute expects
        p = {
            "from": permit["from"],
            "to": recipient_override or permit["to"],
            "value": permit["value"],
            "validAfter": int(permit.get("validAfter", 0)),
            "validBefore": int(permit["validBefore"]),
            "nonce": permit["nonce"],
            "v": int(permit["v"]),
            "r": permit["r"],
            "s": permit["s"],
            "agentId": int(permit.get("agentId", 0)),
            "tokenBudget": int(permit.get("tokenBudget", permit["value"])),
            "categoryId": int(permit.get("categoryId", 0)),
        }
        result = oc.x402_execute(p)
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
                          USDC should go. Defaults to EscrowPayment.
      notes: human-readable message for the 402 challenge body.
    """
    def wrap(view):
        @wraps(view)
        def inner(*args, **kwargs):
            from onchain import ADDRESSES, CHAIN_ID
            usdc = ADDRESSES.get("MockUSDC", "")
            default_recipient = ADDRESSES.get("EscrowPayment", "")
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
                    chain_id=CHAIN_ID,
                    notes=notes,
                )
                resp = jsonify({
                    "error": "Payment required",
                    "scheme": X402_SCHEME,
                    "challenge": challenge,
                })
                resp.status_code = 402
                resp.headers["X-Payment-Challenge"] = json.dumps(challenge)
                resp.headers["WWW-Authenticate"] = f'{X402_SCHEME} price={price_per_call_usdc} usdc={usdc} chain={CHAIN_ID}'
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

            receipt = execute_payment(permit, recipient_override=recipient)
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
                    "snowtrace": receipt.get("snowtrace"),
                })
            return result
        return inner
    return wrap


# ── Demo mode ────────────────────────────────────────────────────────────────
# For the UI to show the full handshake without requiring a MetaMask signing
# step, we offer a helper that auto-signs a permit using the facilitator's own
# key (facilitator pays itself) so the 3-tx flow still fires for real.

def auto_sign_demo_permit(recipient: str, amount_usdc: float,
                           *, agent_id: int = 0, category_id: int = 0) -> dict:
    """Produce an EIP-3009 permit signed by the facilitator. The 'buyer'
    and 'payer' are the same wallet — only valid in demo contexts.
    Returns a dict matching our X-Payment header format."""
    from onchain import OnChain, ADDRESSES, CHAIN_ID
    from eth_account.messages import encode_typed_data
    from eth_account import Account
    oc = OnChain.from_env()
    if not oc.facilitator:
        raise RuntimeError("facilitator key not set")

    value = int(amount_usdc * 1_000_000)
    valid_before = int(time.time()) + 3600
    nonce_bytes = __import__("secrets").token_bytes(32)

    typed_data = {
        "types": {
            "EIP712Domain": [
                {"name":"name","type":"string"},
                {"name":"version","type":"string"},
                {"name":"chainId","type":"uint256"},
                {"name":"verifyingContract","type":"address"},
            ],
            "TransferWithAuthorization": [
                {"name":"from","type":"address"},
                {"name":"to","type":"address"},
                {"name":"value","type":"uint256"},
                {"name":"validAfter","type":"uint256"},
                {"name":"validBefore","type":"uint256"},
                {"name":"nonce","type":"bytes32"},
            ],
        },
        "primaryType": "TransferWithAuthorization",
        "domain": {
            "name": "Mock USDC", "version": "1",
            "chainId": CHAIN_ID,
            "verifyingContract": ADDRESSES["MockUSDC"],
        },
        "message": {
            "from": oc.facilitator.address,
            "to": recipient,
            "value": value,
            "validAfter": 0,
            "validBefore": valid_before,
            "nonce": nonce_bytes,
        },
    }
    signable = encode_typed_data(full_message=typed_data)
    sig = Account.sign_message(signable, private_key=oc.facilitator.key)
    return {
        "from": oc.facilitator.address,
        "to": recipient,
        "value": str(value),
        "validAfter": 0,
        "validBefore": valid_before,
        "nonce": "0x" + nonce_bytes.hex(),
        "v": sig.v,
        "r": "0x" + sig.r.to_bytes(32, "big").hex(),
        "s": "0x" + sig.s.to_bytes(32, "big").hex(),
        "agentId": agent_id,
        "tokenBudget": value,
        "categoryId": category_id,
    }
