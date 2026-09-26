"""Canonical actions and their hashes (docs/decisions/0001-custody-chain.md §1).

An action is the exact thing a human approves. Its canonical JSON is hashed
into ``action_hash`` (stored and shown) and ``action_nonce`` (sent to the
identity provider as the OIDC nonce), so an approval covers that action and
nothing else. Every action carries ``approval_id`` and ``exp``, which makes
each hash single-use.

Canonical form: UTF-8 JSON, keys sorted, no whitespace, non-ASCII kept as-is,
integers only (money is micro-USDC; floats raise TypeError), addresses as
lowercase 0x-hex, agent ids as ``AGT-XXXX-XXXX-C``, timestamps as unix seconds.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import datetime, timezone

from app.common import agent_ids

VERSION = 1

KINDS = {
    "engagement.fund": "Fund engagement",
    "milestone.release": "Release milestone",
    "manifest.publish": "Publish manifest",
    "subhire.fund": "Fund sub-hire",
    "session.login": "Sign in",
}

# Optional fields in display order. Fields that are None are omitted.
FIELDS = (
    "engagement_id", "sow_hash", "amount_micro", "payee_agent_id", "payee_address",
    "payee_source", "milestones", "milestone_idx", "parent_mandate_id", "manifest_hash",
    "screening_id", "screening_ack",
)
_INT_FIELDS = {"amount_micro", "milestone_idx"}
_HASH_FIELDS = {"sow_hash", "manifest_hash"}
_ID_PREFIXES = {"engagement_id": "ENG-", "parent_mandate_id": "MND-", "screening_id": "SCR-"}
_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_HASH_RE = re.compile(r"^0x[0-9a-f]{64}$")
PAYEE_SOURCES = {"ens": "ENS record", "profile": "Agent profile"}


def _check_json(obj, path="$"):
    """Reject values whose JSON encoding is ambiguous across languages."""
    if isinstance(obj, float):
        raise TypeError(f"float at {path}: use integer micro-units")
    if isinstance(obj, dict):
        for k, v in obj.items():
            if not isinstance(k, str):
                raise TypeError(f"non-string key {k!r} at {path}")
            _check_json(v, f"{path}.{k}")
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            _check_json(v, f"{path}[{i}]")
    elif obj is not None and not isinstance(obj, (str, int, bool)):
        raise TypeError(f"unsupported type {type(obj).__name__} at {path}")


def canonical(obj) -> bytes:
    _check_json(obj)
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _digest(obj) -> bytes:
    return hashlib.sha256(canonical(obj)).digest()


def action_hash(obj) -> str:
    return "0x" + _digest(obj).hex()


def action_nonce(obj) -> str:
    """base64url(sha256(canonical(action))) without padding: 43 characters."""
    return base64.urlsafe_b64encode(_digest(obj)).rstrip(b"=").decode("ascii")


def sow_hash(sow) -> str:
    return "0x" + _digest(sow).hex()


def _int(name: str, value) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int, got {type(value).__name__}")
    if value < 0:
        raise ValueError(f"{name} must be >= 0")
    return value


def _address(name: str, value) -> str:
    if not isinstance(value, str) or not _ADDRESS_RE.match(value):
        raise ValueError(f"{name} must be a 0x-prefixed 20-byte hex address")
    return value.lower()


def _hash(name: str, value) -> str:
    if not isinstance(value, str) or not _HASH_RE.match(value.lower()):
        raise ValueError(f"{name} must be a 0x-prefixed 32-byte hex hash")
    return value.lower()


def build_action(kind: str, **fields) -> dict:
    """Build a normalized v1 action. ``approval_id`` and ``exp`` are required;
    fields passed as None are omitted; unknown fields raise TypeError."""
    if kind not in KINDS:
        raise ValueError(f"unknown action kind {kind!r}")
    approval_id = fields.pop("approval_id", None)
    exp = fields.pop("exp", None)
    if not isinstance(approval_id, str) or not approval_id.startswith("APR-"):
        raise ValueError("approval_id (APR-…) is required")
    if isinstance(exp, datetime):
        exp = int(exp.timestamp())
    action = {"v": VERSION, "kind": kind, "approval_id": approval_id, "exp": _int("exp", exp)}
    unknown = set(fields) - set(FIELDS)
    if unknown:
        raise TypeError(f"unknown action fields: {sorted(unknown)}")
    for name in FIELDS:
        value = fields.get(name)
        if value is None:
            continue
        if name in _INT_FIELDS:
            value = _int(name, value)
        elif name in _HASH_FIELDS:
            value = _hash(name, value)
        elif name == "payee_address":
            value = _address(name, value)
        elif name == "payee_agent_id":
            value = agent_ids.normalize(value)
        elif name == "payee_source":
            if value not in PAYEE_SOURCES:
                raise ValueError(f"payee_source must be one of {sorted(PAYEE_SOURCES)}")
        elif name == "screening_ack":
            if not isinstance(value, bool):
                raise TypeError("screening_ack must be a bool")
        elif name == "milestones":
            value = [
                {"idx": _int("milestones.idx", m["idx"]),
                 "amount_micro": _int("milestones.amount_micro", m["amount_micro"]),
                 "title_hash": _hash("milestones.title_hash", m["title_hash"])}
                for m in value
            ]
        elif not isinstance(value, str) or not value.startswith(_ID_PREFIXES[name]):
            raise ValueError(f"{name} must be a {_ID_PREFIXES[name]}… id")
        action[name] = value
    canonical(action)  # fail fast on anything the checks above missed
    return action


def format_usdc(micro: int) -> str:
    """25000000 → '25.00 USDC'; keeps up to six decimals, never uses floats."""
    whole, frac = divmod(micro, 1_000_000)
    digits = f"{frac:06d}".rstrip("0")
    return f"{whole:,}.{digits.ljust(2, '0')} USDC"


def describe(action: dict) -> list[tuple[str, str]]:
    """Human-readable summary rows for approval screens."""
    rows = [("Action", KINDS.get(action.get("kind"), str(action.get("kind"))))]
    labels = (
        ("amount_micro", "Amount", format_usdc),
        ("payee_agent_id", "Payee agent", str),
        ("payee_address", "Payee address", str),
        ("payee_source", "Payee address from", lambda s: PAYEE_SOURCES.get(s, s)),
        ("engagement_id", "Engagement", str),
        ("milestone_idx", "Milestone", lambda i: f"#{i + 1}"),
        ("milestones", "Milestones", lambda ms: ", ".join(
            f"#{m['idx'] + 1} {format_usdc(m['amount_micro'])}" for m in ms)),
        ("sow_hash", "Statement of work", str),
        ("parent_mandate_id", "Parent mandate", str),
        ("manifest_hash", "Manifest", str),
        ("screening_id", "Risk screening", lambda s: s + (
            " (warning acknowledged)" if action.get("screening_ack") else "")),
    )
    for key, label, fmt in labels:
        if key in action:
            rows.append((label, fmt(action[key])))
    rows.append(("Approval", action.get("approval_id", "")))
    if "exp" in action:
        exp = datetime.fromtimestamp(action["exp"], tz=timezone.utc)
        rows.append(("Expires", exp.strftime("%Y-%m-%d %H:%M:%S UTC")))
    return rows
