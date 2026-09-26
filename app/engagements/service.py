"""Engagements: scope a job into a SOW, hire (fund) and release milestones.

Money never moves from here directly. ``hire`` and ``request_release``
screen the payee, then create an approval bound to the exact action
(docs/decisions/0001-custody-chain.md §1, §3); the approval service runs the
executors below once a verified human approves. Every release needs its own
fresh approval.

Screening (§4) and approvals (§3) live in ``app.screening.service`` and
``app.approvals.service`` and are imported lazily: if the screener cannot be
loaded or fails, every payment is refused (fail closed); without the approval
service hiring returns 503. The verdict is never taken from a request.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from flask import current_app
from sqlalchemy import select, update

from app.approvals.actions import describe, format_usdc
from app.approvals.executors import ExecutionResult, after_consume, executor
from app.common import agent_ids
from app.common.ids import new_id
from app.engagements import ledger
from app.engagements.ledger import unix
from app.engagements.sow import (
    SowError, build_sow, normalize_milestones, parse_deadline, sow_hash, sow_json, title_hash,
)
from app.extensions import db
from chain.escrow import EscrowError, EscrowService

log = logging.getLogger(__name__)

_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
VERDICTS = ("PAY", "CAP", "REFUSE", "ASK_HUMAN")
HIREABLE = ("scoped", "awaiting_approval")
ACTIVE = ("funded", "in_progress")
RELEASABLE = ("funded", "submitted", "held")
_HTTP_FOR_CODE = {"BANNED": 403, "CAP_EXCEEDED": 403, "SCREENING_REFUSED": 403,
                  "APPROVAL_EXPIRED": 410, "EXPIRED": 410}


class EngagementError(Exception):
    def __init__(self, message: str, code: str = "INVALID_REQUEST", status: int = 400,
                 field: str | None = None, screening: dict | None = None):
        super().__init__(message)
        self.message, self.code, self.status = message, code, status
        self.field, self.screening = field, screening


# ── helpers ───────────────────────────────────────────────────────────────
def _default_cap_micro() -> int:
    from app.engagements.sow import parse_usdc
    try:
        return parse_usdc(os.environ.get("SCREENING_CAP_USDC") or "10", "SCREENING_CAP_USDC")
    except SowError:
        return 10_000_000


def get_escrow():
    """One EscrowService per app (keeps the simulator's idempotency refs)."""
    ext = current_app.extensions
    if "agents_list.escrow" not in ext:
        ext["agents_list.escrow"] = EscrowService.from_env()
    return ext["agents_list.escrow"]


def payee_address(agent) -> str | None:
    for candidate in (agent.payout_address, agent.deployer_wallet):
        if candidate and _ADDRESS_RE.match(candidate):
            return candidate.lower()
    return None


def resolve_agent(ref):
    """``AGT-…`` id, or a numeric database id (JSON number or short digit
    string, as /api/agents lists it) → Agent row; else EngagementError
    INVALID_AGENT_ID / AGENT_NOT_FOUND."""
    from app.models import Agent
    if isinstance(ref, int) and not isinstance(ref, bool) or \
            isinstance(ref, str) and ref.strip().isdigit() and len(ref.strip()) < 9:
        agent = db.session.get(Agent, int(ref)) if int(ref) > 0 else None
        if agent is None or agent.verification_tier == "suspended":
            raise EngagementError("agent not found", "AGENT_NOT_FOUND", 404, "agent_id")
        return agent
    try:
        normalized = agent_ids.normalize(str(ref or ""))
    except agent_ids.AgentIdError as exc:
        hint = f"; did you mean {exc.suggestion}?" if getattr(exc, "suggestion", None) else ""
        raise EngagementError(f"agent_id is not a valid AGT id{hint}", "INVALID_AGENT_ID",
                              400, "agent_id") from None
    agent = Agent.query.filter_by(public_id=normalized).first()
    if agent is None or agent.verification_tier == "suspended":
        raise EngagementError("agent not found", "AGENT_NOT_FOUND", 404, "agent_id")
    return agent


def get_engagement(engagement_id: str):
    from app.models import Engagement
    eng = db.session.get(Engagement, str(engagement_id))
    if eng is None:
        raise EngagementError("engagement not found", "ENGAGEMENT_NOT_FOUND", 404)
    return eng


def get_milestone(eng, idx):
    try:
        idx = int(idx)
    except (TypeError, ValueError):
        idx = -1
    m = next((m for m in eng.milestones if m.idx == idx), None)
    if m is None:
        raise EngagementError("milestone not found", "MILESTONE_NOT_FOUND", 404, "milestone_idx")
    return m


# ── screening (§4) ────────────────────────────────────────────────────────
def _fail_closed(hop: str, code: str, message: str) -> dict:
    now = int(time.time())
    return {"id": new_id("SCR"), "hop": hop, "verdict": "REFUSE", "cap_micro": None,
            "reasons": [{"code": code, "message": message, "source": "agents-list"}],
            "signals": {}, "provider": "none", "fail_closed": True, "latency_ms": 0,
            "created_at": now, "expires_at": now}


def _as_dict(verdict) -> dict:
    if isinstance(verdict, dict):
        return dict(verdict)
    if hasattr(verdict, "to_dict"):
        return dict(verdict.to_dict())
    from dataclasses import asdict, is_dataclass
    if is_dataclass(verdict):
        return asdict(verdict)
    return dict(vars(verdict))


def screen(hop: str, *, chain_address: str, amount_micro: int, engagement, agent) -> dict:
    """Server-side screening. The verdict always comes from the screener,
    never from the request; any failure is a fail-closed REFUSE."""
    try:
        _screen = importlib.import_module("app.screening.service").screen
    except (ImportError, AttributeError):
        v = _fail_closed(hop, "SCREENING_UNAVAILABLE", "screening service is not installed")
    else:
        try:
            v = _as_dict(_screen(hop, chain_address=chain_address, amount_micro=amount_micro,
                                 engagement_id=engagement.id, agent_id=agent.public_id))
        except Exception as exc:
            log.warning("screening failed (%s): %s", hop, str(exc)[:200])
            v = _fail_closed(hop, "SCREENING_ERROR", "screening failed")
    if v.get("verdict") not in VERDICTS or not str(v.get("id", "")).startswith("SCR-"):
        v = _fail_closed(hop, "SCREENING_INVALID", "screener returned an unusable verdict")
    _record_screening(v, hop, chain_address, engagement, agent)
    return v


def _record_screening(v: dict, hop: str, chain_address: str, engagement, agent) -> None:
    """Persist the verdict unless the screener already did (FK target for
    approvals and ledger rows)."""
    from app.models import Screening
    if db.session.get(Screening, v["id"]) is not None:
        return
    subject, signals = v.get("subject") or {}, v.get("signals") or {}
    expires = v.get("expires_at")
    db.session.add(Screening(
        id=v["id"], hop=v.get("hop") or hop, engagement_id=engagement.id, agent_id=agent.id,
        chain_address=(subject.get("chain_address") or chain_address).lower(),
        screened_address=subject.get("screened_address"), network=subject.get("network"),
        verdict=v["verdict"], cap_micro=v.get("cap_micro"), reasons=v.get("reasons") or [],
        toxic_score=signals.get("toxic_score"), traits=signals.get("traits") or [],
        risk_group=signals.get("risk_group"), raw=v, provider=v.get("provider") or "",
        fail_closed=bool(v.get("fail_closed")), latency_ms=v.get("latency_ms"),
        expires_at=datetime.fromtimestamp(expires, tz=timezone.utc) if expires else None,
    ))
    db.session.flush()


def screening_json(v: dict | None) -> dict | None:
    if not v:
        return None
    return {k: v.get(k) for k in ("id", "hop", "verdict", "cap_micro", "reasons",
                                  "fail_closed", "provider", "expires_at")}


# ── approvals (§3) ────────────────────────────────────────────────────────
def _approvals():
    try:
        return importlib.import_module("app.approvals.service")
    except ImportError:
        raise EngagementError("the approval service is not available", "APPROVALS_UNAVAILABLE",
                              503) from None


def _create_approval(kind: str, fields: dict, *, flow: str, engagement, milestone=None,
                     screening_id: str):
    """Hand the action's §1 fields to the approval service, which adds a
    fresh ``approval_id`` and ``exp`` (so the hash is single-use). An
    approval it creates ``blocked`` (screening REFUSE, banned buyer, weekly
    cap) is an error."""
    svc = _approvals()
    try:
        approval = svc.create_approval(
            kind, fields, flow=flow, engagement_id=engagement.id,
            milestone_id=milestone.id if milestone else None, agent_id=engagement.agent_id,
            screening_id=screening_id)
    except Exception as exc:
        code = getattr(exc, "code", None)
        if not code:
            raise
        db.session.rollback()
        raise EngagementError(str(exc), code, _HTTP_FOR_CODE.get(code, 409)) from None
    if approval.state == "blocked":
        code = approval.failure_code or "BLOCKED"
        text = getattr(svc, "failure_text", lambda c: c)(code) or "approval blocked"
        raise EngagementError(text, code, _HTTP_FOR_CODE.get(code, 403))
    return approval


def _start(approval):
    """Start the device flow right away so the response carries the user
    code. A provider error leaves the approval ``created``; it can be
    started again from its page."""
    if approval.flow != "device" or approval.state != "created":
        return approval
    try:
        return _approvals().start_device(approval)
    except Exception as exc:
        log.warning("device start failed for %s: %s", approval.id, str(exc)[:200])
        raise EngagementError("could not start sign-in with the identity provider; open the "
                              f"approval page to retry: /approvals/{approval.id}",
                              "IDP_ERROR", 502) from None


def _refuse(engagement, verdict: dict, *, terminal: bool,
            message: str = "payment refused by risk screening") -> None:
    """No approval is created. A definitive REFUSE at hire ends the job; a
    fail-closed refusal (screener down) does not."""
    if terminal and not verdict.get("fail_closed"):
        engagement.status = "refused"
    db.session.commit()
    raise EngagementError(message, "SCREENING_REFUSED", 403, screening=screening_json(verdict))


def _cap_of(verdict: dict) -> int:
    cap = verdict.get("cap_micro")
    if isinstance(cap, int) and not isinstance(cap, bool) and cap > 0:
        return cap
    return _default_cap_micro()


# ── scope ─────────────────────────────────────────────────────────────────
def create_engagement(*, agent, outcome, budget_micro: int, milestones=None, deadline=None):
    """Build the SOW, persist the engagement as ``scoped`` and return
    ``(engagement, screening_preview)``."""
    from app.models import Engagement, Milestone
    try:
        plan = normalize_milestones(milestones, budget_micro)
        deadline_ts = parse_deadline(deadline)
        sow = build_sow(agent_public_id=agent.public_id, outcome=outcome, budget_micro=budget_micro,
                        milestones=plan, deadline=deadline_ts, category=agent.category)
    except SowError as exc:
        raise EngagementError(str(exc), exc.code, 400, exc.field) from None
    eng = Engagement(
        agent_id=agent.id, outcome=sow["outcome"], category=agent.category,
        sow_json=sow_json(sow), sow_hash=sow_hash(sow), total_micro=budget_micro,
        status="scoped",
        deadline_at=datetime.fromtimestamp(deadline_ts, tz=timezone.utc) if deadline_ts else None,
    )
    for m in sow["milestones"]:
        eng.milestones.append(Milestone(idx=m["idx"], title=m["title"], acceptance=m["acceptance"],
                                        amount_micro=m["amount_micro"]))
    db.session.add(eng)
    db.session.flush()
    preview = None
    payee = payee_address(agent)
    if payee:
        preview = screen("engagement.fund", chain_address=payee, amount_micro=budget_micro,
                         engagement=eng, agent=agent)
    db.session.commit()
    return eng, preview


# ── hire → engagement.fund approval ───────────────────────────────────────
def hire(eng, *, flow: str, confirm_micro: int):
    if eng.status not in HIREABLE:
        raise EngagementError(f"engagement is {eng.status}", "NOT_HIREABLE", 409)
    if confirm_micro != eng.total_micro:
        raise EngagementError(f"confirm_amount_usdc must equal the SOW total "
                              f"{format_usdc(eng.total_micro)}", "AMOUNT_MISMATCH", 400,
                              "confirm_amount_usdc")
    agent = eng.agent
    payee = payee_address(agent)
    if payee is None:
        raise EngagementError("agent has no payout address", "PAYEE_ADDRESS_MISSING", 409)
    verdict = screen("engagement.fund", chain_address=payee, amount_micro=eng.total_micro,
                     engagement=eng, agent=agent)
    if verdict["verdict"] == "REFUSE":
        _refuse(eng, verdict, terminal=True)
    if verdict["verdict"] == "CAP" and eng.total_micro > _cap_of(verdict):
        # The escrow is funded in full, and consume() re-screens the same
        # amount; a capped payee can only take a job within the cap.
        _refuse(eng, verdict, terminal=False,
                message=f"risk screening caps payments to this agent at "
                        f"{format_usdc(_cap_of(verdict))}; scope a smaller job")
    approval = _create_approval("engagement.fund", {
        "engagement_id": eng.id, "sow_hash": eng.sow_hash, "amount_micro": eng.total_micro,
        "payee_agent_id": agent.public_id, "payee_address": payee,
        "milestones": [{"idx": m.idx, "amount_micro": m.amount_micro, "title_hash": title_hash(m.title)}
                       for m in eng.milestones],
        "screening_id": verdict["id"], "screening_ack": verdict["verdict"] == "ASK_HUMAN",
    }, flow=flow, engagement=eng, screening_id=verdict["id"])
    eng.status = "awaiting_approval"
    db.session.commit()
    return _start(approval)


# ── submit ────────────────────────────────────────────────────────────────
def submit_milestone(eng, idx, evidence) -> str:
    """Mark a funded milestone as delivered. Returns the evidence digest
    (the evidence text itself is not stored)."""
    m = get_milestone(eng, idx)
    evidence = str(evidence or "").strip()
    if not evidence or len(evidence) > 4000:
        raise EngagementError("evidence is required (at most 4000 characters)",
                              field="evidence")
    if eng.status not in ACTIVE or m.status != "funded":
        raise EngagementError(f"milestone is {m.status}", "MILESTONE_NOT_SUBMITTABLE", 409)
    m.status = "submitted"
    m.submitted_at = datetime.now(timezone.utc)
    eng.status = "in_progress"
    db.session.commit()
    return "0x" + hashlib.sha256(evidence.encode("utf-8")).hexdigest()


# ── release → milestone.release approval ──────────────────────────────────
def request_release(eng, idx, *, flow: str):
    """Screen and create a fresh release approval for what is still owed on
    milestone ``idx``. CAP limits the amount; the rest is held at execution."""
    m = get_milestone(eng, idx)
    if eng.status not in ACTIVE or m.status not in RELEASABLE:
        raise EngagementError(f"milestone is {m.status}", "MILESTONE_NOT_RELEASABLE", 409)
    remaining = m.amount_micro - ledger.released_micro(m)
    if remaining <= 0:
        raise EngagementError("milestone is fully released", "MILESTONE_NOT_RELEASABLE", 409)
    agent = eng.agent
    payee = payee_address(agent)
    if payee is None:
        raise EngagementError("agent has no payout address", "PAYEE_ADDRESS_MISSING", 409)
    verdict = screen("milestone.release", chain_address=payee, amount_micro=remaining,
                     engagement=eng, agent=agent)
    if verdict["verdict"] == "REFUSE":
        _refuse(eng, verdict, terminal=False)
    amount = min(remaining, _cap_of(verdict)) if verdict["verdict"] == "CAP" else remaining
    approval = _create_approval("milestone.release", {
        "engagement_id": eng.id, "sow_hash": eng.sow_hash, "amount_micro": amount,
        "payee_agent_id": agent.public_id, "payee_address": payee, "milestone_idx": m.idx,
        "screening_id": verdict["id"], "screening_ack": verdict["verdict"] == "ASK_HUMAN",
    }, flow=flow, engagement=eng, milestone=m, screening_id=verdict["id"])
    db.session.commit()
    return _start(approval)


# ── executors (run inside approvals.consume) ──────────────────────────────
@dataclass
class _Target:
    eng: object
    payee: str


def _load_target(action: dict):
    """Re-check the approved action against current state. Returns
    (_Target, None) or (None, reason)."""
    eng = _lock_engagement(action.get("engagement_id"))
    if eng is None:
        return None, "engagement not found"
    if action.get("sow_hash") != eng.sow_hash:
        return None, "statement of work changed since approval"
    payee = payee_address(eng.agent)
    if payee is None or action.get("payee_address") != payee:
        return None, "payee address changed since approval"
    if action.get("payee_agent_id") != eng.agent.public_id:
        return None, "payee agent does not match"
    return _Target(eng, payee), None


def _lock_engagement(engagement_id):
    """Load the engagement under a write lock so concurrent executors for the
    same engagement serialize (the no-op UPDATE takes the lock on SQLite,
    where FOR UPDATE is ignored)."""
    from app.models import Engagement
    if not isinstance(engagement_id, str):
        return None
    db.session.execute(update(Engagement).where(Engagement.id == engagement_id)
                       .values(status=Engagement.status))
    return db.session.execute(
        select(Engagement).where(Engagement.id == engagement_id).with_for_update()
        .execution_options(populate_existing=True)).scalar_one_or_none()


def _claim_buyer(eng, approval) -> None:
    """The engagement's human is whoever approves first (§7)."""
    if eng.buyer_human_id is not None or not getattr(approval, "human_sub", None):
        return
    from app.models import Human
    human = Human.query.filter_by(world_sub=approval.human_sub).first()
    if human is not None:
        eng.buyer_human_id = human.id


def _failed(summary: str) -> ExecutionResult:
    log.warning("escrow executor refused: %s", summary)
    return ExecutionResult(ok=False, summary=summary)


@executor("engagement.fund")
def execute_fund(approval, action: dict) -> ExecutionResult:
    target, problem = _load_target(action)
    if problem:
        return _failed(problem)
    eng = target.eng
    if action.get("amount_micro") != eng.total_micro:
        return _failed("amount does not match the engagement total")
    expected = [{"idx": m.idx, "amount_micro": m.amount_micro, "title_hash": title_hash(m.title)}
                for m in eng.milestones]
    if action.get("milestones") != expected:
        return _failed("milestones changed since approval")
    if eng.status not in HIREABLE or ledger.has_live_fund(eng.id):
        return _failed("engagement is already funded")
    escrow = get_escrow()
    try:
        tx = escrow.fund_from_vault(amount_micro=eng.total_micro, ref=f"{eng.id}:fund#{ledger.failed_count(eng.id, 'fund')}")
    except EscrowError as exc:
        return _failed(f"escrow refused funding: {exc.code}")
    except Exception as exc:
        return _failed(f"funding failed: {str(exc)[:200]}")
    entry = ledger.record(eng, kind="fund", amount_micro=eng.total_micro, tx=tx,
                          from_addr=getattr(escrow, "vault_address", None),
                          to_addr=getattr(escrow, "escrow_address", None),
                          approval_id=approval.id, screening_id=action.get("screening_id"))
    for m in eng.milestones:
        m.status = "funded"
    eng.status = "funded"
    _claim_buyer(eng, approval)
    db.session.flush()
    return ExecutionResult(ok=True, summary=f"Funded {format_usdc(eng.total_micro)} into escrow",
                           ledger_ids=[entry.id], redirect=f"/jobs/{eng.id}")


@executor("milestone.release")
def execute_release(approval, action: dict) -> ExecutionResult:
    target, problem = _load_target(action)
    if problem:
        return _failed(problem)
    eng = target.eng
    m = next((m for m in eng.milestones if m.idx == action.get("milestone_idx")), None)
    if m is None:
        return _failed("milestone not found")
    if eng.status not in ACTIVE or m.status not in RELEASABLE:
        return _failed(f"milestone is {m.status}")
    already = ledger.released_micro(m)
    remaining = m.amount_micro - already
    amount = action.get("amount_micro")
    if not isinstance(amount, int) or amount <= 0 or amount > remaining:
        return _failed("release amount exceeds what is still owed on the milestone")
    escrow = get_escrow()
    try:
        tx = escrow.release(to=target.payee, amount_micro=amount,
                            ref=f"{eng.id}:m{m.idx}:release@{already}"
                                f"#{ledger.failed_count(eng.id, 'release', m.id)}")
    except EscrowError as exc:
        return _failed(f"escrow refused release: {exc.code}")
    except Exception as exc:
        return _failed(f"release failed: {str(exc)[:200]}")
    rel = ledger.record(eng, kind="release", amount_micro=amount, milestone=m, tx=tx,
                        from_addr=getattr(escrow, "escrow_address", None), to_addr=target.payee,
                        approval_id=approval.id, screening_id=action.get("screening_id"))
    ids = [rel.id]
    held = remaining - amount
    if held > 0:
        hold = ledger.record(eng, kind="hold", amount_micro=held, milestone=m,
                             status="simulated" if tx.status == "simulated" else "confirmed",
                             approval_id=approval.id, screening_id=action.get("screening_id"))
        ids.append(hold.id)
        m.status = "held"
    else:
        m.status = "released"
    m.released_ledger_id = rel.id
    eng.status = "completed" if all(x.status == "released" for x in eng.milestones) \
        else "in_progress"
    _claim_buyer(eng, approval)
    db.session.flush()
    summary = f"Released {format_usdc(amount)} for milestone #{m.idx + 1}"
    if held:
        summary += f"; {format_usdc(held)} held in escrow"
    return ExecutionResult(ok=True, summary=summary, ledger_ids=ids, redirect=f"/jobs/{eng.id}")


# ── serialization (§7) ────────────────────────────────────────────────────
def milestone_json(m) -> dict:
    return {"idx": m.idx, "title": m.title, "acceptance": m.acceptance,
            "amount_micro": int(m.amount_micro), "released_micro": ledger.released_micro(m),
            "status": m.status, "title_hash": title_hash(m.title),
            "submitted_at": unix(m.submitted_at), "released_ledger_id": m.released_ledger_id}


def approval_json(approval) -> dict:
    """The §7 approval object (the approval service's ``to_dict``), with the
    result's ledger ids and transaction filled in from this engagement's
    ledger, plus ``milestone_idx`` and the approval page ``url``."""
    from app.models import LedgerEntry
    try:
        out = dict(_approvals().to_dict(approval))
    except (EngagementError, AttributeError):
        out = {"approval_id": approval.id, "kind": approval.kind, "state": approval.state,
               "flow": approval.flow, "user_code": approval.user_code,
               "verification_uri": approval.verification_uri,
               "verification_uri_complete": approval.verification_uri_complete,
               "expires_at": unix(approval.expires_at), "action_hash": approval.action_hash,
               "summary": [list(row) for row in describe(approval.action)],
               "screening": None, "failure_code": approval.failure_code, "result": None}
    entries = (LedgerEntry.query.filter_by(approval_id=approval.id)
               .order_by(LedgerEntry.created_at).all())
    if entries:
        tx = next((ledger.entry_json(e) for e in entries if e.kind != "hold"), None)
        out["result"] = {**(out.get("result") or {}),
                         "ledger_ids": [e.id for e in entries],
                         "tx": {"tx_hash": tx["tx_hash"], "status": tx["status"],
                                "explorer": tx["explorer"]} if tx else None}
    out["milestone_idx"] = approval.action.get("milestone_idx")
    out["url"] = f"/approvals/{approval.id}"
    return out


def engagement_json(eng, *, detail: bool = False, with_token: bool = False) -> dict:
    """The §7 engagement object. ``with_token`` adds ``mandate_token`` (the
    grantee's signed mandate); callers must have authenticated first."""
    agent = eng.agent
    out = {
        "engagement_id": eng.id, "agent_id": agent.public_id if agent else None,
        "agent_name": agent.name if agent else None, "status": eng.status,
        "outcome": eng.outcome, "category": eng.category,
        "sow": json.loads(eng.sow_json) if eng.sow_json else None, "sow_hash": eng.sow_hash,
        "total_micro": int(eng.total_micro), "currency": eng.currency,
        "deadline_at": unix(eng.deadline_at), "buyer_address": eng.buyer_address,
        "parent_engagement_id": eng.parent_engagement_id, "depth": eng.depth,
        "milestones": [milestone_json(m) for m in eng.milestones],
        "created_at": unix(eng.created_at), "updated_at": unix(eng.updated_at),
    }
    if detail:
        from app.models import Approval
        approvals = Approval.query.filter_by(engagement_id=eng.id).order_by(Approval.created_at).all()
        out.update(
            ledger=[ledger.entry_json(e) for e in ledger.entries(eng.id)],
            approvals=[approval_json(a) for a in approvals],
            mandate={"mandate_id": eng.mandate_id, "url": f"/api/mandates/{eng.mandate_id}"}
            if eng.mandate_id else None,
            chain_url=f"/api/engagements/{eng.id}/chain",
            escrow_mode=get_escrow().mode,
            page_url=f"/jobs/{eng.id}",
            chain_page_url=f"/jobs/{eng.id}/chain",
        )
    if with_token:
        out["mandate_token"] = _mandate_token(eng)
    return out


def _mandate_token(eng) -> str | None:
    from app.models import Mandate
    row = db.session.get(Mandate, eng.mandate_id) if eng.mandate_id else None
    return row.token if row is not None and row.revoked_at is None else None


MANDATE_DEFAULT_DAYS = 30


def ensure_root_mandate(eng) -> str | None:
    """Mint the hired agent's root mandate (§5) from the engagement's consumed
    ``engagement.fund`` approval: budget = SOW total, categories =
    [engagement category], max_depth = MANDATE_MAX_DEPTH, expiry = the SOW
    deadline (else 30 days). Idempotent; a failure is logged and returns None.

    Runs from the ``engagement.fund`` after-consume hook below, once the
    approval is committed as ``consumed`` (``issue_root`` requires that and
    commits its own transaction, so it cannot run inside the executor)."""
    from app.models import Approval, Human
    if eng.mandate_id or eng.status not in ("funded", "in_progress", "completed"):
        return eng.mandate_id
    approval = (Approval.query.filter_by(engagement_id=eng.id, kind="engagement.fund",
                                         state="consumed")
                .order_by(Approval.consumed_at.desc()).first())
    if approval is None or not approval.human_sub:
        return None
    human = Human.query.filter_by(world_sub=approval.human_sub).first()
    if human is None:
        return None
    try:
        from app.mandates import service as mandates
    except ImportError:
        return None
    now = int(time.time())
    deadline = unix(eng.deadline_at)
    exp = deadline if deadline and deadline > now else now + MANDATE_DEFAULT_DAYS * 86400
    try:
        row = mandates.issue_root(eng, human, approval, eng.agent.public_id, int(eng.total_micro),
                                  [eng.category or "General"], mandates.max_depth_limit(),
                                  int(eng.total_micro), exp)
    except Exception as exc:
        db.session.rollback()
        log.warning("root mandate for %s not issued: %s", eng.id, str(exc)[:200])
        return None
    eng.mandate_id = row.id
    db.session.commit()
    return row.id


@after_consume("engagement.fund")
def _after_fund(approval, action: dict) -> None:
    """Right after funding commits: mint the root mandate, then name the job
    (names are never fatal)."""
    from app.models import Engagement
    eng = db.session.get(Engagement, action.get("engagement_id") or "")
    if eng is None:
        return
    ensure_root_mandate(eng)
    try:
        from app.names import service as names
        names.on_engagement_funded(eng)
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        log.warning("job name for %s not issued: %s", eng.id, str(exc)[:200])


def refresh(eng) -> None:
    """Poll pending escrow receipts for this engagement (cheap no-op when
    simulated) and commit any change."""
    if ledger.refresh_receipts(eng, get_escrow()):
        db.session.commit()
