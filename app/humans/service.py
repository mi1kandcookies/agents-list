"""Humans: upsert by the identity provider's pairwise ``sub``, bans and the
weekly spend cap (docs/decisions/0001-custody-chain.md §3, §11).

Weekly spend is the sum of ``amount_micro`` over this human's consumed
``engagement.fund`` and ``subhire.fund`` approvals in the last seven days.
The cap is ``humans.weekly_cap_micro`` when set, else HUMAN_WEEKLY_CAP_USDC
(USDC, decimals allowed); unset or empty means no cap.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional

from app.approvals.errors import ApprovalError
from app.extensions import db
from app.models import Approval, Human

CAPPED_KINDS = ("engagement.fund", "subhire.fund")
WEEK = timedelta(days=7)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def get_by_sub(sub: str) -> Optional[Human]:
    return db.session.query(Human).filter_by(world_sub=sub).one_or_none()


def upsert_human(sub: str, *, now: Optional[datetime] = None) -> Human:
    """The Human for ``sub``, created on first sight; bumps ``last_seen_at``.
    Flushes but does not commit."""
    if not isinstance(sub, str) or not sub:
        raise ValueError("sub is required")
    human = get_by_sub(sub)
    if human is None:
        human = Human(world_sub=sub)
        db.session.add(human)
    human.last_seen_at = now or _utcnow()
    db.session.flush()
    return human


def ban(human: Human, reason: str, *, now: Optional[datetime] = None) -> Human:
    human.banned_at = now or _utcnow()
    human.ban_reason = reason
    db.session.flush()
    return human


def ensure_not_banned(human: Optional[Human]) -> None:
    if human is not None and human.banned:
        raise ApprovalError("BANNED", "this human is banned")


def default_weekly_cap_micro() -> Optional[int]:
    raw = os.environ.get("HUMAN_WEEKLY_CAP_USDC", "").strip()
    if not raw:
        return None
    try:
        micro = Decimal(raw) * 1_000_000
    except InvalidOperation:
        raise ValueError(f"HUMAN_WEEKLY_CAP_USDC={raw!r} is not a number") from None
    if micro < 0 or micro != micro.to_integral_value():
        raise ValueError("HUMAN_WEEKLY_CAP_USDC must be >= 0 with at most 6 decimals")
    return int(micro)


def weekly_cap_micro(human: Human) -> Optional[int]:
    if human.weekly_cap_micro is not None:
        return int(human.weekly_cap_micro)
    return default_weekly_cap_micro()


def weekly_spent_micro(human: Human, *, now: Optional[datetime] = None,
                       exclude_approval_id: Optional[str] = None) -> int:
    since = (now or _utcnow()) - WEEK
    rows = (db.session.query(Approval.id, Approval.action_json, Approval.consumed_at)
            .filter(Approval.human_sub == human.world_sub,
                    Approval.kind.in_(CAPPED_KINDS),
                    Approval.state == "consumed",
                    Approval.consumed_at.isnot(None))
            .all())
    total = 0
    for approval_id, action_json, consumed_at in rows:
        if approval_id == exclude_approval_id:
            continue
        if consumed_at.tzinfo is None:
            consumed_at = consumed_at.replace(tzinfo=timezone.utc)
        if consumed_at >= since:
            total += int(json.loads(action_json).get("amount_micro") or 0)
    return total


def check_weekly_cap(human: Human, amount_micro: int, *, now: Optional[datetime] = None,
                     exclude_approval_id: Optional[str] = None) -> None:
    """Raise CAP_EXCEEDED if spending ``amount_micro`` more would pass the cap."""
    cap = weekly_cap_micro(human)
    if cap is None:
        return
    spent = weekly_spent_micro(human, now=now, exclude_approval_id=exclude_approval_id)
    if spent + amount_micro > cap:
        raise ApprovalError("CAP_EXCEEDED",
                            f"weekly cap {cap} micro-USDC; spent {spent}, requested {amount_micro}")
