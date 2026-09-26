"""Admin pages and API-key protected moderation, verification and payout actions."""
from __future__ import annotations

import logging
import time

from flask import Blueprint, jsonify, redirect, render_template, request, url_for

from app.auth import require_api_key
from app.extensions import db
from app.services import api_error, get_agent, live_chain_stats

log = logging.getLogger(__name__)
bp = Blueprint("admin", __name__, url_prefix="/admin")


@bp.route("/dashboard")
def admin_dashboard():
    # Default to live-on-chain view so reviewers first-impression isn't 4k+
    # "active orders" from seeded sim ticks. Explicit ?all=1 flips to the
    # full aggregate; ?live_only=0 also respected for backwards compat.
    all_flag  = request.args.get("all", "").lower() in ("1", "true", "yes")
    live_raw  = request.args.get("live_only", "").lower()
    live_only = not all_flag if live_raw == "" else (live_raw in ("1", "true", "yes"))
    s = live_chain_stats(live_only=live_only)
    # Real per-hour revenue (last 24h) aggregated from ChainTransaction settlements.
    try:
        from app.models import ChainTransaction as CT
        from sqlalchemy import func as sfn
        now_ts = int(time.time())
        hourly = [0.0] * 24
        labels = []
        for i in range(23, -1, -1):
            hstart = now_ts - (i + 1) * 3600
            hend   = now_ts - i * 3600
            q = db.session.query(sfn.sum(CT.amount_usdc)).filter(
                CT.kind.in_(["settle", "a2a_settle", "a2a_hire"]),
                CT.ts >= hstart, CT.ts < hend,
            )
            if live_only:
                q = q.filter(CT.meta.like('%"real": true%'))
            total_micro = q.scalar() or 0
            hourly[23 - i] = round(total_micro / 1_000_000, 2)
            import datetime as _dt2
            labels.append(_dt2.datetime.fromtimestamp(hend, tz=_dt2.timezone.utc).strftime("%H:00"))
        s["hourly_revenue"] = hourly
        s["revenue_labels"] = labels
    except Exception as e:
        log.warning("hourly revenue aggregation failed: %s", e)
        s["hourly_revenue"] = [0] * 24
        s["revenue_labels"] = [f"{i:02d}:00" for i in range(24)]
    # Base/fee breakdown from settled txs. Protocol fee is bps.
    base_rev  = round(s.get("total_volume", 0) or 0, 2)
    fees      = round(s.get("platform_fees", 0) or 0, 4)
    s["breakdown_labels"] = ["Base Revenue", "Protocol Fees"]
    s["breakdown_values"] = [base_rev, fees]
    from app.models import Agent as AgentModel
    agents = [a.to_dict() for a in AgentModel.query.order_by(AgentModel.id.desc()).limit(8).all()]
    return render_template("admin/dashboard.html", stats=s, agents=agents)


@bp.route("/verification-queue")
def admin_verification_queue():
    from app.models import VerificationEntry
    queue = [v.to_dict() for v in VerificationEntry.query
             .order_by(VerificationEntry.created_at.desc()).all()]
    return render_template("admin/verification_queue.html", queue=queue)


@bp.route("/review/<vrf_id>", methods=["GET", "POST"])
def admin_human_review(vrf_id):
    """Human review panel for Thorough Audit tier agents."""
    from app.models import VerificationEntry
    row = db.session.get(VerificationEntry, vrf_id)
    if not row:
        return redirect(url_for("admin.admin_verification_queue"))
    if request.method == "POST":
        data = request.get_json(silent=True) or request.form.to_dict()
        action = data.get("action")
        if action == "approve":
            return admin_approve_verification(vrf_id)
        if action == "reject":
            return admin_reject_verification(vrf_id)
        return api_error("action must be approve or reject", field="action")
    agent = get_agent(row.agent_id) if row.agent_id else None
    return render_template("admin/review.html", entry=row.to_dict(), agent=agent)


@bp.route("/moderation")
def admin_moderation():
    from app.models import ModerationReport
    reports = [r.to_dict() for r in
               ModerationReport.query.order_by(ModerationReport.created_at.desc()).all()]
    return render_template("admin/moderation.html", reports=reports)


@bp.route("/payouts")
def admin_payouts():
    from app.models import Payout
    payouts = [p.to_dict() for p in
               Payout.query.order_by(Payout.created_at.desc()).all()]
    # Same default as /admin/dashboard — show live-on-chain numbers first.
    all_flag  = request.args.get("all", "").lower() in ("1", "true", "yes")
    live_raw  = request.args.get("live_only", "").lower()
    live_only = not all_flag if live_raw == "" else (live_raw in ("1", "true", "yes"))
    s = live_chain_stats(live_only=live_only)
    s["hourly_revenue"] = [0]*24
    s["revenue_labels"] = [f"{i:02d}:00" for i in range(24)]
    return render_template("admin/payouts.html", payouts=payouts, stats=s)


def _verification_or_404(vrf_id):
    from app.models import VerificationEntry
    entry = db.session.get(VerificationEntry, vrf_id)
    if not entry:
        return None, api_error("verification entry not found", 404, code="VERIFICATION_NOT_FOUND")
    return entry, None


def _set_verification_status(vrf_id, status, *, allowed_from=None):
    entry, err = _verification_or_404(vrf_id)
    if err:
        return err
    if allowed_from and entry.status not in allowed_from:
        return api_error(f"cannot move from '{entry.status}' to '{status}'", code="INVALID_STATE")
    entry.status = status
    if status == "approved" and entry.agent_id:
        from app.models import Agent as AgentModel
        ag = db.session.get(AgentModel, entry.agent_id)
        if ag:
            ag.verified = True
            ag.verification_tier = entry.tier
    db.session.commit()
    log.info("Verification %s -> %s", vrf_id, status)
    return jsonify({"id": vrf_id, "status": status})


@bp.route("/verification-queue/<vrf_id>/approve", methods=["POST"])
@require_api_key
def admin_approve_verification(vrf_id):
    return _set_verification_status(vrf_id, "approved",
                                    allowed_from={"pending", "testing", "human_review"})


@bp.route("/verification-queue/<vrf_id>/reject", methods=["POST"])
@require_api_key
def admin_reject_verification(vrf_id):
    return _set_verification_status(vrf_id, "rejected",
                                    allowed_from={"pending", "testing", "human_review"})


@bp.route("/verification-queue/<vrf_id>/test-start", methods=["POST"])
@require_api_key
def admin_start_testing(vrf_id):
    return _set_verification_status(vrf_id, "testing", allowed_from={"pending"})


@bp.route("/verification-queue/<vrf_id>/escalate", methods=["POST"])
@require_api_key
def admin_escalate_verification(vrf_id):
    return _set_verification_status(vrf_id, "human_review", allowed_from={"pending", "testing"})


def _set_payout_status(pay_id, status, *, allowed_from):
    from app.models import Payout
    p = db.session.get(Payout, pay_id)
    if not p:
        return api_error("payout not found", 404, code="PAYOUT_NOT_FOUND")
    if p.status not in allowed_from:
        return api_error(f"payout is '{p.status}', cannot move to '{status}'", code="INVALID_STATE")
    p.status = status
    db.session.commit()
    log.info("Payout %s -> %s", pay_id, status)
    return jsonify({"id": pay_id, "status": status})


@bp.route("/payouts/<pay_id>/release", methods=["POST"])
@require_api_key
def admin_release_payout(pay_id):
    return _set_payout_status(pay_id, "released", allowed_from={"pending", "held"})


@bp.route("/payouts/<pay_id>/hold", methods=["POST"])
@require_api_key
def admin_hold_payout(pay_id):
    return _set_payout_status(pay_id, "held", allowed_from={"pending"})


@bp.route("/payouts/<pay_id>/refund", methods=["POST"])
@require_api_key
def admin_refund_payout(pay_id):
    return _set_payout_status(pay_id, "refunded", allowed_from={"pending", "held"})


@bp.route("/payouts/release-all", methods=["POST"])
@require_api_key
def admin_release_all_payouts():
    from app.models import Payout
    pending = Payout.query.filter_by(status="pending").all()
    released_ids = []
    for p in pending:
        p.status = "released"
        released_ids.append(p.id)
    db.session.commit()
    log.info("Bulk release: %d payouts", len(released_ids))
    return jsonify({"released": released_ids, "count": len(released_ids)})


def _report_or_404(rpt_id):
    from app.models import ModerationReport
    r = db.session.get(ModerationReport, rpt_id)
    if not r:
        return None, api_error("report not found", 404, code="REPORT_NOT_FOUND")
    return r, None


@bp.route("/moderation/<rpt_id>/resolve", methods=["POST"])
@require_api_key
def admin_resolve_report(rpt_id):
    r, err = _report_or_404(rpt_id)
    if err:
        return err
    data = request.get_json(silent=True) or {}
    r.status = "resolved"
    notes = str(data.get("notes", "")).strip()
    if notes:
        r.notes = notes
    db.session.commit()
    log.info("Moderation report resolved: %s", rpt_id)
    return jsonify({"id": rpt_id, "status": "resolved"})


@bp.route("/moderation/<rpt_id>/investigate", methods=["POST"])
@require_api_key
def admin_investigate_report(rpt_id):
    r, err = _report_or_404(rpt_id)
    if err:
        return err
    r.status = "investigating"
    db.session.commit()
    log.info("Moderation report under investigation: %s", rpt_id)
    return jsonify({"id": rpt_id, "status": "investigating"})


@bp.route("/moderation/<rpt_id>/suspend", methods=["POST"])
@require_api_key
def admin_suspend_agent(rpt_id):
    from app.models import Agent as AgentModel
    r, err = _report_or_404(rpt_id)
    if err:
        return err
    r.status = "suspended"
    if r.agent_id:
        ag = db.session.get(AgentModel, r.agent_id)
        if ag:
            ag.verified = False
            ag.verification_tier = "suspended"
    db.session.commit()
    log.info("Agent suspended via moderation report: %s", rpt_id)
    return jsonify({"id": rpt_id, "status": "suspended", "agent": r.agent})
