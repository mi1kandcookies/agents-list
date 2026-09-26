"""Seller pages: listing wizard, dashboard, orders, verification, listing management."""
from __future__ import annotations

import logging
import time

from flask import Blueprint, jsonify, redirect, render_template, request, url_for

from app.extensions import db
from app.common.agent_ids import generate_agent_id
from chain.config import explorer_url
from app.services import (
    CATEGORIES, PLATFORM_FEE_BPS, USE_CASES, agents_for_seller, api_error, is_valid_wallet,
    seller_earnings_from_chain,
)

log = logging.getLogger(__name__)
bp = Blueprint("seller", __name__, url_prefix="/seller")


@bp.route("/dashboard")
def seller_dashboard():
    # Legacy path: keep route alive, but canonical seller dashboard is /seller/earnings.
    return redirect(url_for("seller.seller_earnings"))


@bp.route("/create", methods=["GET", "POST"])
def seller_create():
    if request.method == "POST":
        from app.models import Agent as AgentModel, VerificationEntry
        data = request.get_json(silent=True) or request.form.to_dict()

        wallet = str(data.get("wallet") or "").strip()
        if not is_valid_wallet(wallet):
            return jsonify({"error": "connect a wallet first (seller wallet required)",
                            "field": "wallet"}), 400

        required = ["name", "description", "category", "billing"]
        missing = [k for k in required if not data.get(k)]
        if missing:
            return jsonify({"error": f"missing required fields: {missing}"}), 400
        if data["billing"] not in ("per_token", "per_minute"):
            return jsonify({"error": "billing must be per_token or per_minute",
                            "field": "billing"}), 400

        def _num(key, default=0.0):
            try:
                return float(data.get(key) or default)
            except (TypeError, ValueError):
                return default

        # Wizard sends USD per 1M tokens for input/output.
        in_per_1m = _num("min_input_tokens")
        out_per_1m = _num("min_output_tokens")
        min_price = _num("min_price", 0.001)
        max_price = max(_num("max_price", 0.010), min_price)

        row = AgentModel(
            public_id=generate_agent_id(),
            name=data["name"],
            description=data.get("description", ""),
            long_description=data.get("long_description") or data.get("description", ""),
            category=data["category"],
            use_case=data.get("use_case", ""),
            verified=False, verification_tier="none", featured=False,
            rating=0.0, reviews=0,
            billing=data["billing"],
            min_price=min_price, max_price=max_price, current_price=min_price,
            seller=data.get("seller") or wallet,
            seller_rating=0.0, tasks_completed=0,
            avg_completion_time=data.get("avg_completion_time", " - "),
            deployer_wallet=wallet.lower(),
            input_price_per_1m=int(in_per_1m * 1_000_000),
            output_price_per_1m=int(out_per_1m * 1_000_000),
        )
        model = str(data.get("model") or "")
        if "|" in model:
            row.model_provider, row.model_name = [m.strip()[:80] for m in model.split("|", 1)]
        row.tags = [t.strip() for t in (data.get("tags") or "").split(",") if t.strip()]
        row.capabilities = [c.strip() for c in (data.get("capabilities") or "").split("\n") if c.strip()]
        db.session.add(row)
        db.session.flush()   # populate row.id

        db.session.add(VerificationEntry(
            id=f"VRF-{row.id:03d}",
            agent_id=row.id, agent_name=row.name, seller=row.seller,
            tier=data.get("verification_tier", "basic"),
            status="pending", submitted=time.strftime("%Y-%m-%d"),
        ))
        db.session.commit()
        log.info("New agent %s (id=%d) listed by %s", row.name, row.id, wallet)

        if request.is_json:
            return jsonify({
                "agentId": row.id, "status": "listed", "wallet": wallet,
                "message": "Agent listed and queued for verification.",
            }), 201
        return redirect(url_for("catalog.agent_detail", agent_id=row.id))
    return render_template("seller/create.html", categories=CATEGORIES, use_cases=USE_CASES)


@bp.route("/verification")
def seller_verification():
    from app.models import VerificationEntry
    wallet = (request.args.get("wallet") or request.cookies.get("seller_wallet") or "").lower()
    q = VerificationEntry.query
    if wallet:
        q = q.filter(db.func.lower(VerificationEntry.seller) == wallet)
    queue = [v.to_dict() for v in q.order_by(VerificationEntry.created_at.desc()).limit(20).all()] if wallet else []
    return render_template("seller/verification.html", queue=queue)


@bp.route("/orders")
def seller_orders():
    from app.models import Order as OrderModel
    seller = (request.args.get("seller") or request.cookies.get("seller_wallet") or "").strip()
    orders = []
    if seller:
        ids = [a.id for a in agents_for_seller(seller)]
        if ids:
            orders = [o.to_dict() for o in OrderModel.query
                      .filter(OrderModel.agent_id.in_(ids))
                      .order_by(OrderModel.created_at.desc()).limit(200).all()]
    return render_template("seller/orders.html", orders=orders, seller=seller)


@bp.route("/earnings")
def seller_earnings():
    from app.models import Order as OrderModel
    wallet = (request.args.get("wallet") or
              request.cookies.get("seller_wallet") or
              request.cookies.get("buyer_wallet") or "")
    my_agents = [a.to_dict() for a in agents_for_seller(wallet)] if wallet else []
    earnings = seller_earnings_from_chain([a["id"] for a in my_agents])
    orders = ([o.to_dict() for o in OrderModel.query
               .filter(OrderModel.agent_id.in_([a["id"] for a in my_agents])).all()]
              if my_agents else [])

    # Real transaction history for this seller's agents, from ChainTransaction.
    # Replaces the 5 hardcoded rows; grows with every on-chain event.
    from app.models import ChainTransaction as CT
    import json as _json
    my_agent_ids = [a["id"] for a in my_agents]
    tx_history = []
    if my_agent_ids:
        # Real on-chain txs only — the sim rows flooded this table with
        # fake 'Task Payment' entries for agents that never earned anything.
        ct_rows = (CT.query.filter(CT.agent_id.in_(my_agent_ids))
                   .filter(CT.kind.in_(["deposit", "settle", "slash", "stake", "a2a_hire", "a2a_settle"]))
                   .filter(CT.meta.like('%"real": true%'))
                   .order_by(CT.id.desc()).limit(25).all())
        id_to_name = {a["id"]: a["name"] for a in my_agents}
        for r in ct_rows:
            try: m = _json.loads(r.meta or "{}")
            except Exception: m = {}
            amount = (r.amount_usdc or 0) / 1_000_000
            fee = round(amount * PLATFORM_FEE_BPS / 10_000, 4) if r.kind == "deposit" else 0.0
            kind_label = {"deposit": "Deposit", "settle": "Task Payment",
                          "slash": "Slash", "stake": "Stake"}.get(r.kind, r.kind)
            tx_history.append({
                "date":   time.strftime("%b %d, %Y", time.gmtime(r.ts)) if r.ts else "—",
                "agent":  id_to_name.get(r.agent_id, f"Agent #{r.agent_id}"),
                "type":   kind_label,
                "amount": round(amount, 2),
                "fee":    fee,
                "net":    round(amount - fee, 3),
                "status": "released" if r.kind == "settle" else ("escrow" if r.kind == "deposit" else r.kind),
                "real":   bool(m.get("real")),
                "txHash": r.tx_hash,
                "explorer": explorer_url("tx", r.tx_hash),
            })

    # Weekly settle counts per day (last 7 days) for the usage chart — live from CT
    import datetime as _dt
    weekly_labels, weekly_tasks = [], []
    for i in range(6, -1, -1):
        day_start_dt = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=i)).replace(hour=0, minute=0, second=0, microsecond=0)
        day_end_dt   = day_start_dt + _dt.timedelta(days=1)
        day_start = int(day_start_dt.replace(tzinfo=_dt.timezone.utc).timestamp())
        day_end   = int(day_end_dt.replace(tzinfo=_dt.timezone.utc).timestamp())
        if my_agent_ids:
            n = db.session.query(db.func.count(CT.id)).filter(
                CT.agent_id.in_(my_agent_ids),
                CT.kind.in_(["settle", "a2a_settle"]),
                CT.meta.like('%"real": true%'),
                CT.ts >= day_start, CT.ts < day_end
            ).scalar() or 0
        else:
            n = 0
        weekly_labels.append(day_start_dt.strftime("%a"))
        weekly_tasks.append(int(n))

    return render_template("seller/earnings.html", earnings=earnings,
                           agents=my_agents, orders=orders,
                           tx_history=tx_history, seller=wallet,
                           weekly_labels=weekly_labels, weekly_tasks=weekly_tasks)


@bp.route("/agents/<int:agent_id>", methods=["GET", "POST"])
def seller_manage_agent(agent_id):
    from app.models import Agent as AgentModel
    row = db.session.get(AgentModel, agent_id)
    if not row:
        return redirect(url_for("seller.seller_earnings"))
    if request.method == "POST":
        data = request.get_json(silent=True) or request.form.to_dict()
        action = data.get("action", "")
        if action == "pause":
            if not row.verification_tier.endswith("_paused"):
                row.verification_tier = f"{row.verification_tier}_paused"
            db.session.commit()
            log.info("Agent %s paused by seller", agent_id)
            return jsonify({"agentId": agent_id, "status": "paused"})
        if action == "reactivate":
            row.verification_tier = row.verification_tier.removesuffix("_paused")
            db.session.commit()
            log.info("Agent %s reactivated by seller", agent_id)
            return jsonify({"agentId": agent_id, "status": "active"})
        if action == "update":
            try:
                if "name" in data and str(data["name"]).strip():
                    row.name = str(data["name"]).strip()[:120]
                if "description" in data:
                    row.description = str(data["description"])
                if "category" in data and data["category"] in CATEGORIES:
                    row.category = data["category"]
                if "min_price" in data:
                    row.min_price = float(data["min_price"])
                if "max_price" in data:
                    row.max_price = float(data["max_price"])
                if "tags" in data:
                    row.tags = [t.strip() for t in str(data["tags"]).split(",") if t.strip()]
            except (TypeError, ValueError):
                return api_error("prices must be numeric", field="min_price")
            row.current_price = min(max(row.current_price, row.min_price), max(row.max_price, row.min_price))
            db.session.commit()
            log.info("Agent %s updated by seller", agent_id)
            return jsonify({"agentId": agent_id, "status": "updated"})
        return api_error("unknown action", field="action")
    return render_template("seller/manage.html", agent=row.to_dict(), categories=CATEGORIES)
