"""Approval errors (docs/decisions/0001-custody-chain.md §3).

Kept apart from service.py so app/humans/service.py can raise them without an
import cycle.
"""
from __future__ import annotations

# code → (HTTP status, JSON API code from §7)
_HTTP = {
    "NOT_APPROVED":      (409, "NOT_APPROVED"),
    "EXPIRED":           (410, "APPROVAL_EXPIRED"),
    "APPROVAL_CONSUMED": (409, "APPROVAL_CONSUMED"),
    "HASH_MISMATCH":     (409, "HASH_MISMATCH"),
    "WRONG_HUMAN":       (403, "WRONG_HUMAN"),
    "BANNED":            (403, "BANNED"),
    "CAP_EXCEEDED":      (403, "CAP_EXCEEDED"),
    "SCREENING_REFUSED": (403, "SCREENING_REFUSED"),
    # The payee agent stopped being hireable while the approval was open
    # (app.seller.stamp.assert_hireable, re-checked at consume).
    "NOT_STAMPED":       (409, "NOT_STAMPED"),
    "RESTAMP_REQUIRED":  (409, "RESTAMP_REQUIRED"),
    "OPERATOR_BANNED":   (409, "OPERATOR_BANNED"),
    "PAYEE_REFUSED":     (409, "PAYEE_REFUSED"),
    # ENS/profile payee binding changed or could not be resolved while an
    # approval was waiting; never fall back to another payee.
    "PAYEE_MISMATCH":    (409, "PAYEE_MISMATCH"),
    "PAYEE_UNRESOLVED":  (503, "PAYEE_UNRESOLVED"),
}
APPROVAL_ERROR_CODES = frozenset(_HTTP)


class ApprovalError(Exception):
    """An approval cannot be used. ``code`` is one of APPROVAL_ERROR_CODES."""

    def __init__(self, code: str, message: str = ""):
        if code not in _HTTP:
            raise ValueError(f"unknown ApprovalError code {code!r}")
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message or code

    @property
    def http_status(self) -> int:
        return _HTTP[self.code][0]

    @property
    def api_code(self) -> str:
        return _HTTP[self.code][1]


class ApprovalNotFound(LookupError):
    """No approval with that id (or web ``state``)."""


class ApprovalStateError(ValueError):
    """The requested operation is not allowed in the approval's current state."""

    def __init__(self, state: str, message: str):
        super().__init__(message)
        self.state = state
