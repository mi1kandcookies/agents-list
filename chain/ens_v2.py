"""ENSv2 resolution and snapshot helpers for protected hires.

Resolution is intentionally strict: a missing or failed record never falls
back to the platform treasury. ``ENS_MODE=fixture`` is available only for the
local demo and is labelled in the returned snapshot.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from urllib.parse import urlparse

from chain.config import get_chain_config

UNIVERSAL_RESOLVER_SEPOLIA = "0xeEeEEEeE14D718C2B47D9923Deab1335E144EeEe"
ETH_COIN_TYPE = 60

UNIVERSAL_RESOLVER_ABI = [{
    "type": "function", "name": "resolve", "stateMutability": "view",
    "inputs": [{"name": "name", "type": "bytes"}, {"name": "data", "type": "bytes"}],
    "outputs": [{"name": "result", "type": "bytes"}, {"name": "resolver", "type": "address"}],
}]


class ENSResolutionError(RuntimeError):
    """Raised when a name cannot provide all authorization-critical records."""


@dataclass(frozen=True)
class AgentResolution:
    name: str
    address: str
    endpoint: str
    enabled: bool
    status: str
    resolver: str
    source: str

    def snapshot(self) -> dict:
        return asdict(self)


def _normalise_name(name: str) -> str:
    value = str(name or "").strip().rstrip(".").lower()
    if not value or any(not label or len(label.encode("utf-8")) > 63 for label in value.split(".")):
        raise ENSResolutionError("specialist ENS name is invalid")
    if not value.endswith(".eth"):
        raise ENSResolutionError("specialist ENS name must end in .eth")
    return value


def _dns_encode(name: str) -> bytes:
    labels = _normalise_name(name).split(".")
    return b"".join(bytes([len(label.encode("utf-8"))]) + label.encode("utf-8") for label in labels) + b"\x00"


def namehash(name: str) -> bytes:
    """ENS namehash for resolver profile calldata."""
    from web3 import Web3

    node = b"\x00" * 32
    for label in reversed(_normalise_name(name).split(".")):
        node = Web3.keccak(node + Web3.keccak(text=label))
    return node


def _fixture_records(name: str) -> AgentResolution:
    try:
        fixtures = json.loads(os.environ.get("ENS_FIXTURES_JSON", "{}"))
    except ValueError as exc:
        raise ENSResolutionError("ENS_FIXTURES_JSON is not valid JSON") from exc
    record = fixtures.get(name)
    if not isinstance(record, dict):
        raise ENSResolutionError(f"ENS fixture is missing for {name}")
    address = str(record.get("address") or "")
    endpoint = str(record.get("endpoint") or "")
    if not _is_address(address):
        raise ENSResolutionError(f"ENS address record is missing or invalid for {name}")
    if not endpoint:
        raise ENSResolutionError(f"ENS work endpoint is missing for {name}")
    return AgentResolution(
        name=name,
        address=address,
        endpoint=endpoint,
        enabled=bool(record.get("enabled", False)),
        status=str(record.get("status") or "unknown"),
        resolver=str(record.get("resolver") or "fixture"),
        source="fixture",
    )


def _is_address(value: str) -> bool:
    import re
    return bool(re.fullmatch(r"0x[0-9a-fA-F]{40}", value))


def _live_resolve(name: str, w3=None) -> AgentResolution:
    from web3 import Web3

    if w3 is None:
        cfg = get_chain_config()
        w3 = Web3(Web3.HTTPProvider(cfg.rpc_url, request_kwargs={"timeout": 10}))
    resolver_address = os.environ.get("ENS_UNIVERSAL_RESOLVER", UNIVERSAL_RESOLVER_SEPOLIA)
    if not _is_address(resolver_address):
        raise ENSResolutionError("ENS_UNIVERSAL_RESOLVER is invalid")
    universal = w3.eth.contract(address=Web3.to_checksum_address(resolver_address),
                                 abi=UNIVERSAL_RESOLVER_ABI)
    node = namehash(name)

    def read_address():
        # Function selectors are four raw bytes followed by ABI-encoded args;
        # encoding the selector itself as a bytes4 argument would add padding.
        data = Web3.keccak(text="addr(bytes32)")[:4] + w3.codec.encode(["bytes32"], [node])
        result, _resolver = universal.functions.resolve(_dns_encode(name), data).call()
        return w3.codec.decode(["address"], result)[0]

    try:
        address_bytes = read_address()
        endpoint = None
        status = None
        enabled = None
        for profile_key in ("com.agentslist.work-endpoint", "com.agentslist.status", "com.agentslist.enabled"):
            encoded_args = w3.codec.encode(["bytes32", "string"], [node, profile_key])
            profile_data = Web3.keccak(text="text(bytes32,string)")[:4] + encoded_args
            result, _ = universal.functions.resolve(_dns_encode(name), profile_data).call()
            value = w3.codec.decode(["string"], result)[0]
            if profile_key.endswith("work-endpoint"):
                endpoint = value
            elif profile_key.endswith("status"):
                status = value
            else:
                enabled = value.strip().lower() == "true"
    except Exception as exc:
        raise ENSResolutionError(f"ENSv2 resolution failed for {name}: {str(exc)[:180]}") from exc

    checksum = Web3.to_checksum_address(address_bytes)
    if not endpoint:
        raise ENSResolutionError(f"ENS work endpoint is missing for {name}")
    if enabled is None:
        raise ENSResolutionError(f"ENS enabled record is missing for {name}")
    return AgentResolution(name, checksum, endpoint, enabled, status or "unknown",
                           resolver_address, "ensv2")


def resolve_agent(name: str, *, w3=None) -> AgentResolution:
    canonical = _normalise_name(name)
    if os.environ.get("ENS_MODE", "live").strip().lower() == "fixture":
        return _fixture_records(canonical)
    return _live_resolve(canonical, w3=w3)


def validate_endpoint(endpoint: str) -> str:
    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ENSResolutionError("specialist endpoint must be HTTPS")
    allowed_origin = os.environ.get("AGENT_ENDPOINT_ORIGIN", "").strip().rstrip("/")
    if allowed_origin:
        origin = urlparse(allowed_origin)
        if (origin.scheme, origin.netloc) != (parsed.scheme, parsed.netloc):
            raise ENSResolutionError("specialist endpoint is outside AGENT_ENDPOINT_ORIGIN")
    return endpoint


def resolve_authorized_agent(name: str, *, w3=None) -> AgentResolution:
    result = resolve_agent(name, w3=w3)
    validate_endpoint(result.endpoint)
    if not result.enabled:
        raise ENSResolutionError("specialist is disabled in ENS")
    return result
