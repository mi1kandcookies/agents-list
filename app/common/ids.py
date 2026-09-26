"""Prefixed random ids for custody-chain rows: ``ENG-…``, ``APR-…``, ``LED-…``,
``MND-…``, ``SCR-…``, ``HIT-…``. Twelve Crockford base32 symbols (60 random bits)."""
from __future__ import annotations

import secrets

from app.common.agent_ids import ALPHABET

PREFIXES = frozenset({"ENG", "APR", "LED", "MND", "SCR", "HIT"})


def new_id(prefix: str) -> str:
    if prefix not in PREFIXES:
        raise ValueError(f"unknown id prefix {prefix!r}")
    return f"{prefix}-" + "".join(secrets.choice(ALPHABET) for _ in range(12))
