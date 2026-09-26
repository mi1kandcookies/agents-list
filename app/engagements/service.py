"""Engagement, SOW, milestone, and escrow-ledger services."""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone

from app.approvals.actions import action_hash
from app.approvals.service import create_approval, get_approval
from app.extensions import db
from chain.escrow import EscrowError, SimulatedEscrowService
from chain.payment_policy import canonical_json
from chain.intercepta import quick_scan_address
from app.screening.policy import ScreeningBlocked, enforce_verdict, normalize_verdict


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


def _action_terms(row, milestone, *, action_type: str, payee: str,
                  verdict_id: str) -> dict:
    human_wallet = (row.human.wallet or "").lower()
    return {
        "actionType": action_type,
        "engagementId": row.id,
        "milestoneId": milestone.id,
        "payer": human_wallet,
        "payee": str(payee).lower(),
        "amountAtomic": str(int(milestone.amount_atomic)),
        "asset": row.currency.lower(),
        "chainId": int(row.chain_id),
        "screeningVerdictId": verdict_id,
    }


def screen_action(row, milestone, *, action_type: str, payee: str):
    """Run the provider-owned check and persist the evidence for one hop."""
    from app.models import Screening

    payee = str(payee or "").strip()
    if not payee:
        raise EngagementError("payee is required for screening")
    provider_result = quick_scan_address(payee)
    verdict = normalize_verdict(provider_result.to_dict(), address=payee,
                                provider=provider_result.provider)
    enforce_verdict(verdict, milestone.amount_atomic)
    request_hash = "0x" + hashlib.sha256(canonical_json({
        "actionType": action_type, "address": payee.lower(),
        "amountAtomic": str(milestone.amount_atomic), "chainId": row.chain_id,
        "asset": row.currency.lower(),
    }).encode()).hexdigest()
    screening = Screening(
        id=_new_id("SCR"), engagement_id=row.id, action_type=action_type,
        subject_address=payee, decision=verdict.decision,
        verdict_id=verdict.verdict_id, provider=verdict.provider,
        request_hash=request_hash, evidence_json=json.dumps(provider_result.evidence, sort_keys=True),
        checked_at=datetime.fromtimestamp(verdict.checked_at, tz=timezone.utc),
        expires_at=(datetime.fromtimestamp(verdict.expires_at, tz=timezone.utc)
                    if verdict.expires_at else None),
    )
    db.session.add(screening)
    db.session.flush()
    return verdict, screening


def create_action_approval(row, milestone, *, action_type: str, payee: str):
    """Create approval only after the current counterparty has been screened."""
    if action_type == "fund" and milestone.status != "pending":
        raise EngagementError("milestone is not fundable")
    if action_type == "release" and milestone.status != "submitted":
        raise EngagementError("milestone is not ready for release")
    verdict, screening = screen_action(row, milestone, action_type=action_type, payee=payee)
    expires_at = _now().replace(microsecond=0)
    expires_at = expires_at + timedelta(seconds=int(os.environ.get("APPROVAL_TTL_SECONDS", "900")))
    terms = _action_terms(row, milestone, action_type=action_type, payee=payee,
                          verdict_id=verdict.verdict_id)
    approval = create_approval(human_id=row.human_id, action_type=action_type,
                               terms=terms, expires_at=expires_at, payload=terms)
    return approval, screening, verdict


def validate_action_approval(approval_id: str, expected_hash: str, row, milestone, *,
                             action_type: str, payee: str, verdict_id: str):
    approval = get_approval(str(approval_id))
    if not approval:
        raise EngagementError("approval not found")
    terms = _action_terms(row, milestone, action_type=action_type, payee=payee,
                          verdict_id=verdict_id)
    expected = action_hash(approval.id, int(_aware(approval.expires_at).timestamp()), terms)
    if approval.action_hash != expected or expected_hash != approval.action_hash:
        raise EngagementError("approval does not match the current action or screening verdict")
    return approval


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


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
    selected_agent = None
    if agent_id is not None:
        if isinstance(agent_id, str) and not agent_id.isdigit():
            from app.common.agent_ids import require_agent_id
            selected_agent = Agent.query.filter_by(public_id=require_agent_id(agent_id)).first()
        else:
            selected_agent = db.session.get(Agent, int(agent_id))
        if not selected_agent:
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
        agent_id=selected_agent.id if selected_agent else None, title=title, brief=brief, status="draft",
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
