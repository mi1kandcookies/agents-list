"""
chain/ens_v2.py - read-only ENS lookups through the ENSv2 Universal Resolver
on Sepolia (the same deployment the names sidecar writes to, ens/).

    from chain.ens_v2 import UniversalResolver
    ur = UniversalResolver.from_env()
    ur.payout_address("helper.agentslist-app.eth")   # x402 payout record or None
    ur.text("helper.agentslist-app.eth", "agent-context")

Every call is ``UniversalResolver.resolve(dnsEncode(name), calldata)``, with
calldata for the resolver profile (``text(bytes32,string)``,
``addr(bytes32)``). A missing record comes back as ``None``; any RPC or
decoding failure raises ENSResolutionError so callers can fail closed.

No Flask imports. Nothing here writes to the chain.
"""
from __future__ import annotations

import os
import re

UNIVERSAL_RESOLVER_SEPOLIA = "0xeEeEEEeE14D718C2B47D9923Deab1335E144EeEe"
# Text record holding the address an agent is paid at over x402. Kept here,
# next to the resolver calls, like the sidecar's RECORD_KEYS map.
PAYOUT_RECORD_KEY = "x402-payto"
ZERO_ADDRESS = "0x" + "0" * 40

UNIVERSAL_RESOLVER_ABI = [{
    "type": "function", "name": "resolve", "stateMutability": "view",
    "inputs": [{"name": "name", "type": "bytes"}, {"name": "data", "type": "bytes"}],
    "outputs": [{"name": "result", "type": "bytes"}, {"name": "resolver", "type": "address"}],
}]

_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


class ENSResolutionError(RuntimeError):
    def __init__(self, message: str, code: str = "ENS_UNAVAILABLE"):
        super().__init__(message)
        self.code = code


def normalize_name(name: str) -> str:
    value = str(name or "").strip().rstrip(".").lower()
    labels = value.split(".")
    if not value or any(not label or len(label.encode("utf-8")) > 63 for label in labels):
        raise ENSResolutionError(f"invalid ENS name {name!r}", "ENS_INVALID_NAME")
    return value


def dns_encode(name: str) -> bytes:
    out = b""
    for label in normalize_name(name).split("."):
        raw = label.encode("utf-8")
        out += bytes([len(raw)]) + raw
    return out + b"\x00"


def namehash(name: str) -> bytes:
    from eth_utils import keccak
    node = b"\x00" * 32
    for label in reversed(normalize_name(name).split(".")):
        node = keccak(node + keccak(text=label))
    return node


def _selector(signature: str) -> bytes:
    from eth_utils import keccak
    return keccak(text=signature)[:4]


def text_calldata(name: str, key: str) -> bytes:
    from eth_abi import encode
    return _selector("text(bytes32,string)") + encode(["bytes32", "string"], [namehash(name), key])


def addr_calldata(name: str) -> bytes:
    from eth_abi import encode
    return _selector("addr(bytes32)") + encode(["bytes32"], [namehash(name)])


class UniversalResolver:
    def __init__(self, *, w3=None, address: str = UNIVERSAL_RESOLVER_SEPOLIA,
                 rpc_url: str | None = None, timeout: float = 10.0):
        if not _ADDRESS_RE.match(address or ""):
            raise ENSResolutionError("ENS universal resolver address is invalid", "ENS_NOT_CONFIGURED")
        self.address = address
        self._w3 = w3
        self._rpc_url = rpc_url
        self._timeout = timeout

    @classmethod
    def from_env(cls, address: str | None = None) -> "UniversalResolver":
        return cls(address=address or os.environ.get("ENS_UNIVERSAL_RESOLVER")
                   or UNIVERSAL_RESOLVER_SEPOLIA)

    def _web3(self):
        if self._w3 is None:
            from web3 import Web3
            from chain.config import get_chain_config
            url = self._rpc_url or get_chain_config().rpc_url
            self._w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": self._timeout}))
        return self._w3

    def _call(self, name: str, data: bytes) -> bytes:
        """``resolve(dnsEncode(name), data)`` → the resolver's return data."""
        try:
            from web3 import Web3
            w3 = self._web3()
            contract = w3.eth.contract(address=Web3.to_checksum_address(self.address),
                                       abi=UNIVERSAL_RESOLVER_ABI)
            result, _resolver = contract.functions.resolve(dns_encode(name), data).call()
            return bytes(result)
        except ENSResolutionError:
            raise
        except Exception as exc:
            raise ENSResolutionError(f"ENS lookup for {name} failed: {str(exc)[:160]}") from exc

    def text(self, name: str, key: str) -> str | None:
        from eth_abi import decode
        raw = self._call(name, text_calldata(name, key))
        try:
            value = decode(["string"], raw)[0]
        except Exception as exc:
            raise ENSResolutionError(f"ENS text record {key} for {name} is malformed") from exc
        return value or None

    def addr(self, name: str) -> str | None:
        from eth_abi import decode
        raw = self._call(name, addr_calldata(name))
        try:
            value = decode(["address"], raw)[0]
        except Exception as exc:
            raise ENSResolutionError(f"ENS address record for {name} is malformed") from exc
        value = str(value).lower()
        return None if value == ZERO_ADDRESS else value

    def payout_address(self, name: str) -> str | None:
        """The x402 payout address record, lowercased, or None when unset.
        A record that is set but is not an address raises (fail closed)."""
        value = self.text(name, PAYOUT_RECORD_KEY)
        if value is None:
            return None
        value = value.strip()
        if not _ADDRESS_RE.match(value) or value.lower() == ZERO_ADDRESS:
            raise ENSResolutionError(f"ENS record {PAYOUT_RECORD_KEY} for {name} is not an address",
                                     "ENS_BAD_RECORD")
        return value.lower()
