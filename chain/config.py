"""
chain/config.py - single source of truth for network, explorer and contract
addresses.

Defaults target Ethereum Sepolia. Every value is overridable from the
environment and is read at call time, so tests (and a redeploy) only need to
change env vars:

    CHAIN_ID            default 11155111 (Ethereum Sepolia)
    CHAIN_NAME          default "Ethereum Sepolia"
    RPC_URL             default public Sepolia RPC (use a keyed Alchemy/Infura
                        URL for anything beyond light local testing)
    EXPLORER_URL        default https://sepolia.etherscan.io
    NATIVE_CURRENCY_SYMBOL / NATIVE_CURRENCY_NAME   default ETH / Sepolia Ether

    USDC_ADDRESS        payment token (default: Circle USDC on Sepolia)
    <NAME>_ADDRESS      per-contract address override, see CONTRACTS below.

Nothing in this module touches the network.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

SEPOLIA_CHAIN_ID = 11155111
SEPOLIA_NAME = "Ethereum Sepolia"
SEPOLIA_PUBLIC_RPC = "https://ethereum-sepolia-rpc.publicnode.com"
SEPOLIA_EXPLORER = "https://sepolia.etherscan.io"

# Contracts the platform knows about. `default` is the Sepolia address when
# one exists; None means "not deployed on this network yet" and every feature
# that needs it degrades gracefully (503 / DB-only).
CIRCLE_USDC_SEPOLIA = "0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238"

CONTRACTS: dict[str, dict] = {
    # Payment token. Circle's USDC on Sepolia supports EIP-3009
    # transferWithAuthorization / receiveWithAuthorization natively. Point
    # USDC_ADDRESS at a mintable EIP-3009 mock for CI and load tests only.
    "USDC":               {"env": "USDC_ADDRESS", "default": CIRCLE_USDC_SEPOLIA},
    # Canonical ERC-8004 registries on Sepolia (erc-8004/erc-8004-contracts).
    "IdentityRegistry":   {"env": "ERC8004_IDENTITY_REGISTRY",
                           "default": "0x8004A818BFB912233c491871b3d84c89A494BD9e"},
    "ReputationRegistry": {"env": "ERC8004_REPUTATION_REGISTRY",
                           "default": "0x8004B663056A597Dffe9eCcC1965A193B7388713"},
    # Legacy prototype contracts. They were only deployed on the upstream
    # prototype testnet and are superseded by ERC-8004 + EngagementEscrow
    # (roadmap), so there is no Sepolia default. Set the env var to point at
    # a redeploy.
    "AgentRegistry":      {"env": "AGENT_REGISTRY_ADDRESS", "default": None},
    "ReputationContract": {"env": "REPUTATION_ADDRESS",     "default": None},
    "StakingSlashing":    {"env": "STAKING_ADDRESS",        "default": None},
    "EscrowPayment":      {"env": "ESCROW_ADDRESS",         "default": None},
}

_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_TX_RE = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")


@dataclass(frozen=True)
class ChainConfig:
    chain_id: int
    name: str
    rpc_url: str
    explorer: str
    native_currency: dict = field(default_factory=dict)

    @property
    def chain_id_hex(self) -> str:
        return hex(self.chain_id)

    @property
    def caip2(self) -> str:
        """CAIP-2 network id, e.g. eip155:11155111 (used by x402)."""
        return f"eip155:{self.chain_id}"

    def to_frontend(self) -> dict:
        """Shape consumed by the browser (window.AGENTSLIST_CHAIN)."""
        return {
            "chainId": self.chain_id,
            "chainIdHex": self.chain_id_hex,
            "name": self.name,
            "rpcUrl": self.rpc_url,
            "explorer": self.explorer,
            "nativeCurrency": self.native_currency,
        }


def get_chain_config() -> ChainConfig:
    """Active chain configuration, read from the environment at call time."""
    raw_id = os.environ.get("CHAIN_ID", "").strip()
    try:
        chain_id = int(raw_id, 0) if raw_id else SEPOLIA_CHAIN_ID
    except ValueError:
        chain_id = SEPOLIA_CHAIN_ID
    return ChainConfig(
        chain_id=chain_id,
        name=os.environ.get("CHAIN_NAME", "").strip() or SEPOLIA_NAME,
        rpc_url=os.environ.get("RPC_URL", "").strip() or SEPOLIA_PUBLIC_RPC,
        explorer=(os.environ.get("EXPLORER_URL", "").strip() or SEPOLIA_EXPLORER).rstrip("/"),
        native_currency={
            "name": os.environ.get("NATIVE_CURRENCY_NAME", "").strip() or "Sepolia Ether",
            "symbol": os.environ.get("NATIVE_CURRENCY_SYMBOL", "").strip() or "ETH",
            "decimals": 18,
        },
    )


def get_address(name: str) -> str | None:
    """Configured address for a known contract, or None when not deployed.
    Invalid overrides are ignored rather than crashing the app."""
    spec = CONTRACTS[name]
    value = (os.environ.get(spec["env"]) or "").strip() or spec["default"]
    if value and _ADDRESS_RE.match(value):
        return value
    return None


def get_addresses(include_missing: bool = False) -> dict[str, str | None]:
    """All known contract addresses. By default only the configured ones."""
    out = {name: get_address(name) for name in CONTRACTS}
    return out if include_missing else {k: v for k, v in out.items() if v}


_EXPLORER_KINDS = {"tx", "address", "token", "block"}


def explorer_url(kind: str, value, *, base: str | None = None) -> str | None:
    """Block-explorer link for a tx hash, address, token or block number.

    >>> explorer_url("tx", "ab" * 32)   # doctest: +ELLIPSIS
    'https://sepolia.etherscan.io/tx/0xabab...'

    Returns None for empty values so templates can simply test the result.
    Transaction hashes are normalized to a 0x prefix.
    """
    if kind not in _EXPLORER_KINDS:
        raise ValueError(f"unknown explorer kind {kind!r}; expected one of {sorted(_EXPLORER_KINDS)}")
    if value is None or str(value).strip() == "":
        return None
    value = str(value).strip()
    if kind == "tx" and _TX_RE.match(value) and not value.startswith("0x"):
        value = "0x" + value
    root = (base or get_chain_config().explorer).rstrip("/")
    return f"{root}/{kind}/{value}"


def payment_recipient() -> str | None:
    """Where buyer USDC payments go until EngagementEscrow exists: the platform
    treasury (PAYMENT_RECIPIENT)."""
    value = (os.environ.get("PAYMENT_RECIPIENT") or "").strip()
    return value if _ADDRESS_RE.match(value) else None


def get_deployment() -> dict:
    """Chain + contract metadata for /api/onchain/info and /config.js."""
    cfg = get_chain_config()
    contracts = get_addresses(include_missing=True)
    return {
        "chainId": cfg.chain_id,
        "chainIdHex": cfg.chain_id_hex,
        "caip2": cfg.caip2,
        "chain": cfg.name,
        "rpcUrl": cfg.rpc_url,
        "explorer": cfg.explorer,
        "nativeCurrency": cfg.native_currency,
        "contracts": {k: v for k, v in contracts.items() if v},
        "paymentRecipient": payment_recipient(),
        "notDeployed": sorted(k for k, v in contracts.items() if not v),
    }
