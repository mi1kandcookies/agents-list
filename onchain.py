"""
onchain.py - Python-native on-chain layer for Agent's List.

Lets the Flask app talk to Avalanche Fuji without a separate Node service.
Requires: pip install web3 eth-account requests

Usage from app.py:
    from onchain import OnChain
    chain = OnChain.from_env()
    session = chain.get_session(session_id)
    chain.x402_execute(signed_payload)  # executes EIP-3009 + depositFunds
    chain.submit_incident(agent_id, user, severity)  # gatekeeper sig + submit

Env variables:
    RPC_URL                  (default Fuji)
    CHAIN_ID                 (default 43113 Fuji)
    CHAIN_NAME               human-readable chain label
    EXPLORER_URL             block explorer base URL
    FACILITATOR_PRIVATE_KEY  (pays gas for x402 flow)
    GATEKEEPER_PRIVATE_KEY   (signs incidents - must match on-chain GATEKEEPER_ADDRESS)

    <CONTRACT>_ADDRESS       per-contract override for any redeploy:
                             MOCK_USDC_ADDRESS, AGENT_REGISTRY_ADDRESS,
                             REPUTATION_ADDRESS, STAKING_ADDRESS,
                             ESCROW_ADDRESS.
                             Any unset value falls back to the Fuji defaults
                             below - so redeploying one contract doesn't
                             force you to re-set the rest.

This module is stateless and safe to import multiple times.
"""

from __future__ import annotations
import os
import time
from typing import Any

try:
    from web3 import Web3
    from eth_account import Account
    from eth_account.messages import encode_defunct
except ImportError:
    Web3 = None  # Flask can still boot; onchain routes will 503.

# Default values - Avalanche Fuji testnet with the prototype deployment.
# Every single one of these is overridable via env vars; new deployers only
# need to export the contract addresses they redeployed + (optionally) a
# different RPC / chain id.
FUJI_RPC = "https://api.avax-test.network/ext/C/rpc"
_DEFAULT_CHAIN_ID = 43113
_DEFAULT_CHAIN_NAME = "Avalanche Fuji"
# Ava Labs' own explorer indexes Fuji txs in real time — testnet Snowtrace
# routinely lags by 30-60 seconds. The path prefix is c-chain so we can append
# `/tx/{hash}` or `/address/{addr}` exactly like Snowtrace.
_DEFAULT_EXPLORER = "https://subnets-test.avax.network/c-chain"

_DEFAULT_ADDRESSES = {
    "MockUSDC":           "0x9C49D730Dfb82B7663aBE6069B5bFe867fa34c9f",
    "AgentRegistry":      "0x6B71b84Fa3C313ccC43D63A400Ab47e6A0d4BCbB",
    "ReputationContract": "0x40ef89Ce1E248Df00AF6Dc37f96BBf92A9Bf603A",
    "StakingSlashing":    "0xfc942b4d1Eb363F25886b3F5935394BD4932B896",
    "EscrowPayment":      "0xD19990C7CB8C386fa865135Ce9706A5A37A3f2f2",
}

# Map from ADDRESSES key → canonical env var name. Keep this in sync with
# .env.example and docs so redeploys stay self-service.
_ADDRESS_ENV = {
    "MockUSDC":           "MOCK_USDC_ADDRESS",
    "AgentRegistry":      "AGENT_REGISTRY_ADDRESS",
    "ReputationContract": "REPUTATION_ADDRESS",
    "StakingSlashing":    "STAKING_ADDRESS",
    "EscrowPayment":      "ESCROW_ADDRESS",
}

# Resolve every address once at import, allowing env overrides per-contract.
ADDRESSES = {
    name: (os.environ.get(_ADDRESS_ENV[name]) or _DEFAULT_ADDRESSES[name]).strip()
    for name in _DEFAULT_ADDRESSES
}

CHAIN_ID = int(os.environ.get("CHAIN_ID", _DEFAULT_CHAIN_ID))
CHAIN_NAME = os.environ.get("CHAIN_NAME", _DEFAULT_CHAIN_NAME)
EXPLORER_URL = os.environ.get("EXPLORER_URL", _DEFAULT_EXPLORER).rstrip("/")
# Keep a small AVAX reserve so automated writes cannot drain the signer.
MIN_SIGNER_AVAX_RESERVE = float(os.environ.get("MIN_SIGNER_AVAX_RESERVE", "0.25"))


def get_deployment() -> dict:
    """Return the active on-chain deployment config.

    All values reflect the current process environment at call time so tests
    (and `/api/onchain/info`) can override via monkey-patching ``os.environ``
    between requests.
    """
    chain_id = int(os.environ.get("CHAIN_ID", _DEFAULT_CHAIN_ID))
    return {
        "chainId": chain_id,
        "chainIdHex": hex(chain_id),
        "chain": os.environ.get("CHAIN_NAME", _DEFAULT_CHAIN_NAME),
        "rpcUrl": os.environ.get("RPC_URL", FUJI_RPC),
        "explorer": os.environ.get("EXPLORER_URL", _DEFAULT_EXPLORER).rstrip("/"),
        "contracts": {
            name: (os.environ.get(_ADDRESS_ENV[name]) or _DEFAULT_ADDRESSES[name]).strip()
            for name in _DEFAULT_ADDRESSES
        },
    }

# Minimal ABIs - only what this module calls.
ABI = {
    "MockUSDC": [
        {"type":"function","name":"transferWithAuthorization","stateMutability":"nonpayable",
         "inputs":[{"name":"from","type":"address"},{"name":"to","type":"address"},
                   {"name":"value","type":"uint256"},{"name":"validAfter","type":"uint256"},
                   {"name":"validBefore","type":"uint256"},{"name":"nonce","type":"bytes32"},
                   {"name":"v","type":"uint8"},{"name":"r","type":"bytes32"},{"name":"s","type":"bytes32"}],
         "outputs":[]},
        {"type":"function","name":"approve","stateMutability":"nonpayable",
         "inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],
         "outputs":[{"type":"bool"}]},
        {"type":"function","name":"balanceOf","stateMutability":"view",
         "inputs":[{"name":"a","type":"address"}],"outputs":[{"type":"uint256"}]},
    ],
    "EscrowPayment": [
        {"type":"function","name":"depositFunds","stateMutability":"nonpayable",
         "inputs":[{"name":"agentId","type":"uint256"},{"name":"depositAmount","type":"uint256"},
                   {"name":"tokenBudget","type":"uint256"},{"name":"categoryId","type":"uint256"},
                   {"name":"expiresAt","type":"uint64"}],
         "outputs":[{"type":"uint256"}]},
        {"type":"function","name":"getSession","stateMutability":"view",
         "inputs":[{"name":"sid","type":"uint256"}],
         "outputs":[{"components":[
             {"name":"agentId","type":"uint256"},{"name":"user","type":"address"},
             {"name":"totalDeposit","type":"uint256"},{"name":"tokenBudget","type":"uint256"},
             {"name":"pricePerToken","type":"uint256"},{"name":"categoryId","type":"uint256"},
             {"name":"expiresAt","type":"uint64"},{"name":"settled","type":"bool"},
             {"name":"cancelled","type":"bool"}],"type":"tuple"}]},
    ],
    "ReputationContract": [
        {"type":"function","name":"submitIncident","stateMutability":"nonpayable",
         "inputs":[{"name":"agentId","type":"uint256"},{"name":"affectedUser","type":"address"},
                   {"name":"severity","type":"uint8"},{"name":"gatekeeperSignature","type":"bytes"}],
         "outputs":[]},
        {"type":"function","name":"getCreditProfile","stateMutability":"view",
         "inputs":[{"name":"agentId","type":"uint256"}],
         "outputs":[{"name":"score","type":"uint256"},{"name":"tier","type":"uint8"},
                    {"name":"tasksCompleted","type":"uint256"},{"name":"incidentCount","type":"uint256"},
                    {"name":"lastDecayTs","type":"uint64"},{"name":"projectedScore","type":"uint256"}]},
        {"type":"function","name":"GATEKEEPER_ADDRESS","stateMutability":"view",
         "inputs":[],"outputs":[{"type":"address"}]},
    ],
    "AgentRegistry": [
        {"type":"function","name":"getAgent","stateMutability":"view",
         "inputs":[{"name":"id","type":"uint256"}],
         "outputs":[{"components":[
             {"name":"agentId","type":"uint256"},{"name":"wallet","type":"address"},
             {"name":"name","type":"string"},{"name":"endpointURL","type":"string"},
             {"name":"registeredAt","type":"uint256"},{"name":"active","type":"bool"},
             {"name":"banned","type":"bool"}],"type":"tuple"}]},
        {"type":"function","name":"getListing","stateMutability":"view",
         "inputs":[{"name":"id","type":"uint256"}],
         "outputs":[{"components":[
             {"name":"minPricePerToken","type":"uint256"},{"name":"maxTokensPerSession","type":"uint256"},
             {"name":"acceptingWork","type":"bool"},{"name":"nonce","type":"uint256"}],"type":"tuple"}]},
        {"type":"function","name":"registerAgent","stateMutability":"nonpayable",
         "inputs":[{"name":"wallet","type":"address"},{"name":"name","type":"string"},
                   {"name":"endpointURL","type":"string"}],
         "outputs":[{"type":"uint256"}]},
    ],
    "StakingSlashing": [
        {"type":"function","name":"getStake","stateMutability":"view",
         "inputs":[{"name":"id","type":"uint256"}],
         "outputs":[{"type":"uint256"},{"type":"uint256"},{"type":"bool"}]},
        {"type":"function","name":"getUnstakeRequest","stateMutability":"view",
         "inputs":[{"name":"id","type":"uint256"}],
         "outputs":[{"name":"amount","type":"uint256"},{"name":"availableAt","type":"uint256"}]},
        {"type":"function","name":"stake","stateMutability":"nonpayable",
         "inputs":[{"name":"agentId","type":"uint256"},{"name":"amount","type":"uint256"}],
         "outputs":[]},
        {"type":"function","name":"requestUnstake","stateMutability":"nonpayable",
         "inputs":[{"name":"agentId","type":"uint256"},{"name":"amount","type":"uint256"}],
         "outputs":[]},
        {"type":"function","name":"completeUnstake","stateMutability":"nonpayable",
         "inputs":[{"name":"agentId","type":"uint256"},{"name":"recipient","type":"address"}],
         "outputs":[]},
    ],
}


class OnChain:
    """Python-native binding to the deployed prototype contracts."""

    def __init__(self, rpc_url: str, facilitator_pk: str | None, gatekeeper_pk: str | None):
        if Web3 is None:
            raise RuntimeError("web3 not installed; pip install web3 eth-account")
        self.w3 = Web3(Web3.HTTPProvider(rpc_url))
        self.facilitator = Account.from_key(facilitator_pk) if facilitator_pk else None
        self.gatekeeper = Account.from_key(gatekeeper_pk) if gatekeeper_pk else None
        self._contracts: dict[str, Any] = {
            name: self.w3.eth.contract(address=Web3.to_checksum_address(ADDRESSES[name]), abi=ABI[name])
            for name in ABI
        }

    @classmethod
    def from_env(cls):
        return cls(
            rpc_url=os.environ.get("RPC_URL", FUJI_RPC),
            facilitator_pk=os.environ.get("FACILITATOR_PRIVATE_KEY") or os.environ.get("PRIVATE_KEY"),
            gatekeeper_pk=os.environ.get("GATEKEEPER_PRIVATE_KEY") or os.environ.get("PRIVATE_KEY"),
        )

    # ── Reads ────────────────────────────────────────────────────────────────
    def get_session(self, session_id: int) -> dict:
        s = self._contracts["EscrowPayment"].functions.getSession(int(session_id)).call()
        return {
            "sessionId": str(session_id),
            "agentId": str(s[0]),
            "user": s[1],
            "totalDeposit": str(s[2]),
            "tokenBudget": str(s[3]),
            "pricePerToken": str(s[4]),
            "categoryId": str(s[5]),
            "expiresAt": int(s[6]),
            "settled": s[7],
            "cancelled": s[8],
        }

    def get_credit_profile(self, agent_id: int) -> dict:
        p = self._contracts["ReputationContract"].functions.getCreditProfile(int(agent_id)).call()
        return {
            "score": p[0], "tier": p[1], "tasksCompleted": p[2],
            "incidentCount": p[3], "lastDecayTs": p[4], "projectedScore": p[5],
        }

    def get_stake(self, agent_id: int) -> dict:
        s = self._contracts["StakingSlashing"].functions.getStake(int(agent_id)).call()
        ur = self._contracts["StakingSlashing"].functions.getUnstakeRequest(int(agent_id)).call()
        return {
            "stakedUSDC": str(s[0]),
            "incidentCount": int(s[1]),
            "banned": s[2],
            "unstakeRequest": {
                "amount": str(ur[0]),
                "availableAt": int(ur[1]),
            },
        }

    def stake_for_agent(self, agent_id: int, amount_micro: int) -> dict:
        """Facilitator-signed stake deposit for an agent. Used by the
        /api/seller/bond route so demo-wallet users can bond without a
        client-side signer."""
        if not self.facilitator:
            raise RuntimeError("FACILITATOR_PRIVATE_KEY not set")
        usdc    = self._contracts["MockUSDC"]
        staking = self._contracts["StakingSlashing"]
        base_nonce = self.w3.eth.get_transaction_count(self.facilitator.address)
        # 1. approve
        tx1 = usdc.functions.approve(ADDRESSES["StakingSlashing"], int(amount_micro)).build_transaction(self._tx_params(nonce=base_nonce))
        h1 = self._sign_send(tx1, self.facilitator)
        self.w3.eth.wait_for_transaction_receipt(h1)
        # 2. stake — may revert if the agent isn't registered on-chain yet
        tx2 = staking.functions.stake(int(agent_id), int(amount_micro)).build_transaction(self._tx_params(nonce=base_nonce + 1))
        h2 = self._sign_send(tx2, self.facilitator)
        r2 = self.w3.eth.wait_for_transaction_receipt(h2)
        stake_hash = h2.hex() if r2.status == 1 else None
        return {
            "approveTx": h1.hex(),
            "stakeTx":   stake_hash,
            "status":    "staked" if stake_hash else "approve_only",
            "explorer":  f"https://subnets-test.avax.network/c-chain/tx/{stake_hash or h1.hex()}",
        }

    def get_listing(self, agent_id: int) -> dict:
        l = self._contracts["AgentRegistry"].functions.getListing(int(agent_id)).call()
        return {
            "minPricePerToken": str(l[0]),
            "maxTokensPerSession": str(l[1]),
            "acceptingWork": l[2],
            "nonce": int(l[3]),
        }

    def register_agent(self, wallet: str, name: str, endpoint_url: str) -> dict:
        """Register a new agent on-chain. Facilitator pays gas.
        Rejects attempts to register the facilitator's own address — it's
        already registered and such calls always revert with 'wallet registered'."""
        if not self.facilitator:
            raise RuntimeError("FACILITATOR_PRIVATE_KEY not set")
        if wallet and wallet.lower() == self.facilitator.address.lower():
            raise ValueError(
                "cannot register the facilitator's own wallet — it's already "
                "registered. Pass a unique wallet address for new listings."
            )
        reg = self._contracts["AgentRegistry"]
        tx = reg.functions.registerAgent(
            Web3.to_checksum_address(wallet), name, endpoint_url
        ).build_transaction(self._tx_params())
        h = self._sign_send(tx, self.facilitator)
        receipt = self.w3.eth.wait_for_transaction_receipt(h)
        # Parse agentId from return value via logs (first topic[1] on AgentRegistry)
        agent_id = None
        for log in receipt["logs"]:
            if log["address"].lower() == ADDRESSES["AgentRegistry"].lower():
                if len(log["topics"]) >= 2:
                    agent_id = int(log["topics"][1].hex(), 16)
                    break
        return {
            "agentId": str(agent_id) if agent_id is not None else None,
            "txHash": h.hex(),
            # Ava Labs' official explorer — indexes Fuji faster than Snowtrace.
            # The `snowtrace` key is kept for backward compatibility with
            # callers that were built before this switchover.
            "explorer": f"https://subnets-test.avax.network/c-chain/tx/{h.hex()}",
            "snowtrace": f"https://testnet.snowtrace.io/tx/{h.hex()}",
        }

    def cancel_session(self, session_id: int) -> dict:
        """Cancel an open escrow session. Facilitator pays gas."""
        if not self.facilitator:
            raise RuntimeError("FACILITATOR_PRIVATE_KEY not set")
        escrow = self._contracts["EscrowPayment"]
        tx = escrow.functions.cancelSession(int(session_id)).build_transaction(self._tx_params())
        h = self._sign_send(tx, self.facilitator)
        self.w3.eth.wait_for_transaction_receipt(h)
        return {"sessionId": str(session_id), "status": "cancelled", "txHash": h.hex()}

    # ── x402 execute: EIP-3009 permit alone settles the payment ──────────────
    def x402_execute(self, p: dict) -> dict:
        """Execute an EIP-3009 transferWithAuthorization permit on MockUSDC.
        The permit IS the settlement — it atomically moves `value` from the
        signer to the recipient. No second transfer or approve step needed
        (that was a bug: double-charging the facilitator)."""
        if not self.facilitator:
            raise RuntimeError("FACILITATOR_PRIVATE_KEY not set")
        usdc = self._contracts["MockUSDC"]
        value = int(p["value"])
        agent_id = int(p.get("agentId", 0))
        base_nonce = self.w3.eth.get_transaction_count(self.facilitator.address)

        tx1 = usdc.functions.transferWithAuthorization(
            Web3.to_checksum_address(p["from"]),
            Web3.to_checksum_address(p["to"]),
            value,
            int(p.get("validAfter", 0)),
            int(p["validBefore"]),
            bytes.fromhex(p["nonce"].replace("0x", "")),
            int(p["v"]),
            bytes.fromhex(p["r"].replace("0x", "")),
            bytes.fromhex(p["s"].replace("0x", "")),
        ).build_transaction(self._tx_params(nonce=base_nonce))
        h1 = self._sign_send(tx1, self.facilitator)
        receipt = self.w3.eth.wait_for_transaction_receipt(h1)
        if receipt.status != 1:
            raise RuntimeError(f"x402 permit execution reverted tx={h1.hex()}")
        settlement_tx = h1.hex()
        if settlement_tx and not settlement_tx.startswith("0x"):
            settlement_tx = "0x" + settlement_tx
        return {
            "sessionId": None,
            "agentId":   str(agent_id),
            "status":    "settled",
            "txHashes":  {"permit": settlement_tx},  # same tx — permit IS the settlement
            "explorer":  f"https://subnets-test.avax.network/c-chain/tx/{settlement_tx}",
            "snowtrace": f"https://testnet.snowtrace.io/tx/{settlement_tx}",
        }

    # ── Gatekeeper: sign + submit incident ───────────────────────────────────
    def submit_incident(self, agent_id: int, affected_user: str, severity: int) -> dict:
        if not self.gatekeeper:
            raise RuntimeError("GATEKEEPER_PRIVATE_KEY not set")
        if severity not in (1, 2):
            raise ValueError("severity must be 1 or 2")
        rep = self._contracts["ReputationContract"]
        # Hash format must match Solidity:
        #   keccak256(abi.encode(chainid, address(this), agentId, affectedUser, severity))
        # then toEthSignedMessageHash + ECDSA.recover.
        from eth_abi import encode
        inner = self.w3.keccak(encode(
            ["uint256", "address", "uint256", "address", "uint8"],
            [CHAIN_ID, Web3.to_checksum_address(ADDRESSES["ReputationContract"]),
             int(agent_id), Web3.to_checksum_address(affected_user), int(severity)]
        ))
        msg = encode_defunct(primitive=inner)
        signed = self.gatekeeper.sign_message(msg)
        tx = rep.functions.submitIncident(
            int(agent_id), Web3.to_checksum_address(affected_user), int(severity), signed.signature
        ).build_transaction(self._tx_params(sender=self.gatekeeper.address))
        h = self._sign_send(tx, self.gatekeeper)
        self.w3.eth.wait_for_transaction_receipt(h)
        return {
            "status": "signed",
            "txHash": h.hex(),
            "snowtrace": f"https://testnet.snowtrace.io/tx/{h.hex()}",
        }

    # ── helpers ──────────────────────────────────────────────────────────────
    def _tx_params(self, sender: str | None = None, nonce: int | None = None) -> dict:
        addr = Web3.to_checksum_address(sender or self.facilitator.address)
        return {
            "from": addr,
            "nonce": nonce if nonce is not None else self.w3.eth.get_transaction_count(addr),
            "chainId": CHAIN_ID,
            "gasPrice": self.w3.eth.gas_price,
        }

    def _sign_send(self, tx: dict, signer) -> bytes:
        # estimate gas if not set
        if "gas" not in tx:
            try:
                tx["gas"] = int(self.w3.eth.estimate_gas(tx) * 1.2)
            except Exception:
                tx["gas"] = 500_000
        self._assert_signer_reserve(tx, signer.address)
        signed = signer.sign_transaction(tx)
        h = self.w3.eth.send_raw_transaction(signed.raw_transaction if hasattr(signed, "raw_transaction") else signed.rawTransaction)
        return h

    def _assert_signer_reserve(self, tx: dict, signer_address: str) -> None:
        reserve_wei = self.w3.to_wei(MIN_SIGNER_AVAX_RESERVE, "ether")
        balance_wei = int(self.w3.eth.get_balance(signer_address))
        gas_wei = int(tx.get("gas", 0)) * int(tx.get("gasPrice", 0))
        value_wei = int(tx.get("value", 0))
        required_wei = gas_wei + value_wei + reserve_wei
        if balance_wei < required_wei:
            needed_avax = float(self.w3.from_wei(required_wei - balance_wei, "ether"))
            raise RuntimeError(
                f"Insufficient AVAX reserve for signer {signer_address}. "
                f"Need +{needed_avax:.6f} AVAX to keep reserve floor {MIN_SIGNER_AVAX_RESERVE:.3f}."
            )
