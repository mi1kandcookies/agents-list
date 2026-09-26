"""Fake escrow service with the §6 interface: records every call and returns
deterministic transaction hashes; never touches a chain."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass
class TxResult:
    tx_hash: str
    status: str
    explorer: str | None


class FakeEscrow:
    def __init__(self, mode: str = "simulated"):
        self.mode = mode
        self.calls: list[tuple[str, dict]] = []
        self.statuses: dict[str, str] = {}   # tx_hash → receipt status override
        self.fail_next: Exception | None = None

    def _tx(self, method: str, **kwargs) -> TxResult:
        self.calls.append((method, kwargs))
        if self.fail_next is not None:
            exc, self.fail_next = self.fail_next, None
            raise exc
        tx_hash = "0x" + hashlib.sha256(f"{len(self.calls)}:{method}:{sorted(kwargs.items())}".encode()).hexdigest()
        status = "simulated" if self.mode == "simulated" else "pending"
        return TxResult(tx_hash, status, None if self.mode == "simulated"
                        else f"https://sepolia.etherscan.io/tx/{tx_hash}")

    def fund_from_vault(self, *, amount_micro, valid_seconds=600, ref=None) -> TxResult:
        return self._tx("fund_from_vault", amount_micro=amount_micro, valid_seconds=valid_seconds,
                        ref=ref)

    def fund_from_permit(self, permit, *, ref=None) -> TxResult:
        return self._tx("fund_from_permit", permit=permit, ref=ref)

    def release(self, *, to, amount_micro, ref=None) -> TxResult:
        return self._tx("release", to=to, amount_micro=amount_micro, ref=ref)

    def receipt_status(self, tx_hash) -> str:
        self.calls.append(("receipt_status", {"tx_hash": tx_hash}))
        return self.statuses.get(tx_hash, "confirmed")
