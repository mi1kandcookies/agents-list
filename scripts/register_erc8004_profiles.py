#!/usr/bin/env python3
"""Register marketplace profiles in the canonical ERC-8004 Sepolia registry.

The default mode is read-only and prints the registrations that would be sent.
Use ``--confirm-live`` only after checking the list and funding the dedicated
operator wallet. The key is read from the process environment (normally loaded
from the local keychain); it is never accepted as a CLI argument or printed.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import create_app
from app.extensions import db
from app.models import Agent
from chain.config import get_address, get_chain_config, explorer_url


def _registry_id(address: str) -> str:
    return f"eip155:{get_chain_config().chain_id}:{address}"


def _data_uri(payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    encoded = base64.b64encode(raw).decode("ascii")
    return f"data:application/json;base64,{encoded}"


def _registration(agent, registry: str, *, agent_id: int | None = None) -> dict:
    services = []
    endpoint = (os.environ.get("MCP_PUBLIC_URL") or "").strip()
    if endpoint:
        services.append({"name": "mcp", "endpoint": endpoint.rstrip("/")})
    if agent.ens_name:
        services.append({"name": "ens", "endpoint": agent.ens_name})
    payload = {
        "type": "https://eips.ethereum.org/EIPS/eip-8004#registration-v1",
        "name": agent.name,
        "description": agent.description,
        "agentRegistry": _registry_id(registry),
        "services": services,
        "capabilities": agent.capabilities,
        "tags": agent.tags,
        "payoutAddress": agent.payout_address or agent.deployer_wallet,
        "ensName": agent.ens_name,
        "supportedTrust": ["reputation"],
    }
    if agent_id is not None:
        payload["registrations"] = [{"agentRegistry": _registry_id(registry),
                                     "agentId": str(agent_id)}]
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-id", action="append", dest="agent_ids",
                        help="register only this AGT public id; repeatable")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--confirm-live", action="store_true",
                        help="broadcast Sepolia registrations and URI updates")
    parser.add_argument("--update-existing", action="store_true",
                        help="refresh URI for identities already stored in the database")
    args = parser.parse_args(argv)

    registry = get_address("IdentityRegistry")
    if not registry:
        print("IdentityRegistry is not configured", file=sys.stderr)
        return 2
    with create_app("development").app_context():
        query = Agent.query.filter(Agent.verification_tier != "suspended")
        if args.agent_ids:
            query = query.filter(Agent.public_id.in_(args.agent_ids))
        agents = query.order_by(Agent.id).limit(max(1, min(args.limit, 500))).all()
        if not agents:
            print("No matching active agents.", file=sys.stderr)
            return 1

        pending = [a for a in agents if not a.erc8004_agent_id]
        existing = [a for a in agents if a.erc8004_agent_id]
        print(json.dumps({
            "chain": get_chain_config().name,
            "registry": registry,
            "registry_id": _registry_id(registry),
            "would_register": [a.public_id for a in pending],
            "already_registered": [a.public_id for a in existing],
            "broadcast": bool(args.confirm_live),
        }, indent=2))
        if not args.confirm_live:
            print("Read-only plan. Re-run with --confirm-live to broadcast.")
            return 0

        private_key = (os.environ.get("ERC8004_OPERATOR_PRIVATE_KEY") or
                       os.environ.get("ENS_OPERATOR_PRIVATE_KEY") or "").strip()
        if not private_key:
            print("ERC8004_OPERATOR_PRIVATE_KEY or ENS_OPERATOR_PRIVATE_KEY is required", file=sys.stderr)
            return 2
        from eth_account import Account
        from web3 import Web3
        w3 = Web3(Web3.HTTPProvider(get_chain_config().rpc_url, request_kwargs={"timeout": 10}))
        if not w3.is_connected() or int(w3.eth.chain_id) != get_chain_config().chain_id:
            print("RPC is not connected to Ethereum Sepolia", file=sys.stderr)
            return 2
        from chain.erc8004 import ERC8004Identity
        identity = ERC8004Identity(w3, registry)
        signer = Account.from_key(private_key)
        next_nonce = w3.eth.get_transaction_count(signer.address, "pending")
        results = []
        for agent in agents:
            if agent.erc8004_agent_id and not args.update_existing:
                continue
            if agent.erc8004_agent_id:
                uri = _data_uri(_registration(agent, registry, agent_id=agent.erc8004_agent_id))
                tx_hash = identity.set_agent_uri(signer, agent.erc8004_agent_id, uri, nonce=next_nonce)
                next_nonce += 1
                agent.erc8004_registry = registry
                db.session.commit()
                results.append({"agent_id": agent.public_id, "erc8004_agent_id": agent.erc8004_agent_id,
                                "uri_update_tx": tx_hash, "explorer": explorer_url("tx", tx_hash)})
                continue
            initial_uri = _data_uri(_registration(agent, registry))
            minted = identity.register(signer, initial_uri, nonce=next_nonce)
            next_nonce += 1
            token_id = minted["agent_id"]
            if token_id is None:
                raise RuntimeError(f"registration event missing agent id for {agent.public_id}")
            # Persist the token before the second transaction. If the URI
            # update is interrupted, --update-existing can resume it without
            # minting a second identity.
            agent.erc8004_agent_id = int(token_id)
            agent.erc8004_registry = registry
            db.session.commit()
            final_uri = _data_uri(_registration(agent, registry, agent_id=token_id))
            uri_tx = identity.set_agent_uri(signer, token_id, final_uri, nonce=next_nonce)
            next_nonce += 1
            results.append({"agent_id": agent.public_id, "erc8004_agent_id": int(token_id),
                            "register_tx": minted["tx_hash"], "uri_update_tx": uri_tx,
                            "register_explorer": explorer_url("tx", minted["tx_hash"]),
                            "uri_explorer": explorer_url("tx", uri_tx)})
        print(json.dumps({"registered": results}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
