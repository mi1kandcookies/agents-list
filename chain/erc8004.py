"""
chain/erc8004.py - read-only client for the canonical ERC-8004 registries.

ERC-8004 ("Trustless Agents") gives every agent an ERC-721 identity in the
IdentityRegistry whose token URI points at the agent's registration file
(agentURI), plus a ReputationRegistry for feedback. Canonical deployments on
Ethereum Sepolia are configured in chain.config.

This module covers the ERC-721 reads and the canonical ``register(string)`` /
``setAgentURI`` writes used by the explicit Sepolia profile-registration
script. It does not deploy or replace the protocol-owned registry.

It follows the canonical ERC-8004 registry interfaces.
"""
from __future__ import annotations

import os

from chain.config import explorer_url, get_address

IDENTITY_ABI = [
    {"type": "function", "name": "register", "stateMutability": "nonpayable",
     "inputs": [{"name": "agentURI", "type": "string"}],
     "outputs": [{"name": "agentId", "type": "uint256"}]},
    {"type": "function", "name": "setAgentURI", "stateMutability": "nonpayable",
     "inputs": [{"name": "agentId", "type": "uint256"}, {"name": "newURI", "type": "string"}],
     "outputs": []},
    {"type": "function", "name": "ownerOf", "stateMutability": "view",
     "inputs": [{"name": "tokenId", "type": "uint256"}], "outputs": [{"type": "address"}]},
    {"type": "function", "name": "tokenURI", "stateMutability": "view",
     "inputs": [{"name": "tokenId", "type": "uint256"}], "outputs": [{"type": "string"}]},
    {"type": "function", "name": "balanceOf", "stateMutability": "view",
     "inputs": [{"name": "owner", "type": "address"}], "outputs": [{"type": "uint256"}]},
]


def _ensure_eth_reserve(w3, tx: dict, address: str) -> None:
    """Refuse a registration that would consume the operator's safety floor."""
    from web3 import Web3
    try:
        reserve = float(os.environ.get("MIN_SIGNER_ETH_RESERVE", "0.01"))
    except ValueError:
        reserve = 0.01
    fee = int(tx.get("gas", 0)) * int(tx.get("maxFeePerGas") or tx.get("gasPrice") or 0)
    required = fee + Web3.to_wei(reserve, "ether")
    balance = int(w3.eth.get_balance(address))
    if balance < required:
        raise RuntimeError(f"operator needs more Sepolia ETH to keep the {reserve:g} ETH reserve")


class ERC8004Identity:
    """Reads an agent identity (owner + agentURI) from the IdentityRegistry."""

    def __init__(self, w3, address: str | None = None):
        from web3 import Web3
        self.w3 = w3
        self.address = address or get_address("IdentityRegistry")
        if not self.address:
            raise RuntimeError("ERC-8004 IdentityRegistry is not configured for this chain")
        self.contract = w3.eth.contract(address=Web3.to_checksum_address(self.address), abi=IDENTITY_ABI)

    def get_identity(self, agent_id: int) -> dict:
        fn = self.contract.functions
        return {
            "agentId": int(agent_id),
            "owner": fn.ownerOf(int(agent_id)).call(),
            "agentURI": fn.tokenURI(int(agent_id)).call(),
            "registry": self.address,
            "explorer": explorer_url("token", f"{self.address}?a={int(agent_id)}"),
        }

    def agents_owned(self, owner: str) -> int:
        from web3 import Web3
        return int(self.contract.functions.balanceOf(Web3.to_checksum_address(owner)).call())

    def register(self, signer, agent_uri: str, *, wait: bool = True) -> dict:
        """Mint one identity NFT from the canonical registry.

        This is intentionally a small signer adapter. The caller must opt into
        live writes and provide the operator; no private key is accepted via a
        function argument exposed to the web app.
        """
        from chain.client import eip1559_fees
        from chain.config import get_chain_config
        from web3 import Web3
        if not isinstance(agent_uri, str) or not agent_uri:
            raise ValueError("agent_uri is required")
        chain = get_chain_config()
        tx = self.contract.functions.register(agent_uri).build_transaction({
            "from": Web3.to_checksum_address(signer.address),
            "nonce": self.w3.eth.get_transaction_count(signer.address, "pending"),
            "chainId": chain.chain_id,
            **eip1559_fees(self.contract.w3),
        })
        tx["gas"] = int(self.w3.eth.estimate_gas(tx) * 1.2)
        _ensure_eth_reserve(self.w3, tx, signer.address)
        signed = signer.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
        tx_hash = self.w3.eth.send_raw_transaction(raw)
        if not wait:
            return {"tx_hash": tx_hash.hex(), "agent_id": None}
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash)
        if receipt.status != 1:
            raise RuntimeError(f"ERC-8004 registration reverted: {tx_hash.hex()}")
        event_abi = {
            "anonymous": False, "inputs": [
                {"indexed": True, "name": "agentId", "type": "uint256"},
                {"indexed": False, "name": "agentURI", "type": "string"},
                {"indexed": True, "name": "owner", "type": "address"}],
            "name": "Registered", "type": "event",
        }
        event = self.w3.eth.contract(address=self.address, abi=[event_abi]).events.Registered()
        decoded = event.process_receipt(receipt)
        agent_id = int(decoded[0]["args"]["agentId"]) if decoded else None
        return {"tx_hash": tx_hash.hex(), "agent_id": agent_id}

    def set_agent_uri(self, signer, agent_id: int, agent_uri: str, *, wait: bool = True) -> str:
        """Update an identity URI owned by ``signer`` and return its tx hash."""
        from chain.client import eip1559_fees
        from chain.config import get_chain_config
        from web3 import Web3
        chain = get_chain_config()
        tx = self.contract.functions.setAgentURI(int(agent_id), agent_uri).build_transaction({
            "from": Web3.to_checksum_address(signer.address),
            "nonce": self.w3.eth.get_transaction_count(signer.address, "pending"),
            "chainId": chain.chain_id,
            **eip1559_fees(self.contract.w3),
        })
        tx["gas"] = int(self.w3.eth.estimate_gas(tx) * 1.2)
        _ensure_eth_reserve(self.w3, tx, signer.address)
        signed = signer.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
        tx_hash = self.w3.eth.send_raw_transaction(raw)
        if wait:
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash)
            if receipt.status != 1:
                raise RuntimeError(f"ERC-8004 URI update reverted: {tx_hash.hex()}")
        return tx_hash.hex()
