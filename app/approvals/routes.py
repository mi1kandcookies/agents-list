"""Approval pages and JSON API (docs/decisions/0001-custody-chain.md §3, §7).

    GET  /approvals/<id>             status page (action summary, live state)
    GET  /approvals/<id>/start       start the web flow (redirect to World ID)
                                     or the device flow (back to the status page)
    GET  /api/approvals/<id>         approval object; advances device flows
    POST /api/approvals/<id>/cancel  cancel a created / pending approval

An approval id is an unguessable capability: anyone holding it can see the
action summary and cancel it, but only the human who authenticates with World
ID can approve it.
"""
from __future__ import annotations

from flask import jsonify, redirect, render_template, request, url_for

from app.approvals import actions, bp, service
from app.identity.session import remember_web_state
from app.identity.world import WorldError

PILL = {
    "created": ("Waiting", "badge-pending"),
    "pending": ("Waiting for approval", "badge-review"),
    "approved": ("Approved", "badge-approved"),
    "consumed": ("Approved", "badge-approved"),
    "denied": ("Denied", "badge-rejected"),
    "expired": ("Expired", "badge-pending"),
    "cancelled": ("Cancelled", "badge-pending"),
    "rejected": ("Rejected", "badge-rejected"),
    "blocked": ("Blocked", "badge-rejected"),
    "failed": ("Failed", "badge-rejected"),
}


def _api_error(message: str, code: str, status: int, field=None):
    return jsonify({"error": message, "code": code, "field": field}), status


def _find(approval_id: str):
    try:
        return service.get(approval_id)
    except service.ApprovalNotFound:
        return None


@bp.get("/approvals/<approval_id>")
def status(approval_id):
    approval = _find(approval_id)
    if approval is None:
        return render_template("404.html"), 404
    approval = service.poll(approval)  # expire / advance before rendering
    return render_template(
        "approvals/status.html",
        approval=approval,
        summary=actions.describe(approval.action),
        kind_label=actions.KINDS.get(approval.kind, approval.kind),
        pill=PILL.get(approval.state, (approval.state, "badge-pending")),
        terminal=approval.state in service.TERMINAL,
        expires_at=service.unix(approval.expires_at),
        result=service.result_of(approval),
        failure=service.failure_text(approval.failure_code),
        replays=service.replay_attempts(approval),
        start_error=request.args.get("error"),
        **_page_context(approval),
    )


def _page_context(approval) -> dict:
    """Display-only extras for the status page: who is paid, how much, the
    bound screening verdict, and the ledger movement the approval caused."""
    from app.engagements.ledger import entry_json
    from app.extensions import db
    from app.models import Agent, LedgerEntry, Screening
    action = approval.action
    agent = db.session.get(Agent, approval.agent_id) if approval.agent_id else None
    if agent is None and action.get("payee_agent_id"):
        agent = Agent.query.filter_by(public_id=action["payee_agent_id"]).first()
    screening = None
    if approval.screening_id:
        from app.screening.service import verdict_from_row
        row = db.session.get(Screening, approval.screening_id)
        screening = verdict_from_row(row) if row else None
    entries = [entry_json(e) for e in LedgerEntry.query.filter_by(approval_id=approval.id)
               .order_by(LedgerEntry.created_at).all()]
    return {
        "agent": agent,
        "amount_micro": action.get("amount_micro"),
        "screening": screening,
        "ledger": entries,
        "job_url": f"/jobs/{approval.engagement_id}" if approval.engagement_id else None,
    }


@bp.get("/approvals/<approval_id>/start")
def start(approval_id):
    approval = _find(approval_id)
    if approval is None:
        return render_template("404.html"), 404
    back = url_for("approvals.status", approval_id=approval.id)
    if approval.state not in ("created", "pending"):
        return redirect(back)
    try:
        if approval.flow == "web":
            url = service.start_web(approval)
            remember_web_state(approval.state_param, approval.id)
            return redirect(url)
        service.start_device(approval)
    except service.ApprovalStateError:
        pass
    except WorldError:
        return redirect(url_for("approvals.status", approval_id=approval.id, error="provider"))
    return redirect(back)


@bp.get("/api/approvals/<approval_id>")
def api_get(approval_id):
    approval = _find(approval_id)
    if approval is None:
        return _api_error("approval not found", "NOT_FOUND", 404)
    approval = service.poll(approval)
    return jsonify(service.to_dict(approval))


@bp.post("/api/approvals/<approval_id>/cancel")
def api_cancel(approval_id):
    body = request.get_json(silent=True) or {}
    reason = str(body.get("reason") or request.form.get("reason") or "cancelled by user")[:500]
    try:
        approval = service.cancel(approval_id, reason)
    except service.ApprovalNotFound:
        return _api_error("approval not found", "NOT_FOUND", 404)
    except service.ApprovalStateError as exc:
        return _api_error(str(exc), "INVALID_STATE", 409, "state")
    return jsonify(service.to_dict(approval))
