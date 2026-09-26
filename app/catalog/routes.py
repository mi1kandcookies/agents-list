"""Buyer-facing pages: landing, marketplace, agent detail, checkout, orders."""
from __future__ import annotations

import re
import time

from flask import Blueprint, jsonify, redirect, render_template, request, url_for

from app.extensions import db
from app.services import (
    CATEGORIES, USE_CASES, api_error, get_agent, is_valid_wallet, listed_agents_query,
    live_chain_stats, new_order_id, orders_for_buyer, search_filter,
)

bp = Blueprint("catalog", __name__)


@bp.route("/")
def index():
    """Landing page. Click-through to /marketplace."""
    from app.models import Agent as AgentModel
    featured = [a.to_dict() for a in listed_agents_query()
                .filter(AgentModel.featured.is_(True))
                .order_by(AgentModel.rating.desc()).limit(6).all()]
    return render_template("landing.html", featured=featured, stats=live_chain_stats())


@bp.route("/marketplace")
def marketplace():
    from app.models import Agent as AgentModel
    category  = request.args.get("category", "")
    use_case  = request.args.get("use_case", "")
    verified  = request.args.get("verified", "")
    featured  = request.args.get("featured", "")
    sort      = (request.args.get("sort", "relevance") or "relevance").strip().lower()
    min_price = request.args.get("min_price", "")
    max_price = request.args.get("max_price", "")
    query     = request.args.get("q", "")

    q = search_filter(listed_agents_query(), query)
    if category:
        q = q.filter(AgentModel.category == category)
    if use_case:
        q = q.filter(AgentModel.use_case == use_case)
    if verified == "verified":
        q = q.filter(AgentModel.verified.is_(True))
    elif verified == "unverified":
        q = q.filter(AgentModel.verified.is_(False))
    if featured:
        q = q.filter(AgentModel.featured.is_(True))
    try:
        if min_price not in ("", None):
            q = q.filter(AgentModel.current_price >= float(min_price))
        if max_price not in ("", None):
            q = q.filter(AgentModel.current_price <= float(max_price))
    except ValueError:
        pass

    relevance = (AgentModel.featured.desc(), AgentModel.verified.desc(),
                 AgentModel.rating.desc(), AgentModel.current_price.asc())
    order_by = {
        "price_low":  (AgentModel.current_price.asc(),) + relevance,
        "price_high": (AgentModel.current_price.desc(),) + relevance,
        "rating":     (AgentModel.rating.desc(),) + relevance,
        "newest":     (AgentModel.id.desc(),),
    }.get(sort, relevance)
    agents = [a.to_dict() for a in q.order_by(*order_by).all()]

    return render_template("marketplace.html", agents=agents, categories=CATEGORIES,
                           use_cases=USE_CASES, filters={"category": category, "use_case": use_case,
                           "verified": verified, "sort": sort, "q": query, "featured": featured})


@bp.route("/agent/<int:agent_id>")
def agent_detail(agent_id):
    from app.models import Agent as AgentModel, Review as ReviewModel
    agent = get_agent(agent_id)
    if not agent:
        return redirect(url_for("catalog.marketplace"))
    rows = (ReviewModel.query.filter_by(agent_id=agent_id)
            .order_by(ReviewModel.created_at.desc()).limit(10).all())
    reviews = [r.to_dict() for r in rows]
    # "Works well with": verified agents from complementary categories.
    affinity = {
        "Development":      ["Security", "Data & Analytics"],
        "Data & Analytics": ["Research", "Content"],
        "Content":          ["Research", "Data & Analytics"],
        "Finance":          ["Research", "Data & Analytics"],
        "Research":         ["Content", "Data & Analytics"],
        "Security":         ["Development", "Automation"],
        "Automation":       ["Development", "Content"],
    }
    pair_cats = affinity.get(agent["category"], [])
    collaborators = [a.to_dict() for a in listed_agents_query()
                     .filter(AgentModel.id != agent_id,
                             AgentModel.category.in_(pair_cats),
                             AgentModel.verified.is_(True))
                     .order_by(AgentModel.rating.desc()).limit(3).all()]
    return render_template("agent_detail.html", agent=agent, reviews=reviews,
                           collaborators=collaborators)


@bp.route("/checkout/<int:agent_id>", methods=["GET", "POST"])
def checkout(agent_id):
    from app.models import Order as OrderModel
    agent = get_agent(agent_id)
    if not agent:
        return redirect(url_for("catalog.marketplace"))
    if request.method == "POST":
        data = request.get_json(silent=True) or request.form.to_dict()
        try:
            amount = float(data.get("amount") or agent.get("current_price") or agent.get("min_price") or 0)
        except (TypeError, ValueError):
            return api_error("amount must be numeric", field="amount")
        if amount <= 0:
            return api_error("amount must be > 0", field="amount")
        buyer = str(data.get("buyer") or "").strip() or request.cookies.get("buyer_wallet") or ""
        if not is_valid_wallet(buyer):
            return api_error("connect a wallet first (buyer address required)", field="buyer")
        order = OrderModel(
            id=new_order_id(), agent_id=agent_id, buyer=buyer, amount=amount,
            status="pending_payment", task=str(data.get("task", ""))[:4000],
            date=time.strftime("%Y-%m-%d"),
        )
        db.session.add(order)
        db.session.commit()
        if request.is_json:
            return jsonify({"orderId": order.id, "status": order.status}), 201
        return redirect(url_for("catalog.order_detail", order_id=order.id))
    return render_template("checkout.html", agent=agent)


@bp.route("/order/<order_id>")
def order_detail(order_id):
    from app.models import Order as OrderModel, ChainTransaction as CT
    row = db.session.get(OrderModel, order_id)
    if not row:
        return render_template("404.html", missing=f"order {order_id}"), 404
    order = row.to_dict()
    ct = (CT.query.filter(CT.meta.like(f'%"orderId": "{order_id}"%'))
          .order_by(CT.id.desc()).first())
    order["tx_hash"] = ct.tx_hash if ct else ""
    agent = get_agent(row.agent_id)
    if not agent:
        return render_template("404.html", missing=f"agent for order {order_id}"), 404
    return render_template("order.html", order=order, agent=agent)


@bp.route("/new")
def new_job():
    """Start a job. The guided scoping flow will live here; for now it keeps
    the buyer's description and suggests listed agents that match its words."""
    agent = get_agent(request.args.get("agent")) if request.args.get("agent") else None
    q = request.args.get("q", "").strip()[:500]
    return render_template("new_job.html", q=q, agent=agent, matches=_keyword_matches(q))


def _keyword_matches(text: str, limit: int = 6) -> list[dict]:
    """Listed agents ranked by how many words of a free-text job description
    appear in their name, description, category or tags."""
    words = {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 3}
    if not words:
        return []
    scored = []
    for a in listed_agents_query().all():
        hay = " ".join([a.name, a.description, a.category, a.use_case, " ".join(a.tags)]).lower()
        hits = sum(1 for w in words if w in hay)
        if hits:
            scored.append((hits, a.rating, a.id, a))
    scored.sort(key=lambda t: (-t[0], -t[1], t[2]))
    return [a.to_dict() for *_, a in scored[:limit]]


@bp.route("/how-it-works")
def how_it_works():
    return render_template("how_it_works.html")


@bp.route("/active-jobs")
def active_jobs():
    wallet = request.args.get("wallet") or request.cookies.get("buyer_wallet") or ""
    orders = orders_for_buyer(wallet, completed=False)
    return render_template("active_jobs.html", orders=orders, wallet=wallet)


@bp.route("/past-jobs")
def past_jobs():
    wallet = request.args.get("wallet") or request.cookies.get("buyer_wallet") or ""
    orders = orders_for_buyer(wallet, completed=True)
    return render_template("past_jobs.html", orders=orders, wallet=wallet)


@bp.route("/list-your-agent")
def list_your_agent():
    return redirect(url_for("seller.seller_create"))
