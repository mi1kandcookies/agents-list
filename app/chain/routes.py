"""Chain-facing routes: frontend chain config, x402 payments, legacy contract reads."""
from __future__ import annotations

import json
import hmac
import logging
import os
from decimal import Decimal

from flask import Blueprint, Response, jsonify, request

from app.extensions import limiter
from app.services import api_error, get_onchain, is_valid_wallet

log = logging.getLogger(__name__)
bp = Blueprint("chain", __name__)



def _chain_unavailable(exc: Exception):
    """Map chain-client failures to API errors: 503 when the contract or
    signer is not configured on this network, 502 for RPC/tx failures."""
    from chain.client import ContractNotConfigured
    if isinstance(exc, ContractNotConfigured):
        return api_error(str(exc), 503, code="NOT_DEPLOYED")
    return api_error(f"chain call failed: {str(exc)[:200]}", 502, code="CHAIN_ERROR")


def _mcp_read_guard():
    """Protect machine-readable wallet data without exposing private keys.

    Local development remains convenient when MCP_API_TOKEN is unset.  Once a
    token is configured, terminal clients must present it; browser cookies and
    frontend headers are not accepted for this machine-facing endpoint.
    """
    token = (os.environ.get("MCP_API_TOKEN") or "").strip()
    if token:
        provided = request.headers.get("Authorization", "")
        if not hmac.compare_digest(provided.encode(), f"Bearer {token}".encode()):
            return jsonify({"error": "missing or invalid bearer token", "code": "UNAUTHORIZED"}), 401
    return None


_WALLET_ROLES = {
    "ens_operator": "ENS_OPERATOR_PRIVATE_KEY",
    "facilitator": "FACILITATOR_PRIVATE_KEY",
    "escrow": "ESCROW_PRIVATE_KEY",
    "buyer_vault": "BUYER_VAULT_PRIVATE_KEY",
    "x402_payer": "X402_PAYER_PRIVATE_KEY",
    "mock_deployer": "MOCK_USDC_DEPLOYER_PRIVATE_KEY",
}


def _wallet_snapshot() -> dict:
    """Read public balances for locally configured operator roles.

    This function derives addresses in-process and never serializes a key.
    Missing keys are represented as unconfigured roles so the terminal can
    explain what still needs funding before an on-chain rehearsal.
    """
    from chain.config import get_address, get_chain_config

    cfg = get_chain_config()
    token = get_address("USDC")
    rows = []
    try:
        from eth_account import Account
        from web3 import Web3
        w3 = Web3(Web3.HTTPProvider(cfg.rpc_url, request_kwargs={"timeout": 4}))
        connected = bool(w3.is_connected())
    except Exception as exc:
        w3, connected = None, False
        rpc_error = f"RPC unavailable: {exc.__class__.__name__}"

    token_contract = None
    if w3 is not None and connected and token:
        try:
            token_contract = w3.eth.contract(
                address=Web3.to_checksum_address(token),
                abi=[{"type": "function", "name": "balanceOf", "stateMutability": "view",
                      "inputs": [{"name": "account", "type": "address"}],
                      "outputs": [{"type": "uint256"}]}],
            )
        except Exception:
            token_contract = None

    for role, env_name in _WALLET_ROLES.items():
        key = (os.environ.get(env_name) or "").strip()
        row = {"role": role, "env_var": env_name, "configured": bool(key),
               "address": None, "eth_balance_wei": None, "eth_balance": None,
               "token_balance_atomic": None, "token_balance_usdc": None, "error": None}
        if not key:
            rows.append(row)
            continue
        try:
            account = Account.from_key(key)
            row["address"] = account.address
            if not connected:
                row["error"] = rpc_error if "rpc_error" in locals() else "RPC unavailable"
            else:
                wei = int(w3.eth.get_balance(account.address))
                row["eth_balance_wei"] = wei
                row["eth_balance"] = str(Decimal(wei) / Decimal(10 ** 18))
                if token_contract is not None:
                    atomic = int(token_contract.functions.balanceOf(account.address).call())
                    row["token_balance_atomic"] = atomic
                    row["token_balance_usdc"] = str(Decimal(atomic) / Decimal(10 ** 6))
                elif token:
                    row["error"] = "payment token balance unavailable"
        except Exception as exc:
            # Keep the public address when it was derived, but make RPC/key
            # failures explicit instead of manufacturing a zero balance.
            row["error"] = str(exc)[:160]
        rows.append(row)

    return {
        "chain": {"chainId": cfg.chain_id, "name": cfg.name, "caip2": cfg.caip2,
                  "rpcUrl": cfg.rpc_url, "explorer": cfg.explorer},
        "token": {"address": token, "symbol": "USDC", "name": "USD Coin", "decimals": 6},
        "rpc_connected": connected,
        "wallets": rows,
        "configured_wallets": sum(1 for row in rows if row["configured"]),
    }


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


@bp.route("/api/wallet/status")
def api_wallet_status():
    denied = _mcp_read_guard()
    if denied:
        return denied
    return jsonify(_wallet_snapshot())


@bp.route("/api/protocol/status")
def api_protocol_status():
    denied = _mcp_read_guard()
    if denied:
        return denied
    from chain.config import get_deployment
    from app.names import service as names
    tree = names.tree()
    names_client = names.get_client()
    try:
        name_count = len(tree.get("children") or [])
    except AttributeError:
        name_count = 0
    return jsonify({
        "onchain": get_deployment(),
        "wallets": _wallet_snapshot(),
        "integrations": {
            "ens": {"configured": names_client.configured, "local_name_records": name_count},
            "intercepta": {"configured": bool((os.environ.get("INTERCEPTA_API_KEY") or "").strip()),
                           "fail_closed": True},
            "world_approval": {"backend_gate": True},
        },
    })


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
    """Closed. It submitted any buyer-signed USDC authorization and recorded
    an order without an approval, a mandate or screening. Paying an agent now
    goes through an approved engagement (POST /api/engagements/<id>/hire) or,
    agent to agent, the x402 v2 task endpoint under a mandate."""
    return api_error("this payment path is disabled; hire through /api/engagements or pay "
                     "an agent task via POST /api/agents/<agent_id>/tasks (x402 v2 + mandate)",
                     410, code="LEGACY_PAYMENT_DISABLED")


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
    """Closed. It had the facilitator cancel (refund) a legacy EscrowPayment
    session for any caller, with no approval. Engagement escrow replaces it."""
    return api_error("legacy escrow sessions cannot be changed from the API; engagement "
                     "escrow moves funds only after an approval", 410,
                     code="LEGACY_ESCROW_DISABLED")
