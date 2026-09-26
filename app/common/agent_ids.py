"""Crockford-base32 agent identifiers with a mod-37 check character."""
from __future__ import annotations

import re
import secrets

DATA_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CHECK_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ*"
_PATTERN = re.compile(r"^AGT-[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}-[0-9A-Z*]$")


def _check_value(data: str) -> int:
    value = 0
    for char in data:
        value = (value * 32 + DATA_ALPHABET.index(char)) % 37
    return value


def generate_agent_id() -> str:
    data = "".join(secrets.choice(DATA_ALPHABET) for _ in range(8))
    return f"AGT-{data[:4]}-{data[4:]}-{CHECK_ALPHABET[_check_value(data)]}"


def is_valid_agent_id(value: str) -> bool:
    if not isinstance(value, str):
        return False
    candidate = value.upper()
    if not _PATTERN.fullmatch(candidate):
        return False
    groups = candidate.split("-")
    data = groups[1] + groups[2]
    return CHECK_ALPHABET[_check_value(data)] == groups[3]


def require_agent_id(value: str) -> str:
    candidate = str(value or "").upper()
    if not is_valid_agent_id(candidate):
        raise ValueError("invalid agent id")
    return candidate
