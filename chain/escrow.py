"""
chain/escrow.py - engagement escrow (docs/decisions/0001-custody-chain.md §6).

    from chain.escrow import EscrowService
    escrow = EscrowService.from_env()
    tx = escrow.fund_from_vault(amount_micro=25_000_000, ref="ENG-…:fund")
    tx = escrow.release(to=payee, amount_micro=10_000_000, ref="ENG-…:m0:release@0")
    escrow.receipt_status(tx.tx_hash)   # "pending" | "confirmed" | "failed"

The escrow is a plain wallet (ESCROW_PRIVATE_KEY) that holds USDC between
funding and release:

    fund     the buyer vault (BUYER_VAULT_PRIVATE_KEY) signs an EIP-3009
             transferWithAuthorization to the escrow address; the facilitator
             (FACILITATOR_PRIVATE_KEY) submits it and pays gas.
    release  the escrow key calls USDC transfer(to, amount).

Mode is "onchain" only when all three keys are configured and web3 is
installed; otherwise every call is "simulated": nothing is sent and the
returned hash is a local "sim-…" id that callers label as simulated.

Sends never wait for a receipt; callers poll ``receipt_status`` later.

``ref`` is an optional idempotency key (additive to the §6 signatures). A
repeated ref is refused in-process (EscrowError ALREADY_FUNDED /
ALREADY_RELEASED), and on chain the funding ref also derives the EIP-3009
nonce, so the USDC contract itself rejects a second funding with the same
ref. The durable guard for releases is the caller's ledger.

No Flask imports: this module is usable from scripts and workers.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import threading
import time
from dataclasses import dataclass

from chain.config import explorer_url, get_address

MODES = ("onchain", "simulated")

USDC_TRANSFER_ABI = [
    {"type": "function", "name": "transfer", "stateMutability": "nonpayable",
     "inputs": [{"name": "to", "type": "address"}, {"name": "value", "type": "uint256"}],
     "outputs": [{"type": "bool"}]},
]


class EscrowError(RuntimeError):
    """Refused escrow call. ``code`` is machine-readable."""

    def __init__(self, code: str, message: str | None = None):
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class TxResult:
    tx_hash: str
    status: str            # simulated | pending | confirmed | failed
    explorer: str | None


def _positive_int(name: str, value) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise EscrowError("INVALID_AMOUNT", f"{name} must be an int (micro-USDC)")
    if value <= 0:
        raise EscrowError("INVALID_AMOUNT", f"{name} must be > 0")
    return value


def _is_address(value) -> bool:
    return (isinstance(value, str) and len(value) == 42 and value.startswith("0x")
            and all(c in "0123456789abcdefABCDEF" for c in value[2:]))


def ref_nonce(ref: str) -> str:
    """Deterministic EIP-3009 nonce (bytes32 hex) for an idempotency ref."""
    return "0x" + hashlib.sha256(f"agents-list/escrow/v1/{ref}".encode("utf-8")).hexdigest()


class EscrowService:
    def __init__(self, *, escrow_key: str | None = None, facilitator_key: str | None = None,
                 vault_key: str | None = None, onchain=None):
        """``onchain`` is a chain.client.OnChain (built from the env when
        omitted and all keys are set); tests inject one with a fake provider."""
        self._lock = threading.Lock()
        self._refs: set[str] = set()
        self._escrow = self._vault = None
        self.onchain = None
        self.mode = "simulated"
        if not (escrow_key and facilitator_key and vault_key):
            return
        try:
            from eth_account import Account
        except ImportError:  # web3 extras missing: stay simulated
            return
        self._escrow = Account.from_key(escrow_key)
        self._vault = Account.from_key(vault_key)
        if onchain is None:
            from chain.client import OnChain
            onchain = OnChain.from_env()
        if onchain.facilitator is None:
            onchain.facilitator = Account.from_key(facilitator_key)
        self.onchain = onchain
        self.mode = "onchain"

    @classmethod
    def from_env(cls) -> "EscrowService":
        env = os.environ
        return cls(escrow_key=env.get("ESCROW_PRIVATE_KEY") or None,
                   facilitator_key=env.get("FACILITATOR_PRIVATE_KEY") or None,
                   vault_key=env.get("BUYER_VAULT_PRIVATE_KEY") or None)

    # ── addresses ───────────────────────────────────────────────────────────
    @property
    def escrow_address(self) -> str | None:
        return self._escrow.address.lower() if self._escrow else None

    @property
    def vault_address(self) -> str | None:
        return self._vault.address.lower() if self._vault else None

    # ── idempotency ─────────────────────────────────────────────────────────
    def _claim(self, ref: str | None, code: str) -> None:
        if ref is None:
            return
        with self._lock:
            if ref in self._refs:
                raise EscrowError(code, f"escrow call {ref!r} already executed")
            self._refs.add(ref)

    def _unclaim(self, ref: str | None) -> None:
        if ref is not None:
            with self._lock:
                self._refs.discard(ref)

    @staticmethod
    def _simulated() -> TxResult:
        return TxResult("sim-" + secrets.token_hex(16), "simulated", None)

    def _sent(self, tx_hash) -> TxResult:
        h = tx_hash.hex() if hasattr(tx_hash, "hex") else str(tx_hash)
        h = h if h.startswith("0x") else "0x" + h
        return TxResult(h, "pending", explorer_url("tx", h))

    def _usdc_address(self) -> str:
        addr = get_address("USDC")
        if not addr:
            raise EscrowError("NOT_CONFIGURED", "USDC_ADDRESS is not configured")
        return addr

    # ── §6 interface ────────────────────────────────────────────────────────
    def fund_from_vault(self, *, amount_micro, valid_seconds=600, ref: str | None = None) -> TxResult:
        """Move ``amount_micro`` from the buyer vault into escrow."""
        amount_micro = _positive_int("amount_micro", amount_micro)
        self._claim(ref, "ALREADY_FUNDED")
        if self.mode == "simulated":
            return self._simulated()
        try:
            from chain.usdc import authorization_typed_data, get_usdc_domain
            from eth_account.messages import encode_typed_data
            permit = {
                "from": self._vault.address,
                "to": self._escrow.address,
                "value": str(amount_micro),
                "validAfter": 0,
                "validBefore": int(time.time()) + int(valid_seconds),
                "nonce": ref_nonce(ref) if ref else "0x" + secrets.token_hex(32),
            }
            domain = get_usdc_domain(w3=self.onchain.w3)
            signed = self._vault.sign_message(
                encode_typed_data(full_message=authorization_typed_data(permit, domain)))
            permit.update(v=signed.v, r=hex(signed.r), s=hex(signed.s))
            return self._submit_permit(permit)
        except BaseException:
            self._unclaim(ref)
            raise

    def fund_from_permit(self, permit, *, ref: str | None = None) -> TxResult:
        """Submit a buyer-signed EIP-3009 permit whose recipient is the escrow."""
        if not isinstance(permit, dict):
            raise EscrowError("INVALID_PERMIT", "permit must be a dict")
        try:
            value = int(permit.get("value", 0))
        except (TypeError, ValueError):
            raise EscrowError("INVALID_PERMIT", "permit.value must be an integer") from None
        _positive_int("permit.value", value)
        if self.mode == "onchain" and str(permit.get("to", "")).lower() != self.escrow_address:
            raise EscrowError("WRONG_RECIPIENT", "permit recipient is not the escrow address")
        self._claim(ref, "ALREADY_FUNDED")
        if self.mode == "simulated":
            return self._simulated()
        try:
            return self._submit_permit(permit)
        except BaseException:
            self._unclaim(ref)
            raise

    def release(self, *, to, amount_micro, ref: str | None = None) -> TxResult:
        """Pay ``amount_micro`` from escrow to ``to``."""
        amount_micro = _positive_int("amount_micro", amount_micro)
        if not _is_address(to):
            raise EscrowError("INVALID_ADDRESS", "to must be a 0x-prefixed 20-byte address")
        self._claim(ref, "ALREADY_RELEASED")
        if self.mode == "simulated":
            return self._simulated()
        try:
            from web3 import Web3
            oc = self.onchain
            token = oc.w3.eth.contract(address=Web3.to_checksum_address(self._usdc_address()),
                                       abi=USDC_TRANSFER_ABI)
            tx = token.functions.transfer(Web3.to_checksum_address(to), amount_micro) \
                .build_transaction(oc._tx_params(sender=self._escrow.address))
            return self._sent(oc._sign_send(tx, self._escrow))
        except BaseException:
            self._unclaim(ref)
            raise

    def receipt_status(self, tx_hash) -> str:
        if self.mode == "simulated" or str(tx_hash).startswith("sim-"):
            return "confirmed"
        try:
            receipt = self.onchain.w3.eth.get_transaction_receipt(tx_hash)
        except Exception:  # TransactionNotFound (not mined yet) or a flaky RPC
            return "pending"
        if receipt is None:
            return "pending"
        status = receipt.get("status") if hasattr(receipt, "get") else getattr(receipt, "status", None)
        return "confirmed" if int(status or 0) == 1 else "failed"

    # ── helpers ─────────────────────────────────────────────────────────────
    def _submit_permit(self, p: dict) -> TxResult:
        """chain.client.OnChain.x402_execute without waiting for the receipt."""
        from web3 import Web3
        oc = self.onchain
        oc._require_facilitator()
        token = oc.contract("USDC")
        tx = token.functions.transferWithAuthorization(
            Web3.to_checksum_address(p["from"]),
            Web3.to_checksum_address(p["to"]),
            int(p["value"]),
            int(p.get("validAfter", 0)),
            int(p["validBefore"]),
            bytes.fromhex(str(p["nonce"]).replace("0x", "")),
            int(p["v"]),
            int(str(p["r"]), 16).to_bytes(32, "big"),
            int(str(p["s"]), 16).to_bytes(32, "big"),
        ).build_transaction(oc._tx_params())
        return self._sent(oc._sign_send(tx, oc.facilitator))
