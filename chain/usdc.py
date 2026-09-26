"""
chain/usdc.py - payment-token metadata, most importantly the EIP-712 domain
buyers sign EIP-3009 authorizations against.

The domain is read from the token contract (`name()` and `version()`) rather
than hardcoded, so pointing USDC_ADDRESS at a different EIP-3009 token just
works. Circle's Sepolia USDC reports name "USDC", version "2"; that pair is
the fallback when the RPC is unreachable, so the app never blocks on the
network. Results are cached per (chain, token) for DOMAIN_CACHE_SECONDS.
"""
from __future__ import annotations

import logging
import os
import threading
import time

from chain.config import get_address, get_chain_config

log = logging.getLogger(__name__)

FALLBACK_NAME = "USDC"
FALLBACK_VERSION = "2"
USDC_DECIMALS = 6

TOKEN_METADATA_ABI = [
    {"type": "function", "name": "name", "stateMutability": "view",
     "inputs": [], "outputs": [{"type": "string"}]},
    {"type": "function", "name": "version", "stateMutability": "view",
     "inputs": [], "outputs": [{"type": "string"}]},
    {"type": "function", "name": "DOMAIN_SEPARATOR", "stateMutability": "view",
     "inputs": [], "outputs": [{"type": "bytes32"}]},
]

TRANSFER_WITH_AUTHORIZATION_TYPES = {
    "TransferWithAuthorization": [
        {"name": "from", "type": "address"},
        {"name": "to", "type": "address"},
        {"name": "value", "type": "uint256"},
        {"name": "validAfter", "type": "uint256"},
        {"name": "validBefore", "type": "uint256"},
        {"name": "nonce", "type": "bytes32"},
    ],
}

_cache: dict[tuple, tuple[float, dict]] = {}
_lock = threading.Lock()


def _ttl() -> float:
    try:
        return float(os.environ.get("DOMAIN_CACHE_SECONDS", "3600"))
    except ValueError:
        return 3600.0


def fallback_domain(address: str | None = None, chain_id: int | None = None) -> dict:
    """The domain Circle USDC uses, without any network call."""
    return {
        "name": FALLBACK_NAME,
        "version": FALLBACK_VERSION,
        "chainId": chain_id if chain_id is not None else get_chain_config().chain_id,
        "verifyingContract": address if address is not None else get_address("USDC"),
        "source": "fallback",
    }


def read_domain(contract, *, chain_id: int) -> dict:
    """Read name()/version() from a web3 contract bound to TOKEN_METADATA_ABI.
    Raises on any RPC failure; callers decide how to degrade."""
    fn = contract.functions
    name = fn.name().call()
    try:
        version = fn.version().call()
    except Exception:
        # Not every EIP-3009 token exposes version(); FiatToken v2 does.
        version = FALLBACK_VERSION
    return {
        "name": name,
        "version": version,
        "chainId": chain_id,
        "verifyingContract": contract.address,
        "source": "contract",
    }


def get_usdc_domain(*, w3=None, refresh: bool = False) -> dict:
    """EIP-712 domain for the configured USDC token, read from the contract
    and cached; falls back to name "USDC", version "2" on any failure."""
    chain = get_chain_config()
    address = get_address("USDC")
    if not address:
        return fallback_domain(None, chain.chain_id)
    key = (chain.chain_id, address.lower(), chain.rpc_url)
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and not refresh and now - hit[0] < _ttl():
            return dict(hit[1])
    try:
        if w3 is None:
            from web3 import Web3
            w3 = Web3(Web3.HTTPProvider(chain.rpc_url, request_kwargs={"timeout": 5}))
        from web3 import Web3 as _W3
        contract = w3.eth.contract(address=_W3.to_checksum_address(address), abi=TOKEN_METADATA_ABI)
        domain = read_domain(contract, chain_id=chain.chain_id)
    except Exception as exc:
        log.info("USDC domain read failed (%s); using fallback", str(exc)[:120])
        domain = fallback_domain(address, chain.chain_id)
        # Cache fallbacks briefly so a flaky RPC is retried soon.
        with _lock:
            _cache[key] = (now - _ttl() + 60, domain)
        return dict(domain)
    with _lock:
        _cache[key] = (now, domain)
    return dict(domain)


def cached_usdc_domain() -> dict:
    """Domain from cache if present, else the fallback. Never hits the network
    (safe to call while rendering /config.js)."""
    chain = get_chain_config()
    address = get_address("USDC")
    if address:
        hit = _cache.get((chain.chain_id, address.lower(), chain.rpc_url))
        if hit:
            return dict(hit[1])
    return fallback_domain(address, chain.chain_id)


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def to_micro(amount_usdc: float) -> int:
    return int(round(float(amount_usdc) * 10 ** USDC_DECIMALS))


def authorization_typed_data(permit: dict, domain: dict) -> dict:
    """Full EIP-712 payload for a TransferWithAuthorization permit."""
    nonce = str(permit["nonce"])
    return {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            **TRANSFER_WITH_AUTHORIZATION_TYPES,
        },
        "primaryType": "TransferWithAuthorization",
        "domain": {k: domain[k] for k in ("name", "version", "chainId", "verifyingContract")},
        "message": {
            "from": permit["from"],
            "to": permit["to"],
            "value": int(permit["value"]),
            "validAfter": int(permit.get("validAfter", 0)),
            "validBefore": int(permit["validBefore"]),
            "nonce": bytes.fromhex(nonce[2:] if nonce.startswith("0x") else nonce),
        },
    }


def recover_authorization_signer(permit: dict, domain: dict) -> str:
    """Address that signed the permit's (v, r, s) over the given domain."""
    from eth_account import Account
    from eth_account.messages import encode_typed_data

    signable = encode_typed_data(full_message=authorization_typed_data(permit, domain))
    r = int(str(permit["r"]), 16)
    s = int(str(permit["s"]), 16)
    return Account.recover_message(signable, vrs=(int(permit["v"]), r, s))
