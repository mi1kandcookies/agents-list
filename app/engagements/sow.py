"""Statement of work: a deterministic document built from the buyer's
request (no LLM), hashed with ``actions.sow_hash`` and bound into every
approval for the engagement.

    sow = build_sow(agent_public_id="AGT-…", outcome="…", budget_micro=25_000_000,
                    milestones=[{"title": "…", "acceptance": "…", "amount_micro": 10_000_000}, …],
                    deadline=1790000000, category="Development")
    sow_hash(sow)   # "0x…", stable for the same inputs

Money is integer micro-USDC; ``parse_usdc`` turns "12.5" / 12.5 into 12500000
through Decimal, never float arithmetic.
"""
from __future__ import annotations

import hashlib
import os
import re
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from app.approvals.actions import sow_hash  # noqa: F401  (re-exported)
from app.approvals.actions import canonical

SOW_VERSION = 1
MAX_OUTCOME = 4000
MAX_TITLE = 200
MAX_ACCEPTANCE = 2000
MAX_MILESTONES = 20
MAX_BUDGET_MICRO = 1_000_000 * 1_000_000   # 1M USDC per engagement
MAX_SOURCE_DOCUMENT_BYTES = 128 * 1024


class SowError(ValueError):
    def __init__(self, message: str, field: str | None = None, code: str = "INVALID_REQUEST"):
        super().__init__(message)
        self.field = field
        self.code = code


def parse_usdc(value, field: str) -> int:
    """Decimal USDC (str/int/float) → positive integer micro-USDC."""
    if isinstance(value, bool) or value is None or value == "":
        raise SowError(f"{field} is required", field)
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise SowError(f"{field} must be a decimal USDC amount", field) from None
    if not amount.is_finite() or amount <= 0:
        raise SowError(f"{field} must be > 0", field)
    micro = amount * 1_000_000
    if micro != micro.to_integral_value():
        raise SowError(f"{field} has more than 6 decimal places", field)
    micro = int(micro)
    if micro > MAX_BUDGET_MICRO:
        raise SowError(f"{field} is too large", field)
    return micro


def parse_deadline(value) -> int | None:
    """Unix seconds, or an ISO-8601 datetime (UTC when naive). A bare date
    means the end of that day, UTC."""
    if value in (None, ""):
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        ts = value
    else:
        raw = str(value).strip()
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            raise SowError("deadline must be unix seconds or an ISO-8601 date", "deadline") from None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        ts = int(dt.timestamp()) + (86_399 if len(raw) == 10 else 0)
    if ts <= int(time.time()):
        raise SowError("deadline must be in the future", "deadline")
    return ts


def _text(value, field: str, limit: int, *, required: bool = True, multiline: bool = False) -> str:
    """Collapse runs of spaces; ``multiline`` keeps (non-empty) line breaks."""
    lines = str(value or "").splitlines() if multiline else [str(value or "")]
    text = "\n".join(" ".join(line.split()) for line in lines if line.strip())
    if required and not text:
        raise SowError(f"{field} is required", field)
    if len(text) > limit:
        raise SowError(f"{field} is longer than {limit} characters", field)
    return text


def title_hash(title: str) -> str:
    """Milestone title digest bound into approval actions (§1 ``title_hash``)."""
    return sow_hash(title)


def source_document_metadata(document) -> dict:
    """Validate an uploaded text SOW and return only its binding metadata.

    The raw document is deliberately not copied into the engagement row.  The
    application binds the exact uploaded bytes into the canonical SOW by
    filename, byte count and SHA-256 digest; the caller can retain the source
    file locally or in its own document store without expanding the payment
    database into an arbitrary file bucket.
    """
    if not isinstance(document, dict):
        raise SowError("sow_document must be an object", "sow_document")
    filename = str(document.get("filename") or "").strip()
    content = document.get("content")
    if not filename:
        raise SowError("sow_document.filename is required", "sow_document.filename")
    if len(filename) > 240 or os.path.basename(filename) != filename or filename in {".", ".."}:
        raise SowError("sow_document.filename must be a plain file name", "sow_document.filename")
    if not isinstance(content, str):
        raise SowError("sow_document.content must be UTF-8 text", "sow_document.content")
    raw = content.encode("utf-8")
    if not raw:
        raise SowError("sow_document.content is empty", "sow_document.content")
    if len(raw) > MAX_SOURCE_DOCUMENT_BYTES:
        raise SowError(f"sow_document is larger than {MAX_SOURCE_DOCUMENT_BYTES} bytes",
                       "sow_document.content", code="SOW_DOCUMENT_TOO_LARGE")
    return {"filename": filename, "bytes": len(raw),
            "sha256": "0x" + hashlib.sha256(raw).hexdigest()}


def normalize_source_document(value) -> dict | None:
    """Normalize metadata produced by the web uploader or MCP text upload.

    The web flow already has a digest from its parser; the MCP flow supplies
    UTF-8 text and byte-count metadata. Both shapes are canonicalized before
    they enter the SOW hash so the approval binds one exact document.
    """
    if value in (None, {}, ""):
        return None
    if not isinstance(value, dict):
        raise SowError("source_document must be an object", "source_document")
    filename = str(value.get("filename") or "").strip()
    if not filename:
        raise SowError("source_document.filename is required", "source_document.filename")
    filename = re.split(r"[\\/]", filename)[-1]
    filename = _text(re.sub(r"[\x00-\x1f\x7f]", "", filename),
                     "source_document.filename", 240)
    digest = str(value.get("sha256") or "").strip().lower()
    if digest.startswith("0x"):
        digest = digest[2:]
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise SowError("source_document.sha256 must be 64 hex characters",
                       "source_document.sha256")
    if "bytes" not in value:
        return {"filename": filename, "sha256": digest}
    byte_count = value.get("bytes")
    if isinstance(byte_count, bool) or not isinstance(byte_count, int) or byte_count <= 0:
        raise SowError("source_document.bytes must be a positive integer", "source_document.bytes")
    if byte_count > MAX_SOURCE_DOCUMENT_BYTES:
        raise SowError(f"source_document is larger than {MAX_SOURCE_DOCUMENT_BYTES} bytes",
                       "source_document.bytes", code="SOW_DOCUMENT_TOO_LARGE")
    return {"filename": filename, "bytes": byte_count, "sha256": "0x" + digest}


def default_milestones(outcome: str, budget_micro: int) -> list[dict]:
    """One milestone for the whole outcome when the buyer gives none."""
    first = outcome.splitlines()[0]
    title = first if len(first) <= 80 else first[:77].rstrip() + "..."
    return [{"title": f"Deliver: {title}",
             "acceptance": "The delivered work achieves the stated outcome and the buyer accepts it.",
             "amount_micro": budget_micro}]


def normalize_milestones(raw, budget_micro: int) -> list[dict]:
    """Validate API milestones ``[{title, acceptance, amount_usdc}]`` (or
    ``amount_micro``) and check that they add up to the budget."""
    if raw in (None, []):
        return []
    if not isinstance(raw, list):
        raise SowError("milestones must be a list", "milestones")
    if len(raw) > MAX_MILESTONES:
        raise SowError(f"at most {MAX_MILESTONES} milestones", "milestones")
    out = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise SowError("each milestone must be an object", f"milestones[{i}]")
        if "amount_micro" in item and "amount_usdc" not in item:
            amount = item["amount_micro"]
            if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
                raise SowError("amount_micro must be a positive int", f"milestones[{i}].amount_micro")
        else:
            amount = parse_usdc(item.get("amount_usdc"), f"milestones[{i}].amount_usdc")
        out.append({
            "title": _text(item.get("title"), f"milestones[{i}].title", MAX_TITLE),
            "acceptance": _text(item.get("acceptance"), f"milestones[{i}].acceptance",
                                MAX_ACCEPTANCE, multiline=True),
            "amount_micro": amount,
        })
    total = sum(m["amount_micro"] for m in out)
    if total != budget_micro:
        raise SowError(f"milestone amounts add up to {total} micro-USDC, budget is {budget_micro}",
                       "milestones", code="MILESTONES_MISMATCH")
    return out


def build_sow(*, agent_public_id: str, outcome: str, budget_micro: int,
              milestones: list[dict] | None = None, deadline: int | None = None,
              category: str | None = None, source_document: dict | None = None) -> dict:
    """The canonical SOW object. Same inputs → same dict → same hash."""
    outcome = _text(outcome, "outcome", MAX_OUTCOME, multiline=True)
    if isinstance(budget_micro, bool) or not isinstance(budget_micro, int) or budget_micro <= 0:
        raise SowError("budget must be a positive amount", "budget_usdc")
    plan = milestones or default_milestones(outcome, budget_micro)
    sow = {
        "v": SOW_VERSION,
        "agent_id": agent_public_id,
        "outcome": outcome,
        "currency": "USDC",
        "budget_micro": budget_micro,
        "milestones": [{"idx": i, "title": m["title"], "acceptance": m["acceptance"],
                        "amount_micro": m["amount_micro"]} for i, m in enumerate(plan)],
    }
    if category:
        sow["category"] = category
    if deadline is not None:
        sow["deadline"] = deadline
    source = normalize_source_document(source_document)
    if source:
        sow["source_document"] = source
    canonical(sow)   # fail fast on anything non-canonical
    return sow


def sow_json(sow: dict) -> str:
    return canonical(sow).decode("utf-8")
