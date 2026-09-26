"""Chain-facing routes: frontend chain config, x402 payments, legacy contract reads."""
from __future__ import annotations

import json
import logging

from flask import Blueprint, Response, jsonify, request

from app.extensions import db, limiter
from app.services import api_error, get_onchain, is_valid_wallet, record_order_from_payment

log = logging.getLogger(__name__)
bp = Blueprint("chain", __name__)



def _chain_unavailable(exc: Exception):
    """Map chain-client failures to API errors: 503 when the contract or
    signer is not configured on this network, 502 for RPC/tx failures."""
    from chain.client import ContractNotConfigured
    if isinstance(exc, ContractNotConfigured):
        return api_error(str(exc), 503, code="NOT_DEPLOYED")
    return api_error(f"chain call failed: {str(exc)[:200]}", 502, code="CHAIN_ERROR")


@bp.route("/config.js")
def config_js():
    """window.AGENTSLIST_CHAIN / AGENTSLIST_ADDRESSES for the frontend, rendered
    from server env so the browser never carries hardcoded chain values."""
    from chain.config import get_chain_config, get_deployment
    from chain.usdc import cached_usdc_domain
    d = get_deployment()
    js = (
        "// Auto-generated from server env. Do not edit.\n"
        "window.AGENTSLIST_CHAIN = " + json.dumps(get_chain_config().to_frontend()) + ";\n"
        "window.AGENTSLIST_ADDRESSES = " + json.dumps(d["contracts"]) + ";\n"
        "window.AGENTSLIST_PAYMENT_RECIPIENT = " + json.dumps(d["paymentRecipient"]) + ";\n"
        # Cached/fallback domain only; the signer fetches /api/x402/domain,
        # which reads name()/version() from the token contract.
        "window.AGENTSLIST_USDC_DOMAIN = " + json.dumps(cached_usdc_domain()) + ";\n"
    )
    return Response(js, mimetype="application/javascript")


@bp.route("/api/onchain/info")
def api_onchain_info():
    from chain.config import get_deployment
    return jsonify(get_deployment())


@bp.route("/api/x402/domain")
def api_x402_domain():
    """EIP-712 domain + token metadata a buyer needs to sign an EIP-3009
    USDC authorization. Read from the token contract, cached, with a
    name "USDC" / version "2" fallback when the RPC is unreachable."""
    from chain.config import get_address, payment_recipient
    from chain.usdc import TRANSFER_WITH_AUTHORIZATION_TYPES, USDC_DECIMALS, get_usdc_domain
    domain = get_usdc_domain(refresh=request.args.get("refresh") == "1")
    return jsonify({
        "domain": {k: domain[k] for k in ("name", "version", "chainId", "verifyingContract")},
        "domainSource": domain["source"],
        "types": TRANSFER_WITH_AUTHORIZATION_TYPES,
        "primaryType": "TransferWithAuthorization",
        "token": {"address": get_address("USDC"), "symbol": "USDC", "decimals": USDC_DECIMALS},
        "recipient": payment_recipient(),
    })


@bp.route("/api/x402/pay", methods=["POST"])
@limiter.limit("30/minute")
def api_x402_pay():
    """Legacy payment endpoint, deliberately disabled for protected hires.

    The old body accepted a task and a platform-recipient permit without an
    immutable approval or ENS snapshot. Keeping it callable would create a
    payment bypass even if the new route is correct.
    """
    return api_error(
        "legacy payment path disabled; create a protected hire intent and use its x402 work route",
        410, code="LEGACY_PAYMENT_DISABLED",
    )


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
    if not (oc and oc.facilitator and oc.has_contract("AgentRegistry")):
        return api_error("legacy AgentRegistry is not deployed on this network; "
                         "ERC-8004 registration is on the roadmap", 503, code="NOT_DEPLOYED")
    try:
        return jsonify(oc.register_agent(payload["wallet"], payload["name"], payload["endpointURL"])), 201
    except Exception as e:
        return _chain_unavailable(e)


@bp.route("/api/session/<session_id>")
def api_session(session_id):
    """Read a legacy EscrowPayment session (replaced by EngagementEscrow)."""
    try:
        sid = int(session_id)
    except ValueError:
        return api_error("session id must be numeric", field="session_id")
    oc = get_onchain()
    if not (oc and oc.has_contract("EscrowPayment")):
        return api_error("EscrowPayment is not deployed on this network", 503, code="NOT_DEPLOYED")
    try:
        s = oc.get_session(sid)
    except Exception as e:
        return _chain_unavailable(e)
    if s.get("user") == "0x0000000000000000000000000000000000000000":
        return api_error("session not found", 404, code="SESSION_NOT_FOUND")
    return jsonify(s)


@bp.route("/api/session/<session_id>/cancel", methods=["POST"])
def api_session_cancel(session_id):
    try:
        sid = int(session_id)
    except ValueError:
        return api_error("session id must be numeric", field="session_id")
    oc = get_onchain()
    if not (oc and oc.facilitator and oc.has_contract("EscrowPayment")):
        return api_error("EscrowPayment is not deployed on this network", 503, code="NOT_DEPLOYED")
    try:
        return jsonify(oc.cancel_session(sid))
    except Exception as e:
        return _chain_unavailable(e)
