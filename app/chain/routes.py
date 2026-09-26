"""Chain-facing routes: frontend chain config, x402 payments, legacy contract reads."""
from __future__ import annotations

import logging
import os

from flask import Blueprint, jsonify, request

from app.extensions import db, limiter
from app.services import api_error, get_onchain, is_valid_wallet, record_order_from_payment

log = logging.getLogger(__name__)
bp = Blueprint("chain", __name__)

FACILITATOR_URL = os.environ.get("FACILITATOR_URL")


@bp.route("/config.js")
def config_js():
    import json
    from flask import Response
    from chain.client import get_deployment
    d = get_deployment()
    js = (
        "// Auto-generated from server env. Do not edit.\n"
        "window.AGENTSLIST_CHAIN = " + json.dumps({
            "chainId":    d["chainId"],
            "chainIdHex": d["chainIdHex"],
            "name":       d["chain"],
            "rpcUrl":     d["rpcUrl"],
            "explorer":   d["explorer"],
            "nativeCurrency": {"name": "AVAX", "symbol": "AVAX", "decimals": 18},
        }) + ";\n"
        "window.AGENTSLIST_ADDRESSES = " + json.dumps(d["contracts"]) + ";\n"
    )
    return Response(js, mimetype="application/javascript")


@bp.route("/api/onchain/info")
def api_onchain_info():
    from chain.client import get_deployment
    return jsonify(get_deployment())


@bp.route("/api/x402/pay", methods=["POST"])
@limiter.limit("30/minute")
def api_x402_pay():
    """Execute a buyer-signed EIP-3009 transferWithAuthorization via the
    facilitator and record the resulting order."""
    payload = request.get_json(silent=True) or {}
    required = ["from", "to", "value", "validBefore", "nonce", "v", "r", "s", "agentId"]
    missing = [k for k in required if k not in payload]
    if missing:
        return api_error(f"missing fields: {missing}", field=missing[0])
    for field in ("from", "to"):
        if not is_valid_wallet(payload.get(field)):
            return api_error(f"'{field}' must be a 0x-prefixed 40-hex address", field=field)
    try:
        value_micro = int(payload["value"])
        agent_id = int(payload["agentId"])
    except (TypeError, ValueError):
        return api_error("value and agentId must be integers", field="value")
    if value_micro <= 0:
        return api_error("value must be > 0", field="value")
    from app.models import Agent as AgentModel
    if not db.session.get(AgentModel, agent_id):
        return api_error("agent not found", 404, code="AGENT_NOT_FOUND", field="agentId")

    amount_usdc = value_micro / 1_000_000.0
    buyer_addr = payload["from"]
    task = str(payload.get("task") or "")[:4000]

    oc = get_onchain()
    if oc and oc.facilitator:
        try:
            result = oc.x402_execute(payload)
        except Exception as e:
            log.warning("x402 execute failed: %s", e)
            return api_error(f"on-chain payment failed: {str(e)[:200]}", 502, code="PAYMENT_FAILED")
        tx_hash = (result.get("txHashes") or {}).get("permit") or ""
        order_id = record_order_from_payment(agent_id, buyer_addr, amount_usdc,
                                              paid=True, task=task, tx_hash=tx_hash)
        return jsonify({**result, "orderId": order_id, "realTx": True})

    # No facilitator configured: record the order as awaiting payment.
    order_id = record_order_from_payment(agent_id, buyer_addr, amount_usdc, paid=False, task=task)
    return jsonify({
        "orderId": order_id,
        "agentId": agent_id,
        "status": "pending_payment",
        "realTx": False,
        "note": "FACILITATOR_PRIVATE_KEY is not set, so the signed authorization was not submitted on-chain.",
    })


@bp.route("/api/agents/register", methods=["POST"])
def api_agents_register():
    """Register a new agent on-chain via AgentRegistry.registerAgent."""
    payload = request.get_json(silent=True) or {}
    required = ["wallet", "name", "endpointURL"]
    missing = [k for k in required if k not in payload]
    if missing:
        return api_error(f"missing fields: {missing}", field=missing[0])
    if not is_valid_wallet(payload.get("wallet")):
        return api_error("wallet must be a 0x-prefixed 40-hex address", field="wallet")
    if len(str(payload.get("name") or "").strip()) < 3:
        return api_error("name must be at least 3 characters", field="name")

    oc = get_onchain()
    if oc:
        try:
            result = oc.register_agent(payload["wallet"], payload["name"], payload["endpointURL"])
            return jsonify(result), 201
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    return jsonify({
        "agentId": None,
        "status": "mock_registered",
        "note": "No FACILITATOR_PRIVATE_KEY - registration not sent on-chain.",
    }), 201


@bp.route("/api/session/<session_id>")
def api_session(session_id):
    try:
        sid = int(session_id)
    except ValueError:
        return jsonify({"error": "session id must be numeric"}), 400
    oc = get_onchain()
    if oc:
        try:
            s = oc.get_session(sid)
            # Contract returns zero-struct for unknown sessions; map that to 404.
            if s.get("user") == "0x0000000000000000000000000000000000000000":
                return jsonify({"error": "session not found"}), 404
            return jsonify(s)
        except Exception as e:
            return jsonify({"error": str(e)}), 502
    if FACILITATOR_URL:
        try:
            import requests
            r = requests.get(f"{FACILITATOR_URL}/session/{session_id}", timeout=10)
            return (r.text, r.status_code, r.headers.items())
        except Exception as e:
            return jsonify({"error": str(e)}), 502
    return jsonify({"error": "no on-chain backend configured"}), 503


@bp.route("/api/session/<session_id>/cancel", methods=["POST"])
def api_session_cancel(session_id):
    try:
        sid = int(session_id)
    except ValueError:
        return jsonify({"error": "session id must be numeric"}), 400

    oc = get_onchain()
    if oc:
        try:
            return jsonify(oc.cancel_session(sid))
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    if FACILITATOR_URL:
        try:
            import requests as _req
            r = _req.post(f"{FACILITATOR_URL}/session/{session_id}/cancel", timeout=15)
            return (r.text, r.status_code, r.headers.items())
        except Exception as e:
            return jsonify({"error": str(e)}), 502
    return jsonify({"error": "no on-chain backend configured"}), 503
