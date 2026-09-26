"""
icm.py — Interchain Messaging (ICM) / Teleporter integration.

Avalanche's ICM lets a contract on one subnet send a verifiable message to
a contract on another subnet in ~1 block. The TeleporterMessenger is
deployed at the same address on every Avalanche L1:

    0x253b2784c75e510dD0fF1da844684a1aC0aa5fcf

This module wraps sendCrossChainMessage for the AgentHire use case:
a buyer on one L1 posts a bid whose settlement lives on a different L1.
The agent registry and escrow stay on Fuji C-chain (the source-of-truth
reputation surface), but payment can originate anywhere Avalanche
supports.

Reads:
- getMessageHash(blockchainId, index) for confirming receipts

Writes:
- sendCrossChainMessage(TeleporterMessageInput) for originating a bid

Gas:
- Outbound from this subnet, plus the relayer-funded delivery on the
  destination. The Teleporter docs recommend including feeInfo so the
  message can be executed without a manual relayer push.

Fuji known subnet blockchainIds (hex, 32 bytes):
- C-Chain (Fuji):  0x7fc93d85c6d62c5b2ac0b519c87010ea5294012d1e407030d6acd0021cac10d5
- Dispatch (demo): 0x9f3be606497285d0ffbb5ac9ba24aa60346a9b1812479ed66cb329f394a4b1c7
"""
from __future__ import annotations
import os
import time
from typing import Any

try:
    from web3 import Web3
    from eth_account import Account
    _WEB3_OK = True
except ImportError:
    _WEB3_OK = False
    Web3 = Account = None  # type: ignore


# ── Canonical addresses + chainIds ────────────────────────────────────────

TELEPORTER_MESSENGER = "0x253b2784c75e510dD0fF1da844684a1aC0aa5fcf"

# 32-byte Avalanche blockchain IDs (not EVM chainIds) — Teleporter addresses
# messages by blockchain ID, not by chainId.
BLOCKCHAIN_IDS = {
    "fuji-c":    "0x7fc93d85c6d62c5b2ac0b519c87010ea5294012d1e407030d6acd0021cac10d5",
    "dispatch":  "0x9f3be606497285d0ffbb5ac9ba24aa60346a9b1812479ed66cb329f394a4b1c7",
    "echo":      "0x1278d1be4b9e21bedf9e0cdc897fdd75c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0",  # placeholder
}


# Minimal ABI for TeleporterMessenger.sendCrossChainMessage + getMessageHash
TELEPORTER_ABI = [
    {
        "type": "function",
        "name": "sendCrossChainMessage",
        "stateMutability": "nonpayable",
        "inputs": [{
            "name": "messageInput", "type": "tuple",
            "components": [
                {"name": "destinationBlockchainID", "type": "bytes32"},
                {"name": "destinationAddress",     "type": "address"},
                {"name": "feeInfo", "type": "tuple", "components": [
                    {"name": "feeTokenAddress", "type": "address"},
                    {"name": "amount",          "type": "uint256"},
                ]},
                {"name": "requiredGasLimit",     "type": "uint256"},
                {"name": "allowedRelayerAddresses", "type": "address[]"},
                {"name": "message",              "type": "bytes"},
            ],
        }],
        "outputs": [{"name": "messageID", "type": "bytes32"}],
    },
    {
        "type": "function",
        "name": "getMessageHash",
        "stateMutability": "view",
        "inputs": [{"name": "messageID", "type": "bytes32"}],
        "outputs": [{"type": "bytes32"}],
    },
]


class ICM:
    """Send cross-chain messages via Avalanche's Teleporter."""

    def __init__(self, rpc_url: str, private_key: str | None):
        if not _WEB3_OK:
            raise RuntimeError("web3/eth_account not installed")
        self.w3 = Web3(Web3.HTTPProvider(rpc_url))
        self.account = Account.from_key(private_key) if private_key else None
        self.messenger = self.w3.eth.contract(
            address=Web3.to_checksum_address(TELEPORTER_MESSENGER),
            abi=TELEPORTER_ABI,
        )

    @classmethod
    def from_env(cls) -> "ICM":
        return cls(
            rpc_url=os.environ.get("RPC_URL", "https://api.avax-test.network/ext/bc/C/rpc"),
            private_key=os.environ.get("FACILITATOR_PRIVATE_KEY") or os.environ.get("PRIVATE_KEY"),
        )

    # ── writes ──────────────────────────────────────────────────────────

    def send_message(
        self,
        destination_blockchain: str,
        destination_address: str,
        payload: bytes,
        *,
        required_gas_limit: int = 500_000,
    ) -> dict:
        """Send an arbitrary bytes payload to `destination_address` on
        `destination_blockchain` (one of BLOCKCHAIN_IDS keys or a raw 0x-hex id).
        """
        if not self.account:
            raise RuntimeError("FACILITATOR_PRIVATE_KEY not set — cannot sign ICM tx")
        dest_id = BLOCKCHAIN_IDS.get(destination_blockchain, destination_blockchain)
        dest_bytes = bytes.fromhex(dest_id.replace("0x", ""))
        assert len(dest_bytes) == 32, "destinationBlockchainID must be 32 bytes"

        message_input = (
            dest_bytes,                                     # destinationBlockchainID
            Web3.to_checksum_address(destination_address),  # destinationAddress
            (
                Web3.to_checksum_address("0x0000000000000000000000000000000000000000"),
                0,
            ),                                              # feeInfo (none — relayer-funded)
            int(required_gas_limit),
            [],                                             # allowedRelayerAddresses (any)
            payload,
        )

        tx = self.messenger.functions.sendCrossChainMessage(message_input).build_transaction({
            "from": self.account.address,
            "nonce": self.w3.eth.get_transaction_count(self.account.address),
            "chainId": self.w3.eth.chain_id,
            "gas": 1_200_000,
            "gasPrice": self.w3.eth.gas_price,
        })
        signed = self.account.sign_transaction(tx)
        h = self.w3.eth.send_raw_transaction(signed.rawTransaction)
        receipt = self.w3.eth.wait_for_transaction_receipt(h)
        # Parse the messageID from the logs (first topic of SendCrossChainMessage)
        message_id = None
        for log in receipt["logs"]:
            if log["address"].lower() == TELEPORTER_MESSENGER.lower():
                if len(log["topics"]) >= 2:
                    message_id = "0x" + log["topics"][1].hex()
                    break
        return {
            "txHash":   h.hex(),
            "messageID": message_id,
            "destination": destination_blockchain,
            "destinationAddress": destination_address,
            "payloadBytes": len(payload),
            "snowtrace": f"https://testnet.snowtrace.io/tx/{h.hex()}",
        }

    def encode_bid_message(self, buyer: str, agent_id: int, token_budget: int,
                           max_price_per_token: int, category_id: int) -> bytes:
        """ABI-encode an AgentHire cross-chain bid: (address, uint256, uint256,
        uint256, uint256). The receiving contract on the destination subnet
        reads this format and forwards into AuctionMarket."""
        from eth_abi import encode
        return encode(
            ["address", "uint256", "uint256", "uint256", "uint256"],
            [Web3.to_checksum_address(buyer), int(agent_id),
             int(token_budget), int(max_price_per_token), int(category_id)],
        )
