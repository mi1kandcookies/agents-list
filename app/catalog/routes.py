"""Buyer-facing pages: landing, marketplace, agent detail, checkout, orders."""
from __future__ import annotations

import time

from flask import Blueprint, redirect, render_template, request, url_for

from app.extensions import db
from app.services import (
    CATEGORIES, USE_CASES, api_error, get_agent, listed_agents_query, orders_for_buyer, search_filter,
)

bp = Blueprint("catalog", __name__)


# Category chips shown on the home and marketplace pages. Each maps a buyer
# facing label onto an existing catalog filter.
CATEGORY_CHIPS = [
    ("Research",    {"category": "Research"}),
    ("Growth",      {"q": "growth"}),
    ("Engineering", {"category": "Development"}),
    ("Data",        {"category": "Data & Analytics"}),
    ("Ops",         {"category": "Automation"}),
    ("Content",     {"category": "Content"}),
]

# Order statuses that count as delivered, and as funds currently held.
_DONE_STATUSES = ("completed", "settled")
_HELD_STATUSES = ("in_escrow", "in_progress")


def marketplace_stats() -> dict:
    """Headline figures, read straight from the database (zero when empty)."""
    from app.models import Order as OrderModel
    held = (db.session.query(db.func.coalesce(db.func.sum(OrderModel.amount), 0))
            .filter(OrderModel.status.in_(_HELD_STATUSES)).scalar())
    return {
        "agents_listed": listed_agents_query().count(),
        "jobs_completed": OrderModel.query.filter(OrderModel.status.in_(_DONE_STATUSES)).count(),
        "usdc_in_escrow": float(held or 0),
    }


@bp.route("/")
def index():
    """Search-first home: search box, headline figures, categories, agents."""
    from app.models import Agent as AgentModel
    from app.seller.stamp import stamp_status
    rows = (listed_agents_query()
            .order_by(AgentModel.featured.desc(), AgentModel.verified.desc(),
                      AgentModel.rating.desc(), AgentModel.id.asc())
            .limit(40).all())
    # Only agents that can be hired right now (valid operator stamp) are shown.
    agents = [a.to_dict() for a in rows if stamp_status(a).ok][:12]
    return render_template("landing.html", agents=agents, stats=marketplace_stats(),
                           chips=CATEGORY_CHIPS)


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

    # Price sorts use the token prices shown on the cards: output first (it
    # dominates most jobs' cost), then input.
    relevance = (AgentModel.featured.desc(), AgentModel.verified.desc(),
                 AgentModel.rating.desc(), AgentModel.output_price_per_1m.asc())
    order_by = {
        "price_low":  (AgentModel.output_price_per_1m.asc(),
                       AgentModel.input_price_per_1m.asc()) + relevance,
        "price_high": (AgentModel.output_price_per_1m.desc(),
                       AgentModel.input_price_per_1m.desc()) + relevance,
        "rating":     (AgentModel.rating.desc(),) + relevance,
        "newest":     (AgentModel.id.desc(),),
    }.get(sort, relevance)
    from app.seller.stamp import stamp_status
    # Customers only see hireable agents; unstamped or re-stamp-pending
    # listings stay visible to their operators on the operator pages.
    agents = [a.to_dict() for a in q.order_by(*order_by).all() if stamp_status(a).ok]

    return render_template("marketplace.html", agents=agents, categories=CATEGORIES,
                           chips=CATEGORY_CHIPS,
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
    agent = get_agent(agent_id)
    if not agent:
        return redirect(url_for("catalog.marketplace"))
    if request.method == "POST":
        return api_error(
            "legacy checkout is disabled; create a protected engagement instead",
            410,
            code="LEGACY_CHECKOUT_DISABLED",
        )
    return redirect(url_for("engagements.jobs_new", agent=agent_id))


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
