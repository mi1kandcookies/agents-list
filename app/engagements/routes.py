"""JSON endpoints for scoped engagements and milestone status."""
from __future__ import annotations

from flask import jsonify, request

from app.approvals.executors import ApprovalConsumptionError, consume_approval
from app.engagements import bp
from app.engagements.service import (
    EngagementError, create_engagement, fund_milestone, get_engagement,
    release_milestone, serialize_engagement, submit_milestone,
)
from app.screening.policy import ScreeningBlocked, enforce_verdict, normalize_verdict


def _error(message, code="INVALID_REQUEST", status=400):
    return jsonify({"error": message, "code": code}), status


@bp.route("", methods=["POST"])
def create():
    body = request.get_json(silent=True) or {}
    try:
        row = create_engagement(
            human_id=str(body.get("humanId") or ""), agent_id=body.get("agentId"),
            title=body.get("title"), brief=body.get("brief"),
            budget_atomic=int(body.get("budgetAtomic", 0)),
            milestones=body.get("milestones") or [], deadline=body.get("deadline"),
            parent_engagement_id=body.get("parentEngagementId"),
        )
    except (EngagementError, TypeError, ValueError) as exc:
        return _error(str(exc), "ENGAGEMENT_REJECTED")
    return jsonify(serialize_engagement(row)), 201


@bp.route("/<engagement_id>")
def status(engagement_id):
    row = get_engagement(engagement_id)
    if not row:
        return _error("engagement not found", "ENGAGEMENT_NOT_FOUND", 404)
    return jsonify(serialize_engagement(row))


@bp.route("/<engagement_id>/milestones/<milestone_id>/fund", methods=["POST"])
def fund(engagement_id, milestone_id):
    row = get_engagement(engagement_id)
    milestone = next((item for item in row.milestones if item.id == milestone_id), None) if row else None
    if not row or not milestone:
        return _error("engagement or milestone not found", "NOT_FOUND", 404)
    body = request.get_json(silent=True) or {}
    try:
        verdict = normalize_verdict(body.get("screening") or {}, address=str(body.get("payee") or ""), provider="api")
        enforce_verdict(verdict, milestone.amount_atomic)
        approval = consume_approval(str(body.get("approvalId") or ""), str(body.get("actionHash") or ""), human_id=row.human_id)
        receipt = fund_milestone(row, milestone, approval_id=approval.id, screening_id=verdict.verdict_id)
    except (ScreeningBlocked, ApprovalConsumptionError, EngagementError) as exc:
        return _error(str(exc), "FUND_BLOCKED", 409)
    return jsonify({"engagement": serialize_engagement(row), "receipt": receipt.to_dict(),
                    "screening": verdict.to_dict()}), 200


@bp.route("/<engagement_id>/milestones/<milestone_id>/submit", methods=["POST"])
def submit(engagement_id, milestone_id):
    row = get_engagement(engagement_id)
    milestone = next((item for item in row.milestones if item.id == milestone_id), None) if row else None
    if not row or not milestone:
        return _error("engagement or milestone not found", "NOT_FOUND", 404)
    body = request.get_json(silent=True) or {}
    try:
        submit_milestone(milestone, evidence_hash=str(body.get("evidenceHash") or ""),
                         deliverable=str(body.get("deliverable") or ""))
    except EngagementError as exc:
        return _error(str(exc), "MILESTONE_REJECTED", 409)
    return jsonify(serialize_engagement(row))


@bp.route("/<engagement_id>/milestones/<milestone_id>/release", methods=["POST"])
def release(engagement_id, milestone_id):
    row = get_engagement(engagement_id)
    milestone = next((item for item in row.milestones if item.id == milestone_id), None) if row else None
    if not row or not milestone:
        return _error("engagement or milestone not found", "NOT_FOUND", 404)
    body = request.get_json(silent=True) or {}
    try:
        verdict = normalize_verdict(body.get("screening") or {}, address=str(body.get("payee") or ""), provider="api")
        enforce_verdict(verdict, milestone.amount_atomic)
        approval = consume_approval(str(body.get("approvalId") or ""), str(body.get("actionHash") or ""), human_id=row.human_id)
        receipt = release_milestone(row, milestone, approval_id=approval.id,
                                    screening_id=verdict.verdict_id, payee=str(body.get("payee") or ""))
    except (ScreeningBlocked, ApprovalConsumptionError, EngagementError) as exc:
        return _error(str(exc), "RELEASE_BLOCKED", 409)
    return jsonify({"engagement": serialize_engagement(row), "receipt": receipt.to_dict(),
                    "screening": verdict.to_dict()}), 200
