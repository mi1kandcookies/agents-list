"""Engagement, SOW, milestone, and escrow-ledger services."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone

from app.extensions import db
from chain.escrow import EscrowError, SimulatedEscrowService
from chain.payment_policy import canonical_json


class EngagementError(ValueError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:20].upper()}"


def sow_hash(title: str, brief: str, budget_atomic: int, deadline: str | None,
             milestones: list[dict]) -> str:
    payload = {
        "title": title, "brief": brief, "budgetAtomic": str(int(budget_atomic)),
        "deadline": deadline or "", "milestones": milestones,
    }
    return "0x" + hashlib.sha256(canonical_json(payload).encode()).hexdigest()


def _serialize_milestone(row) -> dict:
    return {
        "id": row.id, "ordinal": row.ordinal, "title": row.title,
        "acceptanceCriteria": json.loads(row.acceptance_criteria or "[]"),
        "amountAtomic": str(row.amount_atomic), "status": row.status,
        "dueAt": row.due_at.isoformat() if row.due_at else None,
        "autoReleaseAt": row.auto_release_at.isoformat() if row.auto_release_at else None,
        "evidenceHash": row.evidence_hash, "deliverable": row.deliverable,
        "submittedAt": row.submitted_at.isoformat() if row.submitted_at else None,
        "approvedAt": row.approved_at.isoformat() if row.approved_at else None,
        "releasedAt": row.released_at.isoformat() if row.released_at else None,
    }


def serialize_engagement(row) -> dict:
    return {
        "id": row.id, "humanId": row.human_id, "parentEngagementId": row.parent_engagement_id,
        "agentId": row.agent_id, "title": row.title, "brief": row.brief,
        "status": row.status, "budgetAtomic": str(row.budget_atomic),
        "currency": row.currency, "chainId": row.chain_id, "network": row.network,
        "deadline": row.deadline.isoformat() if row.deadline else None,
        "sowHash": row.sow_hash, "depth": row.depth,
        "milestones": [_serialize_milestone(item) for item in row.milestones],
        "createdAt": row.created_at.isoformat() if row.created_at else None,
    }


def create_engagement(*, human_id: str, agent_id: int | None, title: str, brief: str,
                      budget_atomic: int, milestones: list[dict], deadline=None,
                      parent_engagement_id: str | None = None):
    from app.models import Agent, Engagement, Human, Milestone
    human = db.session.get(Human, human_id)
    if not human:
        raise EngagementError("human is not registered")
    if agent_id is not None and not db.session.get(Agent, int(agent_id)):
        raise EngagementError("agent not found")
    title, brief = str(title or "").strip(), str(brief or "").strip()
    if not title or not brief:
        raise EngagementError("title and brief are required")
    if int(budget_atomic) <= 0 or not milestones:
        raise EngagementError("budget and at least one milestone are required")
    normalized = []
    total = 0
    for ordinal, item in enumerate(milestones, start=1):
        amount = int(item.get("amountAtomic", 0))
        criteria = item.get("acceptanceCriteria") or []
        if amount <= 0 or not item.get("title") or not criteria:
            raise EngagementError("each milestone needs a title, criteria, and positive amount")
        total += amount
        normalized.append({"title": str(item["title"]).strip(), "amountAtomic": str(amount),
                           "acceptanceCriteria": list(criteria), "ordinal": ordinal,
                           "dueAt": item.get("dueAt")})
    if total != int(budget_atomic):
        raise EngagementError("milestone amounts must equal the engagement budget")
    parent = db.session.get(Engagement, parent_engagement_id) if parent_engagement_id else None
    if parent_engagement_id and not parent:
        raise EngagementError("parent engagement not found")
    depth = (parent.depth + 1) if parent else 0
    row = Engagement(
        id=_new_id("ENG"), human_id=human_id, parent_engagement_id=parent_engagement_id,
        agent_id=agent_id, title=title, brief=brief, status="draft",
        budget_atomic=int(budget_atomic), sow_hash=sow_hash(title, brief, int(budget_atomic), deadline, normalized),
        depth=depth, deadline=datetime.fromisoformat(deadline) if deadline else None,
    )
    db.session.add(row)
    db.session.flush()
    for item in normalized:
        db.session.add(Milestone(
            id=_new_id("MS"), engagement_id=row.id, ordinal=item["ordinal"],
            title=item["title"], acceptance_criteria=json.dumps(item["acceptanceCriteria"]),
            amount_atomic=int(item["amountAtomic"]), status="pending",
            due_at=datetime.fromisoformat(item["dueAt"]) if item.get("dueAt") else None,
        ))
    db.session.commit()
    return row


def get_engagement(engagement_id: str):
    from app.models import Engagement
    return db.session.get(Engagement, engagement_id)


def _service():
    from flask import current_app
    return current_app.config.setdefault("ESCROW_SERVICE", SimulatedEscrowService())


def fund_milestone(row, milestone, *, approval_id: str, screening_id: str):
    if row.status not in {"draft", "funding"} or milestone.status != "pending":
        raise EngagementError("milestone is not fundable")
    receipt = _service().fund(engagement_id=row.id, milestone_id=milestone.id,
                              amount_atomic=milestone.amount_atomic, approval_id=approval_id,
                              screening_id=screening_id)
    milestone.status = "funded"
    row.status = "funded"
    db.session.commit()
    return receipt


def submit_milestone(milestone, *, evidence_hash: str, deliverable: str):
    if milestone.status not in {"funded", "in_progress"}:
        raise EngagementError("milestone is not ready for submission")
    milestone.status = "submitted"
    milestone.evidence_hash = evidence_hash
    milestone.deliverable = deliverable
    milestone.submitted_at = _now()
    db.session.commit()
    return milestone


def release_milestone(row, milestone, *, approval_id: str, screening_id: str, payee: str):
    if milestone.status != "submitted":
        raise EngagementError("milestone is not ready for release")
    receipt = _service().release(engagement_id=row.id, milestone_id=milestone.id,
                                 amount_atomic=milestone.amount_atomic, approval_id=approval_id,
                                 screening_id=screening_id, payee=payee)
    milestone.status = "released"
    milestone.approved_at = _now()
    milestone.released_at = _now()
    if all(item.status == "released" for item in row.milestones):
        row.status = "completed"
    db.session.commit()
    return receipt
