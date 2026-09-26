"""Public agent ids: ``AGT-XXXX-XXXX-C`` (docs/decisions/0001-custody-chain.md §9).

Eight Crockford base32 symbols carry a 40-bit payload; the final symbol is
``payload mod 37`` over Crockford's 37-symbol check alphabet, so every single
mistyped symbol and every adjacent transposition is rejected.

Decoding is forgiving about presentation: case-insensitive, ``I``/``L`` read
as ``1``, ``O`` as ``0``, hyphens and the ``AGT`` prefix optional.

``from_db_id`` maps sequential database ids through a keyed Feistel
permutation so public ids are unique and stable but not guessable in sequence.
The key is a fixed constant, not a secret: it only scrambles the order.

This module has no imports from the app so it can be vendored as-is.
"""
from __future__ import annotations

import hashlib
import hmac

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CHECK_ALPHABET = ALPHABET + "*~$=U"
PREFIX = "AGT"
PAYLOAD_BITS = 40
MAX_PAYLOAD = (1 << PAYLOAD_BITS) - 1

_VALUES = {c: i for i, c in enumerate(CHECK_ALPHABET)}
_ALIASES = {"I": "1", "L": "1", "O": "0"}
_WEIGHTS = [32 ** (7 - i) for i in range(8)]  # most significant symbol first

# Fixed permutation key for from_db_id. Changing it renumbers every agent.
_PERMUTATION_KEY = b"agents-list/agent-id/v1"
_HALF_BITS = PAYLOAD_BITS // 2
_HALF_MASK = (1 << _HALF_BITS) - 1
_ROUNDS = 4


class AgentIdError(ValueError):
    """Invalid agent id. ``code`` is machine-readable; ``suggestion`` is a
    corrected id when the typo can be pinned down unambiguously, else None."""

    def __init__(self, code: str, suggestion: str | None = None, message: str | None = None):
        self.code = code
        self.suggestion = suggestion
        text = message or code
        if suggestion:
            text += f" (did you mean {suggestion}?)"
        super().__init__(text)


def encode(n: int) -> str:
    """Format a 40-bit payload as ``AGT-XXXX-XXXX-C``."""
    if isinstance(n, bool) or not isinstance(n, int) or not 0 <= n <= MAX_PAYLOAD:
        raise ValueError(f"agent id payload must be an int in [0, 2**{PAYLOAD_BITS})")
    body = "".join(ALPHABET[(n >> (5 * (7 - i))) & 31] for i in range(8))
    return f"{PREFIX}-{body[:4]}-{body[4:]}-{CHECK_ALPHABET[n % 37]}"


def _symbols(s: str) -> list[int]:
    """Normalize ``s`` and return its nine symbol values (payload + check)."""
    if not isinstance(s, str):
        raise AgentIdError("INVALID_FORMAT", message="agent id must be a string")
    raw = s.strip().upper().replace("-", "")
    if len(raw) == 12 and raw.startswith(PREFIX):
        raw = raw[3:]
    if len(raw) != 9:
        raise AgentIdError("INVALID_FORMAT", message="expected AGT-XXXX-XXXX-C")
    values = []
    for i, ch in enumerate(raw):
        ch = _ALIASES.get(ch, ch)
        v = _VALUES.get(ch)
        if v is None or (i < 8 and v >= 32):
            raise AgentIdError("INVALID_CHAR", message=f"invalid character {raw[i]!r}")
        values.append(v)
    return values


def _transposition_fix(digits: list[int], check: int, n: int) -> str | None:
    """Return the id if exactly one adjacent swap makes the check symbol
    match. Substitutions are not suggested: any single position can be
    "fixed" by some substitution, so one would just be a guess."""
    fixes = set()
    for i in range(7):
        a, b = digits[i], digits[i + 1]
        if a == b:
            continue
        m = n + (b - a) * _WEIGHTS[i] + (a - b) * _WEIGHTS[i + 1]
        if m % 37 == check:
            fixes.add(m)
    if check < 32 and check != digits[7]:
        m = n - digits[7] + check
        if m % 37 == digits[7]:
            fixes.add(m)
    return encode(fixes.pop()) if len(fixes) == 1 else None


def decode(s: str) -> int:
    """Parse an agent id and return its 40-bit payload.

    Raises AgentIdError with code INVALID_FORMAT, INVALID_CHAR or BAD_CHECK.
    """
    values = _symbols(s)
    digits, check = values[:8], values[8]
    n = sum(d * w for d, w in zip(digits, _WEIGHTS))
    if n % 37 != check:
        raise AgentIdError("BAD_CHECK", _transposition_fix(digits, check, n),
                           message="check symbol does not match")
    return n


def normalize(s: str) -> str:
    """Canonical ``AGT-XXXX-XXXX-C`` form of a valid id (raises AgentIdError)."""
    return encode(decode(s))


def is_valid(s: str) -> bool:
    try:
        decode(s)
    except AgentIdError:
        return False
    return True


def _round(r: int, half: int) -> int:
    msg = bytes([r]) + half.to_bytes(3, "big")
    digest = hmac.new(_PERMUTATION_KEY, msg, hashlib.sha256).digest()
    return int.from_bytes(digest[:3], "big") & _HALF_MASK


def _permute(n: int) -> int:
    left, right = n >> _HALF_BITS, n & _HALF_MASK
    for r in range(_ROUNDS):
        left, right = right, left ^ _round(r, right)
    return (left << _HALF_BITS) | right


def _unpermute(n: int) -> int:
    left, right = n >> _HALF_BITS, n & _HALF_MASK
    for r in reversed(range(_ROUNDS)):
        left, right = right ^ _round(r, left), left
    return (left << _HALF_BITS) | right


def from_db_id(db_id: int) -> str:
    """Deterministic public id for a database primary key (a bijection)."""
    if isinstance(db_id, bool) or not isinstance(db_id, int) or not 0 <= db_id <= MAX_PAYLOAD:
        raise ValueError("db id out of range")
    return encode(_permute(db_id))


def to_db_id(public_id: str) -> int:
    """Inverse of from_db_id (raises AgentIdError on a malformed id)."""
    return _unpermute(decode(public_id))
