"""Escrow ledger: every fund / release / hold for an engagement, one row per
movement, in integer micro-USDC. The ledger (not the escrow service's memory)
is the durable double-fund / double-release guard."""
from __future__ import annotations

from datetime import datetime, timezone

from app.extensions import db
from chain.config import explorer_url

LIVE = ("simulated", "pending", "confirmed")   # everything except failed


def unix(dt: datetime | None) -> int | None:
    """Unix seconds; naive datetimes (SQLite) are UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def record(engagement, *, kind: str, amount_micro: int, milestone=None, tx=None,
           from_addr: str | None = None, to_addr: str | None = None,
           approval_id: str | None = None, screening_id: str | None = None,
           status: str | None = None):
    """Add a ledger row (flushed, not committed). ``tx`` is an escrow TxResult;
    rows without one (holds) take ``status``."""
    from app.models import LedgerEntry
    entry = LedgerEntry(
        engagement_id=engagement.id, milestone_id=milestone.id if milestone else None,
        kind=kind, amount_micro=int(amount_micro), from_addr=from_addr, to_addr=to_addr,
        tx_hash=tx.tx_hash if tx else None, status=(tx.status if tx else status) or "simulated",
        approval_id=approval_id, screening_id=screening_id,
    )
    db.session.add(entry)
    db.session.flush()
    return entry


def entries(engagement_id: str) -> list:
    from app.models import LedgerEntry
    return (LedgerEntry.query.filter_by(engagement_id=engagement_id)
            .order_by(LedgerEntry.created_at, LedgerEntry.id).all())


def has_live_fund(engagement_id: str) -> bool:
    from app.models import LedgerEntry
    return db.session.query(LedgerEntry.id).filter(
        LedgerEntry.engagement_id == engagement_id, LedgerEntry.kind == "fund",
        LedgerEntry.status.in_(LIVE)).first() is not None


def released_micro(milestone) -> int:
    """Micro-USDC already paid out (or in flight) for a milestone."""
    from app.models import LedgerEntry
    total = db.session.query(db.func.coalesce(db.func.sum(LedgerEntry.amount_micro), 0)).filter(
        LedgerEntry.milestone_id == milestone.id, LedgerEntry.kind == "release",
        LedgerEntry.status.in_(LIVE)).scalar()
    return int(total or 0)


def refresh_receipts(engagement, escrow) -> int:
    """Poll pending on-chain rows once; roll state back when a send failed.
    Returns how many rows changed. Caller commits."""
    changed = 0
    for entry in entries(engagement.id):
        if entry.status != "pending" or not entry.tx_hash:
            continue
        try:
            status = escrow.receipt_status(entry.tx_hash)
        except Exception:
            continue
        if status == entry.status:
            continue
        entry.status = status
        changed += 1
        if status == "failed":
            _roll_back(engagement, entry)
    return changed


def _roll_back(engagement, entry) -> None:
    """A fund or release reverted on chain: undo the optimistic state change."""
    from app.models import LedgerEntry
    if entry.kind == "fund":
        if has_live_fund(engagement.id):   # another funding of this engagement stands
            return
        engagement.status = "scoped"
        for m in engagement.milestones:
            if m.status == "funded":
                m.status = "pending"
    elif entry.kind == "release" and entry.milestone_id is not None:
        for hold in LedgerEntry.query.filter_by(approval_id=entry.approval_id, kind="hold").all():
            hold.status = "failed"
        m = next((m for m in engagement.milestones if m.id == entry.milestone_id), None)
        if m is None:
            return
        paid = released_micro(m)
        if paid >= m.amount_micro:
            return
        m.status = "held" if paid else ("submitted" if m.submitted_at else "funded")
        if m.released_ledger_id == entry.id:
            m.released_ledger_id = None
        if engagement.status == "completed":
            engagement.status = "in_progress"


def entry_json(entry) -> dict:
    linked = entry.tx_hash and entry.status != "simulated" and not entry.tx_hash.startswith("sim-")
    return {
        "ledger_id": entry.id, "kind": entry.kind, "amount_micro": int(entry.amount_micro),
        "milestone_id": entry.milestone_id, "from": entry.from_addr, "to": entry.to_addr,
        "tx_hash": entry.tx_hash, "status": entry.status, "simulated": entry.status == "simulated",
        "explorer": explorer_url("tx", entry.tx_hash) if linked else None,
        "approval_id": entry.approval_id, "screening_id": entry.screening_id,
        "created_at": unix(entry.created_at),
    }
