"""Engagement routes: the §7 JSON API under /api/engagements and the buyer
pages under /jobs (docs/decisions/0001-custody-chain.md).

Request bodies are read field by field; anything else a client sends (for
example a "screening" verdict) is ignored. Screening always runs server-side.
"""
from __future__ import annotations

import functools
import hmac
import math
import os
import time
from datetime import datetime, timezone

from flask import current_app, g, jsonify, redirect, render_template, request, url_for

from app.approvals.actions import KINDS, format_usdc
from app.engagements import bp
from app.engagements import service as svc
from app.engagements import subhire as sub
from app.engagements.ledger import LIVE, unix
from app.engagements.service import ACTIVE, RELEASABLE, EngagementError
from app.engagements.sow import SowError, parse_usdc
from app.extensions import db

FLOWS = ("device", "web")


@bp.app_template_filter("usdc")
def _usdc_filter(amount, scale: str = "micro", unit: bool = True) -> str:
    """``{{ m.amount_micro|usdc }}`` → "900 USDC"; see app.common.money."""
    return format_usdc(amount, scale, unit=unit)


@bp.app_template_filter("utc")
def _utc_filter(ts, fmt: str = "%Y-%m-%d %H:%M UTC") -> str:
    """Unix seconds → UTC text; '' for None."""
    if not ts:
        return ""
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime(fmt)


def _error(exc: EngagementError):
    body = {"error": exc.message, "code": exc.code}
    if exc.field:
        body["field"] = exc.field
    if exc.screening:
        body["screening"] = exc.screening
    return jsonify(body), exc.status


def _same_origin_browser() -> bool:
    """True for requests made by our own pages in a browser.

    Browsers set Sec-Fetch-Site on every fetch and scripts cannot forge it;
    Origin is the fallback for older browsers. Other sites and non-browser
    clients don't qualify and must present the bearer token."""
    site = request.headers.get("Sec-Fetch-Site")
    if site is not None:
        return site == "same-origin"
    origin = request.headers.get("Origin")
    return bool(origin) and origin.rstrip("/") == request.host_url.rstrip("/")


def _api(fn):
    """Bearer MCP_API_TOKEN when configured (no-op otherwise, like
    require_api_key); EngagementError → {error, code, field}. Sets
    ``g.api_token_ok`` only when a configured token was presented."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        token = current_app.config.get("MCP_API_TOKEN") or os.environ.get("MCP_API_TOKEN")
        g.api_token_ok = False
        if token:
            provided = request.headers.get("Authorization", "")
            if hmac.compare_digest(provided.encode(), f"Bearer {token}".encode()):
                g.api_token_ok = True
            elif not _same_origin_browser():
                return jsonify({"error": "missing or invalid bearer token",
                                "code": "UNAUTHORIZED"}), 401
            # Same-origin browser calls (our own pages) are allowed without the
            # token but never get g.api_token_ok, so they can't read mandate
            # tokens; money still moves only through a World ID approval.
        try:
            return fn(*args, **kwargs)
        except EngagementError as exc:
            return _error(exc)
    return wrapper


def _body() -> dict:
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise EngagementError("request body must be a JSON object")
    return body


def _usdc(value, field: str) -> int:
    try:
        return parse_usdc(value, field)
    except SowError as exc:
        raise EngagementError(str(exc), exc.code, 400, exc.field) from None


def _flow(value, default: str) -> str:
    flow = value or default
    if flow not in FLOWS:
        raise EngagementError("flow must be 'device' or 'web'", field="flow")
    return flow


# ── JSON API ──────────────────────────────────────────────────────────────
@bp.route("/api/engagements", methods=["POST"])
@_api
def api_create():
    body = _body()
    agent = svc.resolve_agent(body.get("agent_id"))
    eng, preview = svc.create_engagement(
        agent=agent, outcome=body.get("outcome"),
        budget_micro=_usdc(body.get("budget_usdc"), "budget_usdc"),
        milestones=body.get("milestones"), deadline=body.get("deadline"),
        source_document=body.get("source_document"))
    return jsonify({**svc.engagement_json(eng), "screening": svc.screening_json(preview)}), 201


@bp.route("/api/engagements/<engagement_id>")
@_api
def api_get(engagement_id):
    eng = svc.get_engagement(engagement_id)
    svc.refresh(eng)
    # The hired agent's mandate token is a bearer credential: only callers
    # holding the configured API token (the agent's MCP server) receive it.
    return jsonify(svc.engagement_json(eng, detail=True, with_token=g.api_token_ok))


@bp.route("/api/engagements/<engagement_id>/hire", methods=["POST"])
@_api
def api_hire(engagement_id):
    body = _body()
    eng = svc.get_engagement(engagement_id)
    approval = svc.hire(eng, flow=_flow(body.get("flow"), "device"),
                        confirm_micro=_usdc(body.get("confirm_amount_usdc"), "confirm_amount_usdc"))
    return jsonify(svc.approval_json(approval)), 202


@bp.route("/api/engagements/<engagement_id>/milestones/<int:idx>/submit", methods=["POST"])
@_api
def api_submit(engagement_id, idx):
    body = _body()
    eng = svc.get_engagement(engagement_id)
    evidence_hash = svc.submit_milestone(eng, idx, body.get("evidence"))
    return jsonify({"engagement_id": eng.id, "evidence_hash": evidence_hash,
                    "milestone": svc.milestone_json(svc.get_milestone(eng, idx))}), 200


@bp.route("/api/engagements/<engagement_id>/milestones/<int:idx>/release", methods=["POST"])
@_api
def api_release(engagement_id, idx):
    body = _body()
    eng = svc.get_engagement(engagement_id)
    approval = svc.request_release(eng, idx, flow=_flow(body.get("flow"), "device"))
    return jsonify(svc.approval_json(approval)), 202


@bp.route("/api/engagements/<engagement_id>/subhire", methods=["POST"])
def api_subhire(engagement_id):
    """Authenticated by the caller's mandate (``Authorization: Mandate
    <jwt>``), not the API token: the mandate is the authority being used."""
    try:
        token = sub.mandate_from_header(request.headers.get("Authorization"))
        body = _body()
        status, out = sub.subhire(
            engagement_id, token, agent_ref=body.get("agent_id"), outcome=body.get("outcome"),
            budget_micro=_usdc(body.get("budget_usdc"), "budget_usdc"),
            category=body.get("category"))
    except EngagementError as exc:
        return _error(exc)
    return jsonify(out), status


# ── pages ─────────────────────────────────────────────────────────────────
# Status filter chips on /jobs: key → (label, engagement statuses).
JOB_FILTERS = {
    "all": ("All", None),
    "approval": ("Needs approval", ("draft", "scoped", "awaiting_approval")),
    "active": ("Active", ACTIVE),
    "completed": ("Completed", ("completed",)),
    "closed": ("Closed", ("refused", "cancelled")),
}

# Approval state → (label, status_pill state) on job pages.
APPROVAL_PILLS = {
    "created": ("Waiting", "pending"), "pending": ("Waiting for you", "pending"),
    "approved": ("Approved", "approved"), "consumed": ("Done", "approved"),
    "denied": ("Denied", "denied"), "rejected": ("Rejected", "denied"),
    "blocked": ("Blocked", "denied"), "failed": ("Failed", "denied"),
    "expired": ("Expired", "expired"), "cancelled": ("Cancelled", "expired"),
}
OPEN_APPROVAL = ("created", "pending", "approved")


@bp.route("/jobs")
def jobs_list():
    """The signed-in human's jobs. Signed out, development and test servers
    list every job; production asks the visitor to sign in."""
    from app.identity.session import current_human
    from app.models import Engagement
    human = current_human()
    query = Engagement.query
    if human is not None:
        scope = "mine"
        query = query.filter(Engagement.buyer_human_id == human.id)
    elif current_app.debug or current_app.testing or \
            str(current_app.config.get("ENV_NAME", "")).lower() == "development":
        scope = "all"
    else:
        return render_template("jobs/list.html", jobs=[], scope="signed_out", counts={},
                               filters=JOB_FILTERS, active="all")
    jobs = [_list_row(svc.engagement_json(e))
            for e in query.order_by(Engagement.created_at.desc()).limit(200).all()]
    counts = {key: sum(1 for j in jobs if statuses is None or j["status"] in statuses)
              for key, (_, statuses) in JOB_FILTERS.items()}
    active = request.args.get("status", "all")
    if active not in JOB_FILTERS:
        active = "all"
    statuses = JOB_FILTERS[active][1]
    shown = [j for j in jobs if statuses is None or j["status"] in statuses]
    return render_template("jobs/list.html", jobs=shown, scope=scope, counts=counts,
                           filters=JOB_FILTERS, active=active)


def _list_row(job: dict) -> dict:
    ms = job["milestones"]
    return {**job, "released_micro": sum(m["released_micro"] for m in ms),
            "milestones_done": sum(1 for m in ms if m["status"] == "released")}


@bp.route("/jobs/new", methods=["GET", "POST"])
def jobs_new():
    from app.models import Agent
    agent = db.session.get(Agent, request.values.get("agent", type=int) or 0)
    if agent is None or agent.verification_tier == "suspended":
        return redirect(url_for("catalog.marketplace"))
    from app.seller.stamp import stamp_status
    stamp = stamp_status(agent)
    if not stamp.ok:
        # Hiring needs a valid operator stamp; say why instead of a form that
        # would fail at approval.
        return render_template("jobs/new.html", agent=agent, form=None, error=None,
                               not_hireable=stamp.reason), 409 if request.method == "POST" else 200
    form = {"outcome": "", "budget_usdc": "", "deadline": "", "milestones": "",
            "source_filename": "", "source_sha256": ""}
    error = None
    if request.method == "POST":
        form.update({k: request.form.get(k, "") for k in form})
        try:
            eng, _ = svc.create_engagement(
                agent=agent, outcome=form["outcome"],
                budget_micro=_usdc(form["budget_usdc"], "budget_usdc"),
                milestones=_milestone_lines(form["milestones"]), deadline=form["deadline"] or None,
                source_document={"filename": form["source_filename"], "sha256": form["source_sha256"]}
                if form["source_sha256"] else None)
            return redirect(url_for("engagements.jobs_detail", engagement_id=eng.id))
        except EngagementError as exc:
            error = exc.message
    return render_template("jobs/new.html", agent=agent, form=form, error=error), \
        400 if error else 200


def _milestone_lines(text: str) -> list[dict] | None:
    """One milestone per line: ``title | acceptance | amount_usdc``."""
    out = []
    for n, line in enumerate(l for l in (text or "").splitlines() if l.strip()):
        parts = [p.strip() for p in line.split("|")]
        if len(parts) != 3:
            raise EngagementError(f"milestone line {n + 1} needs: title | acceptance | amount",
                                  field="milestones")
        out.append({"title": parts[0], "acceptance": parts[1], "amount_usdc": parts[2]})
    return out or None


@bp.route("/jobs/<engagement_id>")
def jobs_detail(engagement_id, error: str | None = None, status: int = 200):
    try:
        eng = svc.get_engagement(engagement_id)
    except EngagementError:
        return render_template("404.html"), 404
    svc.refresh(eng)
    job = svc.engagement_json(eng, detail=True)
    return render_template("jobs/detail.html", job=job, view=_detail_view(eng, job),
                           error=error), status


@bp.route("/jobs/<engagement_id>/chain")
def jobs_chain(engagement_id):
    try:
        eng = svc.get_engagement(engagement_id)
    except EngagementError:
        return render_template("404.html"), 404
    return render_template("jobs/chain.html", chain=sub.chain_tree(eng))


def _criteria(acceptance: str) -> list[str]:
    """Acceptance text (one criterion per line, optional bullets) as a list."""
    lines = [ln.strip().lstrip("-*• ").strip() for ln in (acceptance or "").splitlines()]
    return [ln for ln in lines if ln]


def _detail_view(eng, job: dict) -> dict:
    """Display-only figures for the job page, derived from ``job`` (the §7
    engagement object) and the rows behind it. Changes nothing."""
    from app.intake.estimate import AUTO_RELEASE_DAYS
    from app.models import Approval
    ledger_rows = job["ledger"]
    live = [e for e in ledger_rows if e["status"] in LIVE]
    funded = sum(e["amount_micro"] for e in live if e["kind"] == "fund")
    released = sum(m["released_micro"] for m in job["milestones"])
    total = job["total_micro"] or 0
    in_escrow = max(funded - released, 0)
    fund_at = next((e["created_at"] for e in live if e["kind"] == "fund"), None)

    created = {a.id: unix(a.created_at)
               for a in Approval.query.filter_by(engagement_id=eng.id).all()}
    approvals = [{**a, "created_at": created.get(a["approval_id"]),
                  "kind_label": KINDS.get(a["kind"], a["kind"]),
                  "pill": APPROVAL_PILLS.get(a["state"], (a["state"], "unknown"))}
                 for a in job["approvals"]]
    idx_of = {m.id: m.idx for m in eng.milestones}
    auto_at = {m.idx: unix(m.auto_release_at) for m in eng.milestones}
    active = job["status"] in ACTIVE

    milestones = []
    for m in job["milestones"]:
        mine = [a for a in approvals if a["milestone_idx"] == m["idx"]]
        activity = []
        if fund_at and m["status"] != "pending":
            activity.append({"at": fund_at, "label": "Funded in escrow",
                             "amount": m["amount_micro"]})
        if m["submitted_at"]:
            activity.append({"at": m["submitted_at"], "label": "Delivered for review"})
        for a in mine:
            activity.append({"at": a["created_at"], "label": f"Release approval ({a['pill'][0].lower()})",
                             "href": a["url"], "ref": a["approval_id"]})
        for e in ledger_rows:
            if idx_of.get(e["milestone_id"]) != m["idx"]:
                continue
            label = {"release": "Released to agent", "hold": "Held in escrow"}.get(e["kind"],
                                                                                   e["kind"])
            activity.append({"at": e["created_at"], "amount": e["amount_micro"],
                             "label": label + (" (failed)" if e["status"] == "failed" else ""),
                             "href": e["explorer"], "ref": e["id"], "simulated": e["simulated"]})
        activity.sort(key=lambda x: x["at"] or 0)
        auto = auto_at.get(m["idx"])
        if auto is None and m["status"] == "submitted" and m["submitted_at"]:
            auto = m["submitted_at"] + AUTO_RELEASE_DAYS * 86400
        milestones.append({
            **m, "criteria": _criteria(m["acceptance"]), "activity": activity,
            "held_micro": m["amount_micro"] - m["released_micro"] if m["status"] == "held" else 0,
            "can_release": active and m["status"] in RELEASABLE,
            "open_approval": next((a for a in reversed(mine) if a["state"] in OPEN_APPROVAL),
                                  None),
            "auto_release_at": auto,
        })

    deadline = job["deadline_at"]
    return {
        "funded_micro": funded, "released_micro": released, "in_escrow_micro": in_escrow,
        "released_pct": round(100 * released / total, 1) if total else 0,
        "escrow_pct": round(100 * in_escrow / total, 1) if total else 0,
        "deadline_days": math.ceil((deadline - time.time()) / 86400) if deadline else None,
        "milestones": milestones, "approvals": approvals,
        "open_fund": next((a for a in reversed(approvals)
                           if a["kind"] == "engagement.fund" and a["state"] in OPEN_APPROVAL),
                          None),
        "agent": eng.agent.to_dict() if eng.agent else None,
        "names_root": _names_root(eng), "auto_release_days": AUTO_RELEASE_DAYS,
        "chain_page": job.get("chain_page_url") or f"/jobs/{eng.id}/chain",
    }


def _names_root(eng) -> str | None:
    """The job's issued name, if any (for the Names tab)."""
    if eng.ens_name:
        return eng.ens_name
    from app.models import EnsName
    row = (EnsName.query.filter(EnsName.engagement_id == eng.id)
           .order_by(EnsName.updated_at.desc()).first())
    return row.name if row else None


@bp.route("/jobs/<engagement_id>/hire", methods=["POST"])
def jobs_hire(engagement_id):
    try:
        eng = svc.get_engagement(engagement_id)
        approval = svc.hire(eng, flow="web", confirm_micro=eng.total_micro)
    except EngagementError as exc:
        return _page_error(engagement_id, exc)
    return redirect(f"/approvals/{approval.id}")


@bp.route("/jobs/<engagement_id>/milestones/<int:idx>/release", methods=["POST"])
def jobs_release(engagement_id, idx):
    try:
        eng = svc.get_engagement(engagement_id)
        approval = svc.request_release(eng, idx, flow="web")
    except EngagementError as exc:
        return _page_error(engagement_id, exc)
    return redirect(f"/approvals/{approval.id}")


def _page_error(engagement_id, exc: EngagementError):
    if exc.code == "ENGAGEMENT_NOT_FOUND":
        return render_template("404.html"), 404
    return jobs_detail(engagement_id, error=exc.message, status=exc.status)
