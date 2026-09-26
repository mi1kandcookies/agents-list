#!/usr/bin/env python3
"""Read-only Sepolia preflight for the configured wallet roles and token.

This script never signs or broadcasts. It prints public addresses, native
balances, the selected payment token and bytecode presence so funding and
deployment mistakes are visible before a live smoke test.
"""
from __future__ import annotations

import os
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from eth_account import Account
    from web3 import Web3
except ImportError as exc:  # pragma: no cover - operator-facing helper
    raise SystemExit("install requirements.txt before running this helper") from exc

from chain.config import get_address, get_chain_config, payment_token_mode


ROLES = (
    ("ENS operator", "ENS_OPERATOR_PRIVATE_KEY"),
    ("facilitator", "FACILITATOR_PRIVATE_KEY"),
    ("escrow", "ESCROW_PRIVATE_KEY"),
    ("buyer vault", "BUYER_VAULT_PRIVATE_KEY"),
    ("x402 payer", "X402_PAYER_PRIVATE_KEY"),
    ("mock deployer", "MOCK_USDC_DEPLOYER_PRIVATE_KEY"),
)


def _eth(w3: Web3, address: str) -> str:
    return f"{Decimal(w3.eth.get_balance(address)) / Decimal(10**18):.6f} ETH"


def main() -> int:
    cfg = get_chain_config()
    w3 = Web3(Web3.HTTPProvider(cfg.rpc_url, request_kwargs={"timeout": 10}))
    if not w3.is_connected():
        print(f"RPC unreachable: {cfg.rpc_url}", file=sys.stderr)
        return 2
    actual_chain = int(w3.eth.chain_id)
    print(f"network: {cfg.name} ({actual_chain})")
    print(f"rpc: {cfg.rpc_url}")
    if actual_chain != cfg.chain_id:
        print(f"ERROR: configured chain id {cfg.chain_id} != RPC chain id {actual_chain}", file=sys.stderr)
        return 2

    token_mode = payment_token_mode()
    token_symbol = "mUSDC" if token_mode == "mock" else "USDC"
    token = get_address("USDC")
    print(f"payment_token_mode: {token_mode}")
    print(f"payment_token: {token or 'not configured'}")
    token_contract = None
    if token:
        code = w3.eth.get_code(Web3.to_checksum_address(token))
        print(f"payment_token_code: {len(code)} bytes")
        token_contract = w3.eth.contract(
            address=Web3.to_checksum_address(token),
            abi=[{"type": "function", "name": "balanceOf", "stateMutability": "view",
                  "inputs": [{"name": "account", "type": "address"}],
                  "outputs": [{"name": "", "type": "uint256"}]}],
        )

    for label, variable in ROLES:
        key = (os.environ.get(variable) or "").strip()
        if not key:
            print(f"{label}: not loaded ({variable})")
            continue
        try:
            address = Account.from_key(key).address
            line = f"{label}: {address} ({_eth(w3, address)})"
            if token_contract is not None:
                balance = int(token_contract.functions.balanceOf(address).call())
                line += f" ({balance / 10**6:.6f} {token_symbol})"
            print(line)
        except (TypeError, ValueError):
            print(f"{label}: invalid key material in {variable}", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
