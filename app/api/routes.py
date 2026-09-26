"""JSON API: agents, search, ratings, orders, disputes, transactions, health."""
from __future__ import annotations

import logging
import time
import uuid

from flask import Blueprint, jsonify, request

from app.extensions import db
from app.services import (
    PLATFORM_FEE_BPS, api_error, get_agent, get_onchain, get_transactions, is_valid_wallet,
    listed_agents_query, orders_for_buyer, search_filter,
)

log = logging.getLogger(__name__)
bp = Blueprint("api", __name__, url_prefix="/api")


@bp.route("/buyer/<wallet>/jobs")
def api_buyer_jobs(wallet):
    active = orders_for_buyer(wallet, completed=False)
    past = orders_for_buyer(wallet, completed=True)
    return jsonify({
        "wallet": wallet, "active": active, "past": past,
        "totals": {
            "activeCount": len(active),
            "completedCount": len([j for j in past if j["status"] == "completed"]),
            "cancelledCount": len([j for j in past if j["status"] == "cancelled"]),
            "totalSpentUSDC": round(sum(j["amount"] for j in past + active), 4),
            "totalFeesPaidUSDC": round(sum(j["platformFeeUSDC"] for j in past + active), 4),
        },
        "platformFeeBps": PLATFORM_FEE_BPS,
        "source": "db:orders",
    })


@bp.route("/agents")
def api_agents():
    """Paginated, filterable agent list. ``hireable=1`` keeps only agents
    with a valid operator stamp (what the job flow and MCP offer for hire)."""
    from app.models import Agent as AgentModel
    category = request.args.get("category", "")
    use_case = request.args.get("use_case", "")
    verified = request.args.get("verified", "")
    try:
        page = max(1, int(request.args.get("page", 1)))
        per_page = max(1, min(50, int(request.args.get("per_page", 12))))
    except ValueError:
        return api_error("page and per_page must be integers", field="page")

    q = search_filter(listed_agents_query(), request.args.get("q", ""))
    if category:
        q = q.filter(AgentModel.category == category)
    if use_case:
        q = q.filter(AgentModel.use_case == use_case)
    if verified == "true":
        q = q.filter(AgentModel.verified.is_(True))
    elif verified == "false":
        q = q.filter(AgentModel.verified.is_(False))
    q = q.order_by(AgentModel.id)
    if request.args.get("hireable", "").lower() in ("1", "true", "yes"):
        # Only agents that can be hired right now (valid operator stamp),
        # as on the home and marketplace pages. The stamp check is not a
        # column, so filter in Python and paginate the result.
        from app.seller.stamp import stamp_status
        hireable = [a for a in q.all() if stamp_status(a).ok]
        total = len(hireable)
        rows = hireable[(page - 1) * per_page:page * per_page]
    else:
        total = q.count()
        rows = q.offset((page - 1) * per_page).limit(per_page).all()
    return jsonify({"agents": [{**a.to_dict(), "agent_id": a.public_id} for a in rows],
                    "total": total,
                    "page": page, "per_page": per_page})


@bp.route("/agents/<int:agent_id>")
def api_agent(agent_id):
    agent = get_agent(agent_id)
    if not agent:
        return api_error("agent not found", 404, code="AGENT_NOT_FOUND")
    return jsonify(agent)


@bp.route("/agents/<int:agent_id>/rate", methods=["POST"])
def api_rate_agent(agent_id):
    from app.models import Agent as AgentModel, Review as ReviewModel
    payload = request.get_json(silent=True) or {}
    try:
        rating = int(payload.get("rating"))
    except (TypeError, ValueError):
        rating = 0
    if not 1 <= rating <= 5:
        return api_error("rating must be between 1 and 5", field="rating")
    agent = db.session.get(AgentModel, agent_id)
    if not agent:
        return api_error("agent not found", 404, code="AGENT_NOT_FOUND")
    agent.rating = round((agent.rating * agent.reviews + rating) / (agent.reviews + 1), 1)
    agent.reviews += 1
    db.session.add(ReviewModel(
        agent_id=agent_id, user=str(payload.get("user") or "anonymous")[:80], rating=rating,
        comment=str(payload.get("feedback") or "").strip()[:4000],
        date=time.strftime("%Y-%m-%d"),
    ))
    db.session.commit()
    log.info("Agent %s rated %s (new avg %.1f, %d reviews)", agent_id, rating, agent.rating, agent.reviews)
    return jsonify({"agentId": agent_id, "rating": agent.rating, "reviews": agent.reviews})


@bp.route("/search")
def api_search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"results": []})
    rows = search_filter(listed_agents_query(), q).limit(50).all()
    results = [
        {"id": a.id, "name": a.name, "category": a.category,
         "description": a.description, "rating": a.rating,
         "verified": a.verified, "billing": a.billing,
         "input_price_per_1m": a.input_price_per_1m,
         "output_price_per_1m": a.output_price_per_1m}
        for a in rows
    ]
    return jsonify({"results": results, "total": len(results), "query": q.lower()})


@bp.route("/orders/<order_id>/complete", methods=["POST"])
def api_order_complete(order_id):
    """Closed. Orders have no escrow behind them, so marking one complete
    stood for a payout that no human approved. Work is accepted and paid by
    releasing an engagement milestone, which needs its own approval."""
    return api_error("completing an order directly is no longer supported; release the "
                     "engagement milestone instead: POST /api/engagements/<id>/milestones/<idx>/release",
                     410, code="LEGACY_ORDER_COMPLETION_DISABLED")


@bp.route("/orders/<order_id>/start", methods=["POST"])
def api_order_start(order_id):
    """Seller marks a paid order as 'in_progress' to begin execution."""
    from app.models import Order as OrderModel
    row = db.session.get(OrderModel, order_id)
    if not row:
        return api_error("order not found", 404, code="ORDER_NOT_FOUND")
    if row.status != "in_escrow":
        return api_error(f"order cannot start from state '{row.status}'", code="INVALID_STATE")
    row.status = "in_progress"
    db.session.commit()
    log.info("Order %s started by seller", order_id)
    return jsonify({"orderId": order_id, "status": "in_progress"})


@bp.route("/dispute/submit", methods=["POST"])
def api_dispute_submit():
    from app.models import ModerationReport
    payload = request.get_json(silent=True) or {}
    for k in ("agentId", "severity", "reason", "affectedUser"):
        if k not in payload or payload.get(k) in (None, ""):
            return api_error(f"missing {k}", field=k)
    if not is_valid_wallet(payload.get("affectedUser")):
        return api_error("affectedUser must be a 0x-prefixed 40-hex address", field="affectedUser")
    try:
        agent_id = int(payload["agentId"])
        severity = int(payload["severity"])
    except (TypeError, ValueError):
        return api_error("agentId and severity must be integers", field="agentId")
    if severity not in (1, 2):
        return api_error("severity must be 1 or 2", field="severity")

    from app.models import Agent as AgentModel
    agent = db.session.get(AgentModel, agent_id)
    report = ModerationReport(
        id=f"RPT-{uuid.uuid4().hex[:8].upper()}",
        agent=agent.name if agent else f"Agent #{agent_id}",
        agent_id=agent_id,
        reporter=payload["affectedUser"],
        reason=str(payload["reason"])[:4000],
        status="open",
        date=time.strftime("%Y-%m-%d"),
        notes=f"order {payload.get('orderId')}" if payload.get("orderId") else "",
    )
    db.session.add(report)
    db.session.commit()

    result = {"status": "pending_review", "reportId": report.id}
    oc = get_onchain()
    if oc and oc.gatekeeper and oc.has_contract("ReputationContract"):
        try:
            result.update(oc.submit_incident(agent_id, payload["affectedUser"], severity))
        except Exception as e:
            log.warning("on-chain incident failed (report %s kept): %s", report.id, e)
            result["onchainError"] = str(e)[:200]
    return jsonify(result), 201


@bp.route("/agents/<int:agent_id>/transactions")
def api_agent_transactions(agent_id):
    try:
        limit = min(200, max(1, int(request.args.get("limit", 25))))
    except ValueError:
        limit = 25
    kinds_raw = request.args.get("kinds")
    kinds = [k for k in (kinds_raw.split(",") if kinds_raw else []) if k]
    real_only = request.args.get("real_only", "").lower() in ("1", "true", "yes")
    return jsonify({
        "agentId": agent_id,
        "realOnly": real_only,
        "transactions": get_transactions(agent_id=agent_id, kinds=kinds or None,
                                         limit=limit, real_only=real_only),
    })


@bp.route("/transactions")
def api_transactions():
    """Global on-chain activity feed across all agents."""
    try:
        limit = min(200, max(1, int(request.args.get("limit", 50))))
    except ValueError:
        limit = 50
    kinds_raw = request.args.get("kinds")
    kinds = [k for k in (kinds_raw.split(",") if kinds_raw else []) if k]
    real_only = request.args.get("real_only", "").lower() in ("1", "true", "yes")
    return jsonify({
        "realOnly": real_only,
        "transactions": get_transactions(kinds=kinds or None, limit=limit, real_only=real_only),
    })


@bp.route("/llm/status")
def api_llm_status():
    """Probe the Akash-hosted vLLM endpoint — does it respond, is model loaded."""
    from app.llm import health
    return jsonify(health())


@bp.route("/agents/<int:agent_id>/generate", methods=["POST"])
def api_agent_generate(agent_id):
    """Closed legacy preview; paid work must use the protected task route."""
    return api_error(
        "unprotected generation is disabled; use the guided engagement or the "
        "x402 task endpoint with a mandate",
        410,
        code="LEGACY_GENERATION_DISABLED",
    )


@bp.route("/health")
def api_health():
    """Liveness probe - always returns 200 if the process is alive."""
    return jsonify({"status": "ok", "service": "agents-list", "ts": int(time.time())})


@bp.route("/ready")
def api_ready():
    """Readiness probe - checks DB connectivity."""
    try:
        db.session.execute(db.text("SELECT 1"))
        return jsonify({"status": "ready", "db": "ok", "ts": int(time.time())})
    except Exception as e:
        log.error("Readiness check failed: %s", e)
        return jsonify({"status": "unavailable", "db": str(e)}), 503
