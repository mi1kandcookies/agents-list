"""Sub-hires: a hired agent hires another agent under its mandate
(docs/decisions/0001-custody-chain.md §3, §4, §5, §7).

    POST /api/engagements/<id>/subhire      Authorization: Mandate <jwt>
    {agent_id, outcome, budget_usdc, category}

The caller proves its authority with the mandate granted for engagement
``<id>``; ``verify_chain`` re-walks that mandate's whole ancestry back to the
root human's consumed ``engagement.fund`` approval. The request can only
narrow that authority: the category must be one of the mandate's, the budget
at most what it has left, and the new hop within the root's ``max_depth``.

The payee is then screened server-side (hop ``subhire.hop``; anything a
client sends as a verdict is ignored):

    PAY        allocate the budget now                               → 201 child
    CAP        allocate min(budget, cap) now, recording the cap      → 201 child
    ASK_HUMAN  the ROOT human approves a ``subhire.fund`` action on
               their phone (device flow); its executor allocates     → 202 approval
    REFUSE     nothing is created or allocated                       → 403 SCREENING_REFUSED

An allocation is ledger-only: the money stays in the root escrow. It
creates a child mandate (``mandates.allocate``: charged to the parent's
``spent``), a child engagement (``parent_engagement_id``, ``depth``, the
root human as buyer) and one ``subhire_alloc`` ledger row, in one
transaction. The names hook ``on_subhire`` runs afterwards and is never
fatal.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from app.approvals.actions import format_usdc
from app.approvals.executors import ExecutionResult, after_consume, executor
from app.engagements import ledger
from app.engagements.ledger import unix
from app.engagements.service import (
    ACTIVE, EngagementError, _cap_of, _create_approval, _failed, _lock_engagement, _refuse,
    _start, engagement_json, get_escrow, payee_address, resolve_agent, screen, screening_json,
)
from app.engagements.sow import SowError, build_sow, normalize_milestones, sow_hash, sow_json
from app.extensions import db
from app.mandates import service as mandates
from app.mandates.tokens import MandateError, hum_hash

log = logging.getLogger(__name__)

HOP = "subhire.hop"
KIND = "subhire.fund"
# 401: the caller did not prove a live mandate. 403: it did, but the request
# goes beyond it (or its chain was revoked / its human banned).
MANDATE_HTTP = {"MANDATE_INVALID": 401, "MANDATE_EXPIRED": 401, "MANDATE_REVOKED": 403,
                "MANDATE_EXCEEDED": 403, "DEPTH_EXCEEDED": 403, "CATEGORY_NOT_ALLOWED": 403,
                "BANNED": 403}


def _mandate_error(exc: MandateError) -> EngagementError:
    return EngagementError(exc.message, exc.code, MANDATE_HTTP.get(exc.code, 403))


def mandate_from_header(value: str | None) -> str:
    """The JWT from ``Authorization: Mandate <jwt>``."""
    scheme, _, token = (value or "").strip().partition(" ")
    if scheme.lower() != "mandate" or not token.strip():
        raise EngagementError("send the mandate as 'Authorization: Mandate <jwt>'",
                              "MANDATE_INVALID", 401)
    return token.strip()


def _models():
    from app.models import Engagement, Human, Mandate, Milestone
    return Engagement, Human, Mandate, Milestone


# ── request ───────────────────────────────────────────────────────────────
def subhire(engagement_id: str, token: str, *, agent_ref, outcome, budget_micro: int,
            category, flow: str = "device") -> tuple[int, dict]:
    """Handle one sub-hire request. Returns ``(http_status, body)``; every
    refusal raises ``EngagementError``."""
    Engagement, _, Mandate, _ = _models()
    try:
        claims = mandates.verify_chain(token)
    except MandateError as exc:
        raise _mandate_error(exc) from None
    parent = db.session.get(Engagement, str(engagement_id))
    if parent is None:
        raise EngagementError("engagement not found", "ENGAGEMENT_NOT_FOUND", 404)
    if parent.mandate_id != claims["jti"]:
        raise EngagementError("this mandate was not granted for this engagement",
                              "MANDATE_INVALID", 403)
    if parent.status not in ACTIVE:
        raise EngagementError(f"engagement is {parent.status}", "NOT_ACTIVE", 409)
    parent_row = db.session.get(Mandate, claims["jti"])
    root_row = db.session.get(Mandate, parent_row.root_id)

    agent = resolve_agent(agent_ref)
    if agent.public_id == parent_row.grantee_agent_public_id:
        raise EngagementError("an agent cannot sub-hire itself", field="agent_id")
    cat = str(category or "").strip()
    if not cat:
        raise EngagementError("category is required", field="category")

    # Early answers for the caller; mandates.allocate re-checks all of this
    # under the parent's row lock when the money is allocated.
    if cat not in (parent_row.categories or []):
        raise EngagementError(f"category {cat!r} is not in the mandate "
                              f"({', '.join(parent_row.categories or [])})",
                              "CATEGORY_NOT_ALLOWED", 403, "category")
    if parent_row.depth + 1 > min(root_row.max_depth, mandates.max_depth_limit()):
        raise EngagementError(f"sub-hire depth {parent_row.depth + 1} exceeds the chain's "
                              f"max depth {root_row.max_depth}", "DEPTH_EXCEEDED", 403)
    remaining = mandates.remaining_micro(parent_row)
    if budget_micro > remaining:
        raise EngagementError(f"budget exceeds what the mandate has left ({format_usdc(remaining)})",
                              "MANDATE_EXCEEDED", 403, "budget_usdc")
    payee = payee_address(agent)
    if payee is None:
        raise EngagementError("agent has no payout address", "PAYEE_ADDRESS_MISSING", 409)

    verdict = screen(HOP, chain_address=payee, amount_micro=budget_micro, engagement=parent,
                     agent=agent)
    if verdict["verdict"] == "REFUSE":
        _refuse(parent, verdict, terminal=False, message="sub-hire refused by risk screening")
    db.session.commit()   # keep the verdict on record whatever happens next
    capped = verdict["verdict"] == "CAP" and budget_micro > _cap_of(verdict)
    amount = _cap_of(verdict) if capped else budget_micro
    extra = {"requested_micro": budget_micro, "allocated_micro": amount, "capped": capped,
             "cap_micro": _cap_of(verdict) if verdict["verdict"] == "CAP" else None,
             "screening": screening_json(verdict)}

    child = _new_child(parent, parent_row, root_row, agent, outcome, amount, cat)
    if verdict["verdict"] == "ASK_HUMAN":
        return 202, _ask_root_human(child, parent_row, payee, verdict, flow, extra)

    try:
        row, _ = _allocate(child, parent_row, amount, cat, approval_id=None,
                           screening_id=verdict["id"])
        db.session.commit()
    except MandateError as exc:
        db.session.rollback()
        raise _mandate_error(exc) from None
    _name(child)
    return 201, {**engagement_json(child, detail=True), **extra, "mandate_token": row.token}


def _new_child(parent, parent_row, root_row, agent, outcome, amount: int, category: str):
    """The sub-engagement, flushed (not committed) and not yet funded. The
    root human is its buyer, so only they can approve anything on it."""
    Engagement, _, _, Milestone = _models()
    deadline = unix(parent.deadline_at)
    if deadline is not None and deadline <= int(time.time()):
        deadline = None
    try:
        sow = build_sow(agent_public_id=agent.public_id, outcome=outcome, budget_micro=amount,
                        milestones=normalize_milestones(None, amount), deadline=deadline,
                        category=category)
    except SowError as exc:
        raise EngagementError(str(exc), exc.code, 400, exc.field) from None
    child = Engagement(
        agent_id=agent.id, buyer_human_id=root_row.human_id,
        parent_engagement_id=parent.id, depth=parent_row.depth + 1,
        outcome=sow["outcome"], category=category, sow_json=sow_json(sow), sow_hash=sow_hash(sow),
        total_micro=amount, status="scoped", deadline_at=parent.deadline_at if deadline else None,
    )
    for m in sow["milestones"]:
        child.milestones.append(Milestone(idx=m["idx"], title=m["title"], acceptance=m["acceptance"],
                                          amount_micro=m["amount_micro"]))
    db.session.add(child)
    db.session.flush()
    return child


def _ask_root_human(child, parent_row, payee: str, verdict: dict, flow: str, extra: dict) -> dict:
    child.status = "awaiting_approval"
    db.session.flush()
    try:
        approval = _create_approval(KIND, {
            "engagement_id": child.id, "sow_hash": child.sow_hash, "amount_micro": child.total_micro,
            "payee_agent_id": child.agent.public_id, "payee_address": payee,
            "parent_mandate_id": parent_row.id,
            "screening_id": verdict["id"], "screening_ack": True,
        }, flow=flow, engagement=child, screening_id=verdict["id"])
    except EngagementError:
        # Blocked before reaching the human (ban, weekly cap): drop the child.
        from app.models import Engagement
        orphan = db.session.get(Engagement, child.id)
        if orphan is not None:
            orphan.status = "cancelled"
            db.session.commit()
        raise
    db.session.commit()
    approval = _start(approval)
    from app.engagements.service import approval_json
    return {**approval_json(approval), **extra, "child_engagement_id": child.id,
            "parent_engagement_id": child.parent_engagement_id,
            "child_url": f"/jobs/{child.id}"}


def _allocate(child, parent_row, amount: int, category: str, *, approval_id, screening_id):
    """Child mandate + funded child + ``subhire_alloc`` row, flushed only.
    The child mandate expires with its parent. Raises MandateError when the
    parent can no longer cover it."""
    row = mandates.allocate(parent_row.token, child.agent.public_id, amount, [category],
                            unix(parent_row.expires_at))
    child.mandate_id = row.id
    escrow = get_escrow()
    entry = ledger.record(child, kind="subhire_alloc", amount_micro=amount,
                          from_addr=getattr(escrow, "escrow_address", None), to_addr=None,
                          status="simulated" if escrow.mode == "simulated" else "confirmed",
                          approval_id=approval_id, screening_id=screening_id)
    for m in child.milestones:
        m.status = "funded"
    child.status = "funded"
    db.session.flush()
    return row, entry


def _name(child) -> None:
    try:
        from app.names import service as names
        names.on_subhire(child)
    except Exception as exc:  # noqa: BLE001 - names are never fatal
        db.session.rollback()
        log.warning("sub-job name for %s not issued: %s", child.id, str(exc)[:200])


# ── ASK_HUMAN: executor for the root human's approval ─────────────────────
@executor(KIND)
def execute_subhire(approval, action: dict) -> ExecutionResult:
    _, _, Mandate, _ = _models()
    child = _lock_engagement(action.get("engagement_id"))
    if child is None or child.parent_engagement_id is None:
        return _failed("sub-engagement not found")
    if child.status != "awaiting_approval" or child.mandate_id or ledger.entries(child.id):
        return _failed(f"sub-engagement is {child.status}")
    problem = None
    payee = payee_address(child.agent)
    if action.get("sow_hash") != child.sow_hash:
        problem = "statement of work changed since approval"
    elif payee is None or action.get("payee_address") != payee:
        problem = "payee address changed since approval"
    elif action.get("payee_agent_id") != child.agent.public_id:
        problem = "payee agent does not match"
    elif action.get("amount_micro") != child.total_micro:
        problem = "amount does not match the sub-engagement"
    parent_row = db.session.get(Mandate, action.get("parent_mandate_id") or "")
    if problem is None and (parent_row is None or child.parent is None
                            or child.parent.mandate_id != parent_row.id):
        problem = "parent mandate does not match"
    if problem:
        child.status = "cancelled"
        return _failed(problem)
    try:
        with db.session.begin_nested():
            _, entry = _allocate(child, parent_row, child.total_micro, child.category,
                                 approval_id=approval.id, screening_id=action.get("screening_id"))
    except MandateError as exc:
        child.status = "cancelled"
        return _failed(f"the parent mandate no longer covers this sub-hire: {exc.code}")
    return ExecutionResult(ok=True, summary=f"Allocated {format_usdc(child.total_micro)} "
                                            f"to sub-hire {child.id}",
                           ledger_ids=[entry.id], redirect=f"/jobs/{child.id}")


@after_consume(KIND)
def _after_subhire(approval, action: dict) -> None:
    Engagement = _models()[0]
    child = db.session.get(Engagement, action.get("engagement_id") or "")
    if child is not None:
        _name(child)


# ── chain view (/jobs/<id>/chain) ─────────────────────────────────────────
MAX_TREE_DEPTH = 8


def root_of(eng):
    seen = {eng.id}
    while eng.parent is not None and eng.parent.id not in seen and len(seen) <= MAX_TREE_DEPTH:
        eng = eng.parent
        seen.add(eng.id)
    return eng


def _hop_verdict(eng) -> dict | None:
    """The screening verdict for the hop that funded (or will fund) ``eng``:
    the funding ledger row's, else its newest funding approval's."""
    from app.models import Approval, LedgerEntry, Screening
    entry = (LedgerEntry.query.filter(LedgerEntry.engagement_id == eng.id,
                                      LedgerEntry.kind.in_(("fund", "subhire_alloc")),
                                      LedgerEntry.screening_id.is_not(None))
             .order_by(LedgerEntry.created_at.desc()).first())
    screening_id = entry.screening_id if entry else None
    if screening_id is None:
        approval = (Approval.query.filter(Approval.engagement_id == eng.id,
                                          Approval.kind.in_(("engagement.fund", KIND)),
                                          Approval.screening_id.is_not(None))
                    .order_by(Approval.created_at.desc()).first())
        screening_id = approval.screening_id if approval else None
    row = db.session.get(Screening, screening_id) if screening_id else None
    if row is None:
        return None
    return {"id": row.id, "hop": row.hop, "verdict": row.verdict, "cap_micro": row.cap_micro,
            "fail_closed": bool(row.fail_closed)}


def _mandate_view(eng) -> dict | None:
    Mandate = _models()[2]
    row = db.session.get(Mandate, eng.mandate_id) if eng.mandate_id else None
    if row is None:
        return None
    exp = unix(row.expires_at)
    return {"mandate_id": row.id, "budget_micro": int(row.budget_micro),
            "spent_micro": int(row.spent_micro), "remaining_micro": mandates.remaining_micro(row),
            "categories": list(row.categories or []), "depth": row.depth,
            "max_depth": row.max_depth, "exp": exp,
            "exp_text": datetime.fromtimestamp(exp, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "status": mandates.status_of(row)}


def chain_tree(eng) -> dict:
    """Human → root mandate → hired agent → sub-agents, for the page."""
    Engagement, Human, _, _ = _models()
    root = root_of(eng)
    human = db.session.get(Human, root.buyer_human_id) if root.buyer_human_id else None
    hum = hum_hash(human.world_sub) if human else None
    trail: list[dict] = []

    def node(e, level: int) -> dict:
        agent = e.agent
        for entry in ledger.entries(e.id):
            if entry.kind in ("fund", "subhire_alloc"):
                trail.append({"engagement_id": e.id, "agent_name": agent.name if agent else None,
                              **ledger.entry_json(entry)})
        children = [] if level >= MAX_TREE_DEPTH else [
            node(c, level + 1) for c in Engagement.query.filter_by(parent_engagement_id=e.id)
            .order_by(Engagement.created_at).all()]
        return {"engagement_id": e.id, "agent_name": agent.name if agent else None,
                "agent_id": agent.public_id if agent else None, "status": e.status,
                "depth": e.depth, "outcome": e.outcome, "category": e.category,
                "total_micro": int(e.total_micro), "mandate": _mandate_view(e),
                "verdict": _hop_verdict(e), "current": e.id == eng.id, "children": children}

    tree = node(root, 0)
    trail.sort(key=lambda t: (t["created_at"] or 0, t["id"]))
    return {"engagement_id": eng.id, "root_engagement_id": root.id,
            "human": {"label": f"HUM-{hum[:12]}", "hum": hum,
                      "banned": bool(human.banned)} if human else None,
            "root": tree, "trail": trail, "escrow_mode": get_escrow().mode}
