"""
chain/payment_policy.py - limits applied to every x402 payment, on both the
paying and the receiving side, before anything is signed or settled.

    policy = PaymentPolicy(max_amount_micro=1_000_000, allowed_payees={payee})
    policy.check(amount_micro=50_000, pay_to=payee)       # raises PolicyError

    nonces = NonceRegistry()
    nonces.claim(nonce)        # DUPLICATE_NONCE on a second claim (retries included)
    nonces.release(nonce)      # only when nothing was sent

The nonce registry is the in-process replay guard: a nonce is burned once it
is claimed, so resending the same signed authorization (a duplicate or a
retry) is refused before it reaches the facilitator. On chain the USDC
contract also rejects a used EIP-3009 nonce.

No Flask imports.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass

_NONCE_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")


class PolicyError(Exception):
    def __init__(self, code: str, message: str | None = None):
        super().__init__(message or code)
        self.code = code
        self.message = message or code


@dataclass(frozen=True)
class PaymentPolicy:
    max_amount_micro: int
    allowed_payees: frozenset[str] | None = None     # None: any payee

    def __init__(self, max_amount_micro: int, allowed_payees=None):
        if isinstance(max_amount_micro, bool) or not isinstance(max_amount_micro, int) \
                or max_amount_micro < 0:
            raise PolicyError("POLICY_INVALID", "max_amount_micro must be a non-negative int")
        object.__setattr__(self, "max_amount_micro", max_amount_micro)
        object.__setattr__(self, "allowed_payees", None if allowed_payees is None
                           else frozenset(str(p).lower() for p in allowed_payees))

    def check(self, *, amount_micro: int, pay_to: str) -> None:
        if isinstance(amount_micro, bool) or not isinstance(amount_micro, int) or amount_micro <= 0:
            raise PolicyError("INVALID_AMOUNT", "amount must be a positive integer")
        if amount_micro > self.max_amount_micro:
            raise PolicyError("MAX_AMOUNT_EXCEEDED",
                              f"amount {amount_micro} exceeds the limit {self.max_amount_micro}")
        if self.allowed_payees is not None and str(pay_to).lower() not in self.allowed_payees:
            raise PolicyError("PAYEE_NOT_ALLOWED", "payee is not on the allowed list")

    def narrowed(self, *, max_amount_micro: int | None = None, allowed_payees=None) -> "PaymentPolicy":
        """A policy that is at most as permissive as this one."""
        amount = self.max_amount_micro if max_amount_micro is None \
            else min(self.max_amount_micro, max_amount_micro)
        if allowed_payees is None:
            payees = self.allowed_payees
        else:
            wanted = frozenset(str(p).lower() for p in allowed_payees)
            payees = wanted if self.allowed_payees is None else wanted & self.allowed_payees
        return PaymentPolicy(amount, payees)


class NonceRegistry:
    """Nonces seen by this process. Thread-safe."""

    def __init__(self):
        self._lock = threading.Lock()
        self._seen: set[str] = set()

    @staticmethod
    def _key(nonce: str) -> str:
        if not isinstance(nonce, str) or not _NONCE_RE.match(nonce):
            raise PolicyError("INVALID_NONCE", "nonce must be 32 bytes of hex")
        return nonce.lower()

    def claim(self, nonce: str) -> None:
        key = self._key(nonce)
        with self._lock:
            if key in self._seen:
                raise PolicyError("DUPLICATE_NONCE", "this payment authorization was already used")
            self._seen.add(key)

    def release(self, nonce: str) -> None:
        with self._lock:
            self._seen.discard(self._key(nonce))

    def seen(self, nonce: str) -> bool:
        with self._lock:
            return self._key(nonce) in self._seen
