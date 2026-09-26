"""Engagement routes: the §7 JSON API under /api/engagements and the buyer
pages under /jobs (docs/decisions/0001-custody-chain.md).

Request bodies are read field by field; anything else a client sends (for
example a "screening" verdict) is ignored. Screening always runs server-side.
"""
from __future__ import annotations

import functools
import hmac
import os

from flask import current_app, g, jsonify, redirect, render_template, request, url_for

from app.approvals.actions import format_usdc
from app.engagements import bp
from app.engagements import service as svc
from app.engagements.service import EngagementError
from app.engagements.sow import SowError, parse_usdc
from app.extensions import db

FLOWS = ("device", "web")


@bp.app_template_filter("usdc")
def _usdc_filter(micro) -> str:
    return format_usdc(int(micro or 0))


def _error(exc: EngagementError):
    body = {"error": exc.message, "code": exc.code}
    if exc.field:
        body["field"] = exc.field
    if exc.screening:
        body["screening"] = exc.screening
    return jsonify(body), exc.status


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
            if not hmac.compare_digest(provided.encode(), f"Bearer {token}".encode()):
                return jsonify({"error": "missing or invalid bearer token",
                                "code": "UNAUTHORIZED"}), 401
            g.api_token_ok = True
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
        milestones=body.get("milestones"), deadline=body.get("deadline"))
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


# ── pages ─────────────────────────────────────────────────────────────────
@bp.route("/jobs")
def jobs_list():
    from app.models import Engagement
    rows = Engagement.query.order_by(Engagement.created_at.desc()).limit(100).all()
    return render_template("jobs/list.html", jobs=[svc.engagement_json(e) for e in rows])


@bp.route("/jobs/new", methods=["GET", "POST"])
def jobs_new():
    from app.models import Agent
    agent = db.session.get(Agent, request.values.get("agent", type=int) or 0)
    if agent is None or agent.verification_tier == "suspended":
        return redirect(url_for("catalog.marketplace"))
    form = {"outcome": "", "budget_usdc": "", "deadline": "", "milestones": ""}
    error = None
    if request.method == "POST":
        form.update({k: request.form.get(k, "") for k in form})
        try:
            eng, _ = svc.create_engagement(
                agent=agent, outcome=form["outcome"],
                budget_micro=_usdc(form["budget_usdc"], "budget_usdc"),
                milestones=_milestone_lines(form["milestones"]), deadline=form["deadline"] or None)
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
    return render_template("jobs/detail.html", job=svc.engagement_json(eng, detail=True),
                           error=error), status


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
