"""Escrow service interface with deterministic simulation and Sepolia adapter."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol


class EscrowError(RuntimeError):
    pass


@dataclass(frozen=True)
class EscrowReceipt:
    action: str
    engagement_id: str
    milestone_id: str
    amount_atomic: int
    status: str
    tx_hash: str = ""
    mode: str = "simulated"

    def to_dict(self) -> dict:
        return {
            "action": self.action, "engagementId": self.engagement_id,
            "milestoneId": self.milestone_id, "amountAtomic": str(self.amount_atomic),
            "status": self.status, "txHash": self.tx_hash, "mode": self.mode,
        }


class EscrowService(Protocol):
    def fund(self, *, engagement_id: str, milestone_id: str, amount_atomic: int,
             approval_id: str, screening_id: str) -> EscrowReceipt: ...

    def release(self, *, engagement_id: str, milestone_id: str, amount_atomic: int,
                approval_id: str, screening_id: str, payee: str) -> EscrowReceipt: ...


class SimulatedEscrowService:
    """In-memory executor used by tests and local development only."""

    def __init__(self):
        self._funded: dict[tuple[str, str], int] = {}
        self._released: set[tuple[str, str]] = set()

    def fund(self, *, engagement_id: str, milestone_id: str, amount_atomic: int,
             approval_id: str, screening_id: str) -> EscrowReceipt:
        key = (engagement_id, milestone_id)
        if key in self._funded:
            raise EscrowError("milestone is already funded")
        if amount_atomic <= 0 or not approval_id or not screening_id:
            raise EscrowError("funding requires a positive amount, approval, and screening verdict")
        self._funded[key] = int(amount_atomic)
        return EscrowReceipt("fund", engagement_id, milestone_id, int(amount_atomic), "confirmed")

    def release(self, *, engagement_id: str, milestone_id: str, amount_atomic: int,
                approval_id: str, screening_id: str, payee: str) -> EscrowReceipt:
        key = (engagement_id, milestone_id)
        if key not in self._funded:
            raise EscrowError("milestone is not funded")
        if key in self._released:
            raise EscrowError("milestone is already released")
        if self._funded[key] != int(amount_atomic) or not payee:
            raise EscrowError("release terms do not match the funded milestone")
        self._released.add(key)
        return EscrowReceipt("release", engagement_id, milestone_id, int(amount_atomic), "confirmed")


class OnChainEscrowService:
    """Adapter for real Sepolia transfers behind the same service surface.

    Funding uses the existing buyer-signed EIP-3009 executor with the escrow
    address as the recipient. Release uses an injected vault transfer callable
    so contract or vault implementations can evolve without changing the
    approval/ledger boundary. This class never runs unless explicitly wired.
    """

    def __init__(self, onchain, *, escrow_address: str,
                 release_transfer: Callable[..., dict]):
        self.onchain = onchain
        self.escrow_address = escrow_address.lower()
        self.release_transfer = release_transfer

    def fund(self, *, engagement_id: str, milestone_id: str, amount_atomic: int,
             approval_id: str, screening_id: str, payment_payload: dict | None = None) -> EscrowReceipt:
        if not payment_payload:
            raise EscrowError("a buyer-signed payment payload is required for on-chain funding")
        if str(payment_payload.get("to", "")).lower() != self.escrow_address:
            raise EscrowError("funding recipient is not the configured escrow address")
        result = self.onchain.x402_execute(payment_payload)
        tx_hash = str((result.get("txHashes") or {}).get("permit") or "")
        return EscrowReceipt("fund", engagement_id, milestone_id, int(amount_atomic), "confirmed",
                             tx_hash=tx_hash, mode="onchain")

    def release(self, *, engagement_id: str, milestone_id: str, amount_atomic: int,
                approval_id: str, screening_id: str, payee: str) -> EscrowReceipt:
        result = self.release_transfer(to=payee, amount_atomic=int(amount_atomic),
                                       engagement_id=engagement_id, milestone_id=milestone_id)
        return EscrowReceipt("release", engagement_id, milestone_id, int(amount_atomic),
                             "confirmed", tx_hash=str(result.get("txHash") or ""), mode="onchain")
