"""
chain/erc8004.py - read-only client for the canonical ERC-8004 registries.

ERC-8004 ("Trustless Agents") gives every agent an ERC-721 identity in the
IdentityRegistry whose token URI points at the agent's registration file
(agentURI), plus a ReputationRegistry for feedback. Canonical deployments on
Ethereum Sepolia are configured in chain.config.

This module only covers the ERC-721 surface of the IdentityRegistry, which is
stable across registry versions. Registration, feedback and reputation
aggregation land with roadmap commit 9 (see docs/ROADMAP.md).

It replaces AgentHire's adapter, which mapped invented selectors
(getIdentity/getScore/getReputation) onto its own custom contracts.
"""
from __future__ import annotations

from chain.config import explorer_url, get_address

IDENTITY_ABI = [
    {"type": "function", "name": "ownerOf", "stateMutability": "view",
     "inputs": [{"name": "tokenId", "type": "uint256"}], "outputs": [{"type": "address"}]},
    {"type": "function", "name": "tokenURI", "stateMutability": "view",
     "inputs": [{"name": "tokenId", "type": "uint256"}], "outputs": [{"type": "string"}]},
    {"type": "function", "name": "balanceOf", "stateMutability": "view",
     "inputs": [{"name": "owner", "type": "address"}], "outputs": [{"type": "uint256"}]},
]


class ERC8004Identity:
    """Reads an agent identity (owner + agentURI) from the IdentityRegistry."""

    def __init__(self, w3, address: str | None = None):
        from web3 import Web3
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
