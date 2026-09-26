"""
chain/client.py - Python-native web3 client for the platform's on-chain calls.

    from chain.client import OnChain
    oc = OnChain.from_env()
    oc.x402_execute(signed_permit)          # submit a buyer-signed EIP-3009 USDC transfer
    oc.submit_incident(agent_id, user, 1)   # gatekeeper-signed incident (legacy contract)

Network and addresses come from chain.config (Ethereum Sepolia by default).
Constructing the client never touches the network; calls that need a contract
that is not deployed on the active chain raise ContractNotConfigured, which
routes turn into a 503.

Env:
    FACILITATOR_PRIVATE_KEY  pays gas to submit buyer-signed authorizations
    GATEKEEPER_PRIVATE_KEY   signs incidents (legacy ReputationContract)
    MIN_SIGNER_ETH_RESERVE   ETH floor the signer must keep (default 0.01)
    PRIORITY_FEE_GWEI        fallback EIP-1559 tip when the node can't suggest one
    MAX_FEE_GWEI             optional cap on maxFeePerGas
    RPC_TIMEOUT_SECONDS      HTTP timeout for RPC calls (default 10)

Each signer has its own key; there is deliberately no shared PRIVATE_KEY
fallback.
"""
from __future__ import annotations

import os
from typing import Any

from chain.config import explorer_url, get_address, get_chain_config

try:
    from eth_account import Account
    from eth_account.messages import encode_defunct
    from web3 import Web3
except ImportError:  # the app still boots; chain routes return 503
    Web3 = None


class ContractNotConfigured(RuntimeError):
    """The contract needed for this call has no address on the active chain."""


def _gwei_env(name: str, default: float | None) -> float | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def eip1559_fees(w3) -> dict:
    """EIP-1559 fee fields (type 2).

    maxPriorityFeePerGas comes from eth_maxPriorityFeePerGas (fallback
    PRIORITY_FEE_GWEI, default 1.5 gwei); maxFeePerGas = 2 x latest base fee
    + priority, which survives several full blocks of base-fee growth. Sepolia
    fees spike, so MAX_FEE_GWEI optionally caps the total. Chains without a
    base fee fall back to a legacy gasPrice.
    """
    block = w3.eth.get_block("latest")
    base_fee = block.get("baseFeePerGas") if hasattr(block, "get") else getattr(block, "baseFeePerGas", None)
    if base_fee is None:
        return {"gasPrice": int(w3.eth.gas_price)}
    try:
        priority = int(w3.eth.max_priority_fee)
    except Exception:
        priority = int(Web3.to_wei(_gwei_env("PRIORITY_FEE_GWEI", 1.5), "gwei"))
    max_fee = 2 * int(base_fee) + priority
    cap = _gwei_env("MAX_FEE_GWEI", None)
    if cap is not None:
        cap_wei = int(Web3.to_wei(cap, "gwei"))
        max_fee = min(max_fee, cap_wei)
        priority = min(priority, max_fee)
    return {"type": 2, "maxFeePerGas": max_fee, "maxPriorityFeePerGas": priority}


def _reserve_eth() -> float:
    try:
        return float(os.environ.get("MIN_SIGNER_ETH_RESERVE", "0.01"))
    except ValueError:
        return 0.01


# Minimal ABIs - only what this module calls.
ABI: dict[str, list] = {
    "USDC": [
        {"type": "function", "name": "transferWithAuthorization", "stateMutability": "nonpayable",
         "inputs": [{"name": "from", "type": "address"}, {"name": "to", "type": "address"},
                    {"name": "value", "type": "uint256"}, {"name": "validAfter", "type": "uint256"},
                    {"name": "validBefore", "type": "uint256"}, {"name": "nonce", "type": "bytes32"},
                    {"name": "v", "type": "uint8"}, {"name": "r", "type": "bytes32"}, {"name": "s", "type": "bytes32"}],
         "outputs": []},
        {"type": "function", "name": "balanceOf", "stateMutability": "view",
         "inputs": [{"name": "a", "type": "address"}], "outputs": [{"type": "uint256"}]},
    ],
    "EscrowPayment": [
        {"type": "function", "name": "getSession", "stateMutability": "view",
         "inputs": [{"name": "sid", "type": "uint256"}],
         "outputs": [{"components": [
             {"name": "agentId", "type": "uint256"}, {"name": "user", "type": "address"},
             {"name": "totalDeposit", "type": "uint256"}, {"name": "tokenBudget", "type": "uint256"},
             {"name": "pricePerToken", "type": "uint256"}, {"name": "categoryId", "type": "uint256"},
             {"name": "expiresAt", "type": "uint64"}, {"name": "settled", "type": "bool"},
             {"name": "cancelled", "type": "bool"}], "type": "tuple"}]},
        {"type": "function", "name": "cancelSession", "stateMutability": "nonpayable",
         "inputs": [{"name": "sid", "type": "uint256"}], "outputs": []},
    ],
    "ReputationContract": [
        {"type": "function", "name": "submitIncident", "stateMutability": "nonpayable",
         "inputs": [{"name": "agentId", "type": "uint256"}, {"name": "affectedUser", "type": "address"},
                    {"name": "severity", "type": "uint8"}, {"name": "gatekeeperSignature", "type": "bytes"}],
         "outputs": []},
    ],
    "AgentRegistry": [
        {"type": "function", "name": "registerAgent", "stateMutability": "nonpayable",
         "inputs": [{"name": "wallet", "type": "address"}, {"name": "name", "type": "string"},
                    {"name": "endpointURL", "type": "string"}],
         "outputs": [{"type": "uint256"}]},
    ],
}

# Contract name in ABI -> address lookup in chain.config.
_ADDRESS_KEYS = {
    "USDC": "USDC",
    "EscrowPayment": "EscrowPayment",
    "ReputationContract": "ReputationContract",
    "AgentRegistry": "AgentRegistry",
}


def _hex(h) -> str:
    s = h.hex() if hasattr(h, "hex") else str(h)
    return s if s.startswith("0x") else "0x" + s


class OnChain:
    """web3 binding to the configured chain. Cheap to construct, lazy to use."""

    def __init__(self, rpc_url: str, facilitator_pk: str | None, gatekeeper_pk: str | None,
                 *, timeout: float = 10.0):
        if Web3 is None:
            raise RuntimeError("web3 not installed; pip install web3 eth-account")
        self.chain = get_chain_config()
        self.w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": timeout}))
        self.facilitator = Account.from_key(facilitator_pk) if facilitator_pk else None
        self.gatekeeper = Account.from_key(gatekeeper_pk) if gatekeeper_pk else None
        self._contracts: dict[str, Any] = {}

    @classmethod
    def from_env(cls) -> "OnChain":
        try:
            timeout = float(os.environ.get("RPC_TIMEOUT_SECONDS", "10"))
        except ValueError:
            timeout = 10.0
        return cls(
            rpc_url=get_chain_config().rpc_url,
            facilitator_pk=os.environ.get("FACILITATOR_PRIVATE_KEY") or None,
            gatekeeper_pk=os.environ.get("GATEKEEPER_PRIVATE_KEY") or None,
            timeout=timeout,
        )

    # ── contracts ──────────────────────────────────────────────────────────
    def has_contract(self, name: str) -> bool:
        return bool(self._address(name))

    def _address(self, name: str) -> str | None:
        return get_address(_ADDRESS_KEYS[name])

    def contract(self, name: str):
        """web3 contract for `name`, or ContractNotConfigured."""
        addr = self._address(name)
        if not addr:
            raise ContractNotConfigured(
                f"{name} is not deployed on {self.chain.name}; set its *_ADDRESS env var")
        cached = self._contracts.get(name)
        if cached is None or cached.address.lower() != addr.lower():
            cached = self.w3.eth.contract(address=Web3.to_checksum_address(addr), abi=ABI[name])
            self._contracts[name] = cached
        return cached

    # ── reads ──────────────────────────────────────────────────────────────
    def get_session(self, session_id: int) -> dict:
        s = self.contract("EscrowPayment").functions.getSession(int(session_id)).call()
        return {
            "sessionId": str(session_id), "agentId": str(s[0]), "user": s[1],
            "totalDeposit": str(s[2]), "tokenBudget": str(s[3]), "pricePerToken": str(s[4]),
            "categoryId": str(s[5]), "expiresAt": int(s[6]), "settled": s[7], "cancelled": s[8],
        }

    # ── writes ─────────────────────────────────────────────────────────────
    def register_agent(self, wallet: str, name: str, endpoint_url: str) -> dict:
        """Register on the legacy AgentRegistry (replaced by ERC-8004 on the roadmap)."""
        self._require_facilitator()
        reg = self.contract("AgentRegistry")
        tx = reg.functions.registerAgent(
            Web3.to_checksum_address(wallet), name, endpoint_url
        ).build_transaction(self._tx_params())
        h = self._sign_send(tx, self.facilitator)
        receipt = self.w3.eth.wait_for_transaction_receipt(h)
        agent_id = None
        for log in receipt["logs"]:
            if log["address"].lower() == reg.address.lower() and len(log["topics"]) >= 2:
                agent_id = int(_hex(log["topics"][1]), 16)
                break
        return {"agentId": str(agent_id) if agent_id is not None else None,
                "txHash": _hex(h), "explorer": explorer_url("tx", _hex(h))}

    def cancel_session(self, session_id: int) -> dict:
        self._require_facilitator()
        escrow = self.contract("EscrowPayment")
        tx = escrow.functions.cancelSession(int(session_id)).build_transaction(self._tx_params())
        h = self._sign_send(tx, self.facilitator)
        self.w3.eth.wait_for_transaction_receipt(h)
        return {"sessionId": str(session_id), "status": "cancelled",
                "txHash": _hex(h), "explorer": explorer_url("tx", _hex(h))}

    def x402_execute(self, p: dict) -> dict:
        """Submit a buyer-signed EIP-3009 transferWithAuthorization. The permit
        itself moves `value` from the buyer to `to`; the facilitator only pays gas."""
        self._require_facilitator()
        token = self.contract("USDC")
        tx = token.functions.transferWithAuthorization(
            Web3.to_checksum_address(p["from"]),
            Web3.to_checksum_address(p["to"]),
            int(p["value"]),
            int(p.get("validAfter", 0)),
            int(p["validBefore"]),
            bytes.fromhex(str(p["nonce"]).replace("0x", "")),
            int(p["v"]),
            bytes.fromhex(str(p["r"]).replace("0x", "")),
            bytes.fromhex(str(p["s"]).replace("0x", "")),
        ).build_transaction(self._tx_params())
        h = self._sign_send(tx, self.facilitator)
        receipt = self.w3.eth.wait_for_transaction_receipt(h)
        if receipt.status != 1:
            raise RuntimeError(f"transferWithAuthorization reverted tx={_hex(h)}")
        return {
            "sessionId": None,
            "agentId": str(int(p.get("agentId", 0))),
            "status": "settled",
            "txHashes": {"permit": _hex(h)},
            "explorer": explorer_url("tx", _hex(h)),
        }

    def submit_incident(self, agent_id: int, affected_user: str, severity: int) -> dict:
        """Gatekeeper-signed incident on the legacy ReputationContract."""
        if not self.gatekeeper:
            raise RuntimeError("GATEKEEPER_PRIVATE_KEY not set")
        if severity not in (1, 2):
            raise ValueError("severity must be 1 or 2")
        rep = self.contract("ReputationContract")
        # Must match Solidity: keccak256(abi.encode(chainid, address(this), agentId,
        # affectedUser, severity)) -> toEthSignedMessageHash -> ECDSA.recover.
        from eth_abi import encode
        inner = self.w3.keccak(encode(
            ["uint256", "address", "uint256", "address", "uint8"],
            [self.chain.chain_id, Web3.to_checksum_address(rep.address),
             int(agent_id), Web3.to_checksum_address(affected_user), int(severity)],
        ))
        signed = self.gatekeeper.sign_message(encode_defunct(primitive=inner))
        tx = rep.functions.submitIncident(
            int(agent_id), Web3.to_checksum_address(affected_user), int(severity), signed.signature
        ).build_transaction(self._tx_params(sender=self.gatekeeper.address))
        h = self._sign_send(tx, self.gatekeeper)
        self.w3.eth.wait_for_transaction_receipt(h)
        return {"status": "signed", "txHash": _hex(h), "explorer": explorer_url("tx", _hex(h))}

    # ── helpers ────────────────────────────────────────────────────────────
    def _require_facilitator(self) -> None:
        if not self.facilitator:
            raise RuntimeError("FACILITATOR_PRIVATE_KEY not set")

    def _tx_params(self, sender: str | None = None, nonce: int | None = None) -> dict:
        addr = Web3.to_checksum_address(sender or self.facilitator.address)
        return {
            "from": addr,
            "nonce": nonce if nonce is not None else self.w3.eth.get_transaction_count(addr, "pending"),
            "chainId": self.chain.chain_id,
            **eip1559_fees(self.w3),
        }

    def _sign_send(self, tx: dict, signer) -> bytes:
        if "gas" not in tx:
            try:
                tx["gas"] = int(self.w3.eth.estimate_gas(tx) * 1.2)
            except Exception:
                tx["gas"] = 500_000
        self._assert_signer_reserve(tx, signer.address)
        signed = signer.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
        return self.w3.eth.send_raw_transaction(raw)

    def _assert_signer_reserve(self, tx: dict, signer_address: str) -> None:
        """Refuse to send if gas + value would take the signer below the ETH floor."""
        reserve = _reserve_eth()
        reserve_wei = self.w3.to_wei(reserve, "ether")
        balance_wei = int(self.w3.eth.get_balance(signer_address))
        fee_per_gas = int(tx.get("maxFeePerGas") or tx.get("gasPrice") or 0)
        required_wei = int(tx.get("gas", 0)) * fee_per_gas + int(tx.get("value", 0)) + reserve_wei
        if balance_wei < required_wei:
            short = float(self.w3.from_wei(required_wei - balance_wei, "ether"))
            raise RuntimeError(
                f"Insufficient ETH for signer {signer_address}: need +{short:.6f} ETH "
                f"to keep the {reserve:.3f} ETH reserve floor."
            )

