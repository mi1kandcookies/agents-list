from __future__ import annotations
from flask import Flask, render_template, request, jsonify, redirect, url_for
import logging
import os
import time

# Load .env manually (no python-dotenv dependency). Must happen BEFORE onchain
# or any module that reads FACILITATOR_PRIVATE_KEY at import time.
from pathlib import Path as _Path
_env_file = _Path(__file__).parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k, _v)

from config import config as _config_map
from extensions import db, cors, limiter
from auth import require_api_key

# ── Logging setup ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("agents_list")

# ── App factory ────────────────────────────────────────────────────────────────
app = Flask(__name__)

_env = os.environ.get("FLASK_ENV", "development")
app.config.from_object(_config_map.get(_env, _config_map["default"]))

# Init extensions
db.init_app(app)
cors.init_app(app, resources={r"/api/*": {"origins": app.config.get("CORS_ORIGINS", "*")}})
limiter.init_app(app)

app.jinja_env.globals['enumerate'] = enumerate

# Request logging
@app.before_request
def _log_request():
    log.info("%s %s", request.method, request.path)

@app.after_request
def _log_response(response):
    log.info("%s %s → %s", request.method, request.path, response.status_code)
    return response

# ── Mock Data ──────────────────────────────────────────────────────────────────

CATEGORIES = ["Development", "Data & Analytics", "Content", "Finance", "Research", "Security", "Automation"]
USE_CASES  = ["Code Review", "Translation", "Summarization", "Web Scraping", "Image Generation", "Testing", "Resume & Career"]


# ── Shared helpers ─────────────────────────────────────────────────────────────
import re as _re

def _is_valid_wallet(addr: str) -> bool:
    """EVM address shape check — 0x + 40 hex. Keeps junk out of Orders."""
    return bool(_re.fullmatch(r"0x[a-fA-F0-9]{40}", str(addr or "")))


def _api_error(message: str, status: int = 400, *, code: str = "INVALID_REQUEST", field: str | None = None):
    """Uniform JSON error body: {error, code, field?}."""
    body = {"error": message, "code": code}
    if field:
        body["field"] = field
    return jsonify(body), status

# ── Query helpers ──────────────────────────────────────────────────────────────

_HIDDEN_TIERS = ("suspended",)


def _listed_agents_query():
    """Agents visible in the catalog: not suspended, not paused by the seller."""
    from models import Agent as AgentModel
    return AgentModel.query.filter(
        ~AgentModel.verification_tier.in_(_HIDDEN_TIERS),
        ~AgentModel.verification_tier.endswith("_paused", autoescape=True),
    )


def _search_filter(query, text: str):
    """Case-insensitive match on name, description or tags."""
    from models import Agent as AgentModel
    text = (text or "").strip().lower()
    if not text:
        return query
    like = f"%{text}%"
    return query.filter(db.or_(
        db.func.lower(AgentModel.name).like(like),
        db.func.lower(AgentModel.description).like(like),
        db.func.lower(AgentModel._tags).like(like),
    ))


def _get_agent(agent_id) -> dict | None:
    from models import Agent as AgentModel
    try:
        row = db.session.get(AgentModel, int(agent_id))
    except (TypeError, ValueError):
        return None
    return row.to_dict() if row else None


def _agents_for_seller(wallet: str) -> list:
    """Agents owned by a seller wallet (matched on seller or deployer_wallet)."""
    from models import Agent as AgentModel
    w = (wallet or "").strip().lower()
    if not w:
        return []
    return (AgentModel.query
            .filter(db.or_(db.func.lower(AgentModel.seller) == w,
                           db.func.lower(AgentModel.deployer_wallet) == w))
            .order_by(AgentModel.id).all())


# ── Routes ─────────────────────────────────────────────────────────────────────


@app.route("/")
def index():
    """Landing page. Click-through to /marketplace."""
    from models import Agent as AgentModel
    featured = [a.to_dict() for a in _listed_agents_query()
                .filter(AgentModel.featured.is_(True))
                .order_by(AgentModel.rating.desc()).limit(6).all()]
    return render_template("landing.html", featured=featured, stats=_live_chain_stats())



@app.route("/marketplace")
def marketplace():
    from models import Agent as AgentModel
    category  = request.args.get("category", "")
    use_case  = request.args.get("use_case", "")
    verified  = request.args.get("verified", "")
    featured  = request.args.get("featured", "")
    sort      = (request.args.get("sort", "relevance") or "relevance").strip().lower()
    min_price = request.args.get("min_price", "")
    max_price = request.args.get("max_price", "")
    query     = request.args.get("q", "")

    q = _search_filter(_listed_agents_query(), query)
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



@app.route("/agent/<int:agent_id>")
def agent_detail(agent_id):
    from models import Agent as AgentModel, Review as ReviewModel
    agent = _get_agent(agent_id)
    if not agent:
        return redirect(url_for("marketplace"))
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
    collaborators = [a.to_dict() for a in _listed_agents_query()
                     .filter(AgentModel.id != agent_id,
                             AgentModel.category.in_(pair_cats),
                             AgentModel.verified.is_(True))
                     .order_by(AgentModel.rating.desc()).limit(3).all()]
    return render_template("agent_detail.html", agent=agent, reviews=reviews,
                           collaborators=collaborators)



@app.route("/checkout/<int:agent_id>", methods=["GET", "POST"])
def checkout(agent_id):
    from models import Order as OrderModel
    agent = _get_agent(agent_id)
    if not agent:
        return redirect(url_for("marketplace"))
    if request.method == "POST":
        data = request.get_json(silent=True) or request.form.to_dict()
        try:
            amount = float(data.get("amount") or agent.get("current_price") or agent.get("min_price") or 0)
        except (TypeError, ValueError):
            return _api_error("amount must be numeric", field="amount")
        if amount <= 0:
            return _api_error("amount must be > 0", field="amount")
        buyer = str(data.get("buyer") or "").strip() or request.cookies.get("buyer_wallet") or ""
        if not _is_valid_wallet(buyer):
            return _api_error("connect a wallet first (buyer address required)", field="buyer")
        order = OrderModel(
            id=_new_order_id(), agent_id=agent_id, buyer=buyer, amount=amount,
            status="pending_payment", task=str(data.get("task", ""))[:4000],
            date=time.strftime("%Y-%m-%d"),
        )
        db.session.add(order)
        db.session.commit()
        if request.is_json:
            return jsonify({"orderId": order.id, "status": order.status}), 201
        return redirect(url_for("order_detail", order_id=order.id))
    return render_template("checkout.html", agent=agent)



@app.route("/order/<order_id>")
def order_detail(order_id):
    from models import Order as OrderModel, ChainTransaction as CT
    row = db.session.get(OrderModel, order_id)
    if not row:
        return render_template("404.html", missing=f"order {order_id}"), 404
    order = row.to_dict()
    ct = (CT.query.filter(CT.meta.like(f'%"orderId": "{order_id}"%'))
          .order_by(CT.id.desc()).first())
    order["tx_hash"] = ct.tx_hash if ct else ""
    agent = _get_agent(row.agent_id)
    if not agent:
        return render_template("404.html", missing=f"agent for order {order_id}"), 404
    return render_template("order.html", order=order, agent=agent)



@app.route("/how-it-works")
def how_it_works():
    return render_template("how_it_works.html")

def _orders_for_buyer(wallet: str, *, completed: bool) -> list:
    """Order rows for this buyer wallet, with fee breakdown and the payment
    tx (if any). Used by /past-jobs (completed) and /active-jobs (open)."""
    from models import Order as OrderModel, ChainTransaction as CT
    wallet = (wallet or "").strip().lower()
    if not wallet:
        return []
    wanted = ({"completed", "settled", "cancelled"} if completed
              else {"pending_payment", "in_escrow", "in_progress"})
    rows = (OrderModel.query
            .filter(db.func.lower(OrderModel.buyer) == wallet)
            .filter(OrderModel.status.in_(wanted))
            .order_by(OrderModel.created_at.desc())
            .limit(50).all())
    out = []
    for o in rows:
        d = o.to_dict()
        amt = float(o.amount or 0)
        d["escrowedUSDC"]    = round(amt * (1 - PLATFORM_FEE_BPS / 10_000), 4)
        d["platformFeeUSDC"] = round(amt * PLATFORM_FEE_BPS / 10_000, 4)
        ct = (CT.query.filter(CT.meta.like(f'%"orderId": "{o.id}"%'))
              .order_by(CT.id.desc()).first())
        d["txHash"]    = ct.tx_hash if ct else ""
        d["snowtrace"] = f"https://testnet.snowtrace.io/tx/{ct.tx_hash}" if ct else ""
        d["sourceChain"] = bool(ct)
        d["orderUrl"]  = f"/order/{o.id}"
        out.append(d)
    return out




@app.route("/active-jobs")
def active_jobs():
    wallet = request.args.get("wallet") or request.cookies.get("buyer_wallet") or ""
    orders = _orders_for_buyer(wallet, completed=False)
    return render_template("active_jobs.html", orders=orders, wallet=wallet)



@app.route("/past-jobs")
def past_jobs():
    wallet = request.args.get("wallet") or request.cookies.get("buyer_wallet") or ""
    orders = _orders_for_buyer(wallet, completed=True)
    return render_template("past_jobs.html", orders=orders, wallet=wallet)



@app.route("/api/buyer/<wallet>/jobs")
def api_buyer_jobs(wallet):
    active = _orders_for_buyer(wallet, completed=False)
    past = _orders_for_buyer(wallet, completed=True)
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



@app.route("/list-your-agent")
def list_your_agent():
    return redirect(url_for("seller_create"))

# ── Agent Mode ──────────────────────────────────────────────────────────────

# ── Seller ─────────────────────────────────────────────────────────────────────

@app.route("/seller/dashboard")
def seller_dashboard():
    # Legacy path: keep route alive, but canonical seller dashboard is /seller/earnings.
    return redirect(url_for("seller_earnings"))

@app.route("/seller/create", methods=["GET", "POST"])
def seller_create():
    if request.method == "POST":
        from models import Agent as AgentModel, VerificationEntry
        data = request.get_json(silent=True) or request.form.to_dict()

        wallet = str(data.get("wallet") or "").strip()
        if not _is_valid_wallet(wallet):
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
        return redirect(url_for("agent_detail", agent_id=row.id))
    return render_template("seller/create.html", categories=CATEGORIES, use_cases=USE_CASES)

@app.route("/seller/verification")
def seller_verification():
    from models import VerificationEntry
    wallet = (request.args.get("wallet") or request.cookies.get("seller_wallet") or "").lower()
    q = VerificationEntry.query
    if wallet:
        q = q.filter(db.func.lower(VerificationEntry.seller) == wallet)
    queue = [v.to_dict() for v in q.order_by(VerificationEntry.created_at.desc()).limit(20).all()] if wallet else []
    return render_template("seller/verification.html", queue=queue)



@app.route("/seller/orders")
def seller_orders():
    from models import Agent as AgentModel, Order as OrderModel
    seller = (request.args.get("seller") or request.cookies.get("seller_wallet") or "").strip()
    orders = []
    if seller:
        ids = [a.id for a in _agents_for_seller(seller)]
        if ids:
            orders = [o.to_dict() for o in OrderModel.query
                      .filter(OrderModel.agent_id.in_(ids))
                      .order_by(OrderModel.created_at.desc()).limit(200).all()]
    return render_template("seller/orders.html", orders=orders, seller=seller)



@app.route("/seller/earnings")
def seller_earnings():
    from models import Order as OrderModel
    wallet = (request.args.get("wallet") or
              request.cookies.get("seller_wallet") or
              request.cookies.get("buyer_wallet") or "")
    my_agents = [a.to_dict() for a in _agents_for_seller(wallet)] if wallet else []
    earnings = _seller_earnings_from_chain([a["id"] for a in my_agents])
    orders = ([o.to_dict() for o in OrderModel.query
               .filter(OrderModel.agent_id.in_([a["id"] for a in my_agents])).all()]
              if my_agents else [])

    # Real transaction history for this seller's agents, from ChainTransaction.
    # Replaces the 5 hardcoded rows; grows with every on-chain event.
    from models import ChainTransaction as CT
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
                "snowtrace": (f"https://subnets-test.avax.network/c-chain/tx/{r.tx_hash if r.tx_hash.startswith('0x') else '0x' + r.tx_hash}") if r.tx_hash else None,
            })

    # Weekly settle counts per day (last 7 days) for the usage chart — live from CT
    import datetime as _dt
    weekly_labels, weekly_tasks = [], []
    for i in range(6, -1, -1):
        day_start_dt = (_dt.datetime.utcnow() - _dt.timedelta(days=i)).replace(hour=0, minute=0, second=0, microsecond=0)
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


@app.route("/seller/agents/<int:agent_id>", methods=["GET", "POST"])
def seller_manage_agent(agent_id):
    from models import Agent as AgentModel
    row = db.session.get(AgentModel, agent_id)
    if not row:
        return redirect(url_for("seller_earnings"))
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
                return _api_error("prices must be numeric", field="min_price")
            row.current_price = min(max(row.current_price, row.min_price), max(row.max_price, row.min_price))
            db.session.commit()
            log.info("Agent %s updated by seller", agent_id)
            return jsonify({"agentId": agent_id, "status": "updated"})
        return _api_error("unknown action", field="action")
    return render_template("seller/manage.html", agent=row.to_dict(), categories=CATEGORIES)



# ── Admin ──────────────────────────────────────────────────────────────────────

@app.route("/admin/dashboard")
def admin_dashboard():
    # Default to live-on-chain view so reviewers first-impression isn't 4k+
    # "active orders" from seeded sim ticks. Explicit ?all=1 flips to the
    # full aggregate; ?live_only=0 also respected for backwards compat.
    all_flag  = request.args.get("all", "").lower() in ("1", "true", "yes")
    live_raw  = request.args.get("live_only", "").lower()
    live_only = not all_flag if live_raw == "" else (live_raw in ("1", "true", "yes"))
    s = _live_chain_stats(live_only=live_only)
    # Real per-hour revenue (last 24h) aggregated from ChainTransaction settlements.
    try:
        from models import ChainTransaction as CT
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
    from models import Agent as AgentModel
    agents = [a.to_dict() for a in AgentModel.query.order_by(AgentModel.id.desc()).limit(8).all()]
    return render_template("admin/dashboard.html", stats=s, agents=agents)

@app.route("/admin/verification-queue")
def admin_verification_queue():
    from models import VerificationEntry
    queue = [v.to_dict() for v in VerificationEntry.query
             .order_by(VerificationEntry.created_at.desc()).all()]
    return render_template("admin/verification_queue.html", queue=queue)




@app.route("/admin/review/<vrf_id>", methods=["GET", "POST"])
def admin_human_review(vrf_id):
    """Human review panel for Thorough Audit tier agents."""
    from models import VerificationEntry
    row = db.session.get(VerificationEntry, vrf_id)
    if not row:
        return redirect(url_for("admin_verification_queue"))
    if request.method == "POST":
        data = request.get_json(silent=True) or request.form.to_dict()
        action = data.get("action")
        if action == "approve":
            return admin_approve_verification(vrf_id)
        if action == "reject":
            return admin_reject_verification(vrf_id)
        return _api_error("action must be approve or reject", field="action")
    agent = _get_agent(row.agent_id) if row.agent_id else None
    return render_template("admin/review.html", entry=row.to_dict(), agent=agent)



@app.route("/admin/moderation")
def admin_moderation():
    from models import ModerationReport
    reports = [r.to_dict() for r in
               ModerationReport.query.order_by(ModerationReport.created_at.desc()).all()]
    return render_template("admin/moderation.html", reports=reports)


@app.route("/admin/payouts")
def admin_payouts():
    from models import Payout
    payouts = [p.to_dict() for p in
               Payout.query.order_by(Payout.created_at.desc()).all()]
    # Same default as /admin/dashboard — show live-on-chain numbers first.
    all_flag  = request.args.get("all", "").lower() in ("1", "true", "yes")
    live_raw  = request.args.get("live_only", "").lower()
    live_only = not all_flag if live_raw == "" else (live_raw in ("1", "true", "yes"))
    s = _live_chain_stats(live_only=live_only)
    s["hourly_revenue"] = [0]*24
    s["revenue_labels"] = [f"{i:02d}:00" for i in range(24)]
    return render_template("admin/payouts.html", payouts=payouts, stats=s)

# ── API (mock) ─────────────────────────────────────────────────────────────────

# ── x402 / on-chain integration ────────────────────────────────────────────────
# FACILITATOR_PRIVATE_KEY set → onchain.py submits buyer-signed authorizations.
# Otherwise orders are recorded as pending_payment and no tx is sent.
import os
import uuid
FACILITATOR_URL = os.environ.get("FACILITATOR_URL")

_onchain = None
def _get_onchain():
    global _onchain
    if _onchain is None:
        try:
            from onchain import OnChain
            _onchain = OnChain.from_env()
        except Exception as e:
            print(f"[onchain] unavailable: {e}")
            _onchain = False
    return _onchain or None

# ── Live on-chain aggregates (sourced from ChainTransaction audit log) ──────
PLATFORM_FEE_BPS = 10

def _live_chain_stats(live_only: bool = False) -> dict:
    """Site-wide aggregate from ChainTransaction.
    live_only=True restricts to rows with meta.real=true (real Fuji txs
    fired from the demo), hiding the tick-engine backfill."""
    from models import ChainTransaction as CT, Agent as AgentModel, VerificationEntry
    from sqlalchemy import func as sfn
    import datetime as _dt
    def _scope(q):
        return q.filter(CT.meta.like('%"real": true%')) if live_only else q
    try:
        deposit_micro = _scope(db.session.query(sfn.sum(CT.amount_usdc))).filter(CT.kind == "deposit").scalar() or 0
        settle_micro  = _scope(db.session.query(sfn.sum(CT.amount_usdc))).filter(CT.kind == "settle").scalar() or 0
        stake_micro   = _scope(db.session.query(sfn.sum(CT.amount_usdc))).filter(CT.kind == "stake").scalar() or 0
        deposit_count = _scope(db.session.query(sfn.count(CT.id))).filter(CT.kind == "deposit").scalar() or 0
        settle_count  = _scope(db.session.query(sfn.count(CT.id))).filter(CT.kind == "settle").scalar() or 0
        slash_count   = _scope(db.session.query(sfn.count(CT.id))).filter(CT.kind == "slash").scalar() or 0
        # Transparency counts (always available, regardless of live_only)
        total_all = db.session.query(sfn.count(CT.id)).scalar() or 0
        real_all  = db.session.query(sfn.count(CT.id)).filter(CT.meta.like('%"real": true%')).scalar() or 0
        now_ts = int(time.time())
        monthly, labels = [], []
        for i in range(11, -1, -1):
            d = _dt.datetime.utcfromtimestamp(now_ts) - _dt.timedelta(days=i*30)
            mstart = int(_dt.datetime(d.year, d.month, 1, tzinfo=_dt.timezone.utc).timestamp())
            mnext = int(_dt.datetime(d.year + (1 if d.month == 12 else 0), 1 if d.month == 12 else d.month+1, 1, tzinfo=_dt.timezone.utc).timestamp())
            v = _scope(db.session.query(sfn.sum(CT.amount_usdc))).filter(CT.kind == "deposit", CT.ts >= mstart, CT.ts < mnext).scalar() or 0
            monthly.append(round(v / 1_000_000, 2))
            labels.append(d.strftime("%b"))
        total_volume = deposit_micro / 1_000_000
        return {
            "total_agents": AgentModel.query.count(),
            "verified_agents": AgentModel.query.filter(AgentModel.verified.is_(True)).count(),
            "tasks_completed": int(settle_count),
            "usdc_settled": round(settle_micro / 1_000_000, 2),
            "total_volume": round(total_volume, 2),
            "platform_fees": round(total_volume * PLATFORM_FEE_BPS / 10_000, 4),
            "deposit_count": int(deposit_count),
            "settle_count": int(settle_count),
            "slash_count": int(slash_count),
            "total_stake_usdc": round(stake_micro / 1_000_000, 2),
            "active_orders": max(0, int(deposit_count) - int(settle_count)),
            "pending_verifications": VerificationEntry.query.filter(
                VerificationEntry.status.in_(["pending", "testing", "human_review"])).count(),
            "monthly": monthly,
            "monthly_labels": labels,
            "live_only":       bool(live_only),
            "total_ct_rows":   int(total_all),
            "real_ct_rows":    int(real_all),
            "source": "onchain:ChainTransaction" + (" (live only)" if live_only else ""),
        }
    except Exception as e:
        return {"total_agents": 0, "verified_agents": 0, "tasks_completed": 0,
                "usdc_settled": 0, "total_volume": 0, "platform_fees": 0,
                "deposit_count": 0, "settle_count": 0, "slash_count": 0,
                "total_stake_usdc": 0, "active_orders": 0, "pending_verifications": 0,
                "monthly": [0]*12, "monthly_labels": [],
                "live_only": bool(live_only), "total_ct_rows": 0, "real_ct_rows": 0,
                "source": "fallback", "error": str(e)[:120]}


def _seller_earnings_from_chain(agent_ids: list) -> dict:
    """Seller revenue aggregation. Every figure is filtered to REAL Fuji
    txs (meta.real=true). Previously included the 136k seeded sim rows
    which made /seller/earnings show fabricated $1,994 revenue for a
    seller who had never actually been hired."""
    from models import ChainTransaction as CT
    from sqlalchemy import func as sfn
    import datetime as _dt
    if not agent_ids:
        return {"total_revenue": 0, "platform_fees": 0, "escrow_funds": 0,
                "released_payouts": 0, "monthly": [0]*12,
                "labels": [], "source": "onchain"}
    REAL = '%"real": true%'
    try:
        ids = [int(a) for a in agent_ids]
        revenue_micro = db.session.query(sfn.sum(CT.amount_usdc)).filter(
            CT.agent_id.in_(ids), CT.kind.in_(["deposit", "a2a_hire"]),
            CT.meta.like(REAL)).scalar() or 0
        settled_micro = db.session.query(sfn.sum(CT.amount_usdc)).filter(
            CT.agent_id.in_(ids), CT.kind.in_(["settle", "a2a_settle"]),
            CT.meta.like(REAL)).scalar() or 0
        now_ts = int(time.time())
        monthly, labels = [], []
        for i in range(11, -1, -1):
            d = _dt.datetime.utcfromtimestamp(now_ts) - _dt.timedelta(days=i*30)
            mstart = int(_dt.datetime(d.year, d.month, 1, tzinfo=_dt.timezone.utc).timestamp())
            mnext = int(_dt.datetime(d.year + (1 if d.month == 12 else 0), 1 if d.month == 12 else d.month+1, 1, tzinfo=_dt.timezone.utc).timestamp())
            v = db.session.query(sfn.sum(CT.amount_usdc)).filter(
                CT.agent_id.in_(ids), CT.kind.in_(["deposit", "a2a_hire"]),
                CT.meta.like(REAL), CT.ts >= mstart, CT.ts < mnext
            ).scalar() or 0
            monthly.append(round(v / 1_000_000, 2))
            labels.append(d.strftime("%b"))
        total_revenue = round(revenue_micro / 1_000_000, 2)
        return {"total_revenue": total_revenue,
                "platform_fees": round(total_revenue * PLATFORM_FEE_BPS / 10_000, 4),
                "escrow_funds": round(max(0, revenue_micro - settled_micro) / 1_000_000, 2),
                "released_payouts": round(settled_micro / 1_000_000, 2),
                "monthly": monthly,
                "labels": labels, "source": "onchain:ChainTransaction(real)"}
    except Exception as e:
        return {"total_revenue": 0, "platform_fees": 0, "escrow_funds": 0,
                "released_payouts": 0, "monthly": [0]*12,
                "labels": [], "source": "fallback", "error": str(e)[:120]}




def _new_order_id() -> str:
    return f"ORD-{uuid.uuid4().hex[:8].upper()}"


def _record_order_from_payment(agent_id: int, buyer: str, amount_usdc: float, *,
                               paid: bool, task: str = "", tx_hash: str = "") -> str:
    """Persist an Order (and, for real payments, a ChainTransaction row).
    Returns the new order id."""
    from models import Order as OrderModel, ChainTransaction as CT
    import json as _json
    order_id = _new_order_id()
    status = "in_escrow" if paid else "pending_payment"
    today = time.strftime("%Y-%m-%d")
    db.session.add(OrderModel(
        id=order_id, agent_id=int(agent_id), buyer=buyer,
        amount=float(amount_usdc), status=status,
        task=task or "Hire via x402 payment", date=today,
    ))
    if paid and tx_hash:
        db.session.add(CT(
            tx_hash=tx_hash, ts=int(time.time()), kind="payment",
            agent_id=int(agent_id), from_addr=buyer, to_addr="",
            amount_usdc=int(round(amount_usdc * 1_000_000)),
            meta=_json.dumps({"real": True, "orderId": order_id}),
        ))
    db.session.commit()
    return order_id


@app.route("/api/x402/pay", methods=["POST"])
@limiter.limit("30/minute")
def api_x402_pay():
    """Execute a buyer-signed EIP-3009 transferWithAuthorization via the
    facilitator and record the resulting order."""
    payload = request.get_json(silent=True) or {}
    required = ["from", "to", "value", "validBefore", "nonce", "v", "r", "s", "agentId"]
    missing = [k for k in required if k not in payload]
    if missing:
        return _api_error(f"missing fields: {missing}", field=missing[0])
    for field in ("from", "to"):
        if not _is_valid_wallet(payload.get(field)):
            return _api_error(f"'{field}' must be a 0x-prefixed 40-hex address", field=field)
    try:
        value_micro = int(payload["value"])
        agent_id = int(payload["agentId"])
    except (TypeError, ValueError):
        return _api_error("value and agentId must be integers", field="value")
    if value_micro <= 0:
        return _api_error("value must be > 0", field="value")
    from models import Agent as AgentModel
    if not db.session.get(AgentModel, agent_id):
        return _api_error("agent not found", 404, code="AGENT_NOT_FOUND", field="agentId")

    amount_usdc = value_micro / 1_000_000.0
    buyer_addr = payload["from"]
    task = str(payload.get("task") or "")[:4000]

    oc = _get_onchain()
    if oc and oc.facilitator:
        try:
            result = oc.x402_execute(payload)
        except Exception as e:
            log.warning("x402 execute failed: %s", e)
            return _api_error(f"on-chain payment failed: {str(e)[:200]}", 502, code="PAYMENT_FAILED")
        tx_hash = (result.get("txHashes") or {}).get("permit") or ""
        order_id = _record_order_from_payment(agent_id, buyer_addr, amount_usdc,
                                              paid=True, task=task, tx_hash=tx_hash)
        return jsonify({**result, "orderId": order_id, "realTx": True})

    # No facilitator configured: record the order as awaiting payment.
    order_id = _record_order_from_payment(agent_id, buyer_addr, amount_usdc, paid=False, task=task)
    return jsonify({
        "orderId": order_id,
        "agentId": agent_id,
        "status": "pending_payment",
        "realTx": False,
        "note": "FACILITATOR_PRIVATE_KEY is not set, so the signed authorization was not submitted on-chain.",
    })

# Submit a dispute. Always lands in the moderation queue; when a gatekeeper
# key and reputation contract are configured, also records an on-chain incident.
@app.route("/api/dispute/submit", methods=["POST"])
def api_dispute_submit():
    from models import ModerationReport
    payload = request.get_json(silent=True) or {}
    for k in ("agentId", "severity", "reason", "affectedUser"):
        if k not in payload or payload.get(k) in (None, ""):
            return _api_error(f"missing {k}", field=k)
    if not _is_valid_wallet(payload.get("affectedUser")):
        return _api_error("affectedUser must be a 0x-prefixed 40-hex address", field="affectedUser")
    try:
        agent_id = int(payload["agentId"])
        severity = int(payload["severity"])
    except (TypeError, ValueError):
        return _api_error("agentId and severity must be integers", field="agentId")
    if severity not in (1, 2):
        return _api_error("severity must be 1 or 2", field="severity")

    from models import Agent as AgentModel
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
    oc = _get_onchain()
    if oc and oc.gatekeeper:
        try:
            result.update(oc.submit_incident(agent_id, payload["affectedUser"], severity))
        except Exception as e:
            log.warning("on-chain incident failed (report %s kept): %s", report.id, e)
            result["onchainError"] = str(e)[:200]
    return jsonify(result), 201


# Read a live escrow session from chain. Prefers Python-native onchain.py,
# falls back to the Node facilitator if configured.
@app.route("/api/session/<session_id>")
def api_session(session_id):
    try:
        sid = int(session_id)
    except ValueError:
        return jsonify({"error": "session id must be numeric"}), 400
    oc = _get_onchain()
    if oc:
        try:
            s = oc.get_session(sid)
            # Contract returns zero-struct for unknown sessions; map that to 404.
            if s.get("user") == "0x0000000000000000000000000000000000000000":
                return jsonify({"error": "session not found"}), 404
            return jsonify(s)
        except Exception as e:
            return jsonify({"error": str(e)}), 502
    if FACILITATOR_URL:
        try:
            import requests
            r = requests.get(f"{FACILITATOR_URL}/session/{session_id}", timeout=10)
            return (r.text, r.status_code, r.headers.items())
        except Exception as e:
            return jsonify({"error": str(e)}), 502
    return jsonify({"error": "no on-chain backend configured"}), 503


# On-chain deployment metadata for the frontend. Single source of truth is
# onchain.get_deployment(), which reads env overrides at call time so a new
# deployer only needs to export the relevant *_ADDRESS vars and restart.
@app.route("/api/onchain/info")
def api_onchain_info():
    from onchain import get_deployment
    return jsonify(get_deployment())


# /config.js - populates window.AGENTSLIST_CHAIN + window.AGENTSLIST_ADDRESSES
# from the active deployment so the frontend never carries hardcoded addresses.
# Loaded in base.html BEFORE static/js/contracts.js (which now only ships ABIs).
@app.route("/config.js")
def config_js():
    import json
    from flask import Response
    from onchain import get_deployment
    d = get_deployment()
    js = (
        "// Auto-generated from server env. Do not edit.\n"
        "window.AGENTSLIST_CHAIN = " + json.dumps({
            "chainId":    d["chainId"],
            "chainIdHex": d["chainIdHex"],
            "name":       d["chain"],
            "rpcUrl":     d["rpcUrl"],
            "explorer":   d["explorer"],
            "nativeCurrency": {"name": "AVAX", "symbol": "AVAX", "decimals": 18},
        }) + ";\n"
        "window.AGENTSLIST_ADDRESSES = " + json.dumps(d["contracts"]) + ";\n"
    )
    return Response(js, mimetype="application/javascript")


# ── REST Agent API ─────────────────────────────────────────────────────────────

@app.route("/api/agents")
def api_agents():
    """Paginated, filterable agent list."""
    from models import Agent as AgentModel
    category = request.args.get("category", "")
    use_case = request.args.get("use_case", "")
    verified = request.args.get("verified", "")
    try:
        page = max(1, int(request.args.get("page", 1)))
        per_page = max(1, min(50, int(request.args.get("per_page", 12))))
    except ValueError:
        return _api_error("page and per_page must be integers", field="page")

    q = _search_filter(_listed_agents_query(), request.args.get("q", ""))
    if category:
        q = q.filter(AgentModel.category == category)
    if use_case:
        q = q.filter(AgentModel.use_case == use_case)
    if verified == "true":
        q = q.filter(AgentModel.verified.is_(True))
    elif verified == "false":
        q = q.filter(AgentModel.verified.is_(False))
    total = q.count()
    rows = q.order_by(AgentModel.id).offset((page - 1) * per_page).limit(per_page).all()
    return jsonify({"agents": [a.to_dict() for a in rows], "total": total,
                    "page": page, "per_page": per_page})




@app.route("/api/agents/<int:agent_id>")
def api_agent(agent_id):
    agent = _get_agent(agent_id)
    if not agent:
        return _api_error("agent not found", 404, code="AGENT_NOT_FOUND")
    return jsonify(agent)




@app.route("/api/agents/register", methods=["POST"])
def api_agents_register():
    """Register a new agent on-chain via AgentRegistry.registerAgent."""
    payload = request.get_json(silent=True) or {}
    required = ["wallet", "name", "endpointURL"]
    missing = [k for k in required if k not in payload]
    if missing:
        return _api_error(f"missing fields: {missing}", field=missing[0])
    if not _is_valid_wallet(payload.get("wallet")):
        return _api_error("wallet must be a 0x-prefixed 40-hex address", field="wallet")
    if len(str(payload.get("name") or "").strip()) < 3:
        return _api_error("name must be at least 3 characters", field="name")

    oc = _get_onchain()
    if oc:
        try:
            result = oc.register_agent(payload["wallet"], payload["name"], payload["endpointURL"])
            return jsonify(result), 201
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    return jsonify({
        "agentId": None,
        "status": "mock_registered",
        "note": "No FACILITATOR_PRIVATE_KEY - registration not sent on-chain.",
    }), 201


# ── On-chain agent reads ────────────────────────────────────────────────────────

@app.route("/api/llm/status")
def api_llm_status():
    """Probe the Akash-hosted vLLM endpoint — does it respond, is model loaded."""
    from llm import health
    return jsonify(health())


@app.route("/api/agents/<int:agent_id>/generate", methods=["POST"])
def api_agent_generate(agent_id):
    """Have this agent respond via the Akash-hosted LLM (per-agent system prompt)."""
    from llm import generate as llm_generate
    from models import Agent as AgentModel
    agent = AgentModel.query.get_or_404(agent_id)
    body = request.get_json(silent=True) or {}
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        return jsonify({"error": "prompt required"}), 400
    # Augment the agent's bio with concrete pricing so Qwen can answer
    # cost/duration questions with real numbers instead of generic "it
    # depends on CPU/memory" filler.
    bio_parts = [getattr(agent, "description", "") or ""]
    if agent.billing == "per_token":
        bio_parts.append(
            f"Pricing: {float(agent.min_price):.4f}–{float(agent.max_price):.4f} USDC per token "
            f"(current rate: {float(agent.current_price):.4f} USDC/token). "
            f"Example: 1,000 tokens costs ${float(agent.current_price) * 1000:.2f} USDC."
        )
    else:  # per_minute
        bio_parts.append(
            f"Pricing: {float(agent.min_price):.4f}–{float(agent.max_price):.4f} USDC per minute "
            f"(current: {float(agent.current_price):.4f} USDC/min). "
            f"Example: 10 minutes costs ${float(agent.current_price) * 10:.2f} USDC."
        )
    bio_parts.append(
        f"On-chain reputation: tier T{getattr(agent, 'verification_tier', 'basic')}, "
        f"{agent.tasks_completed or 0} tasks settled, "
        f"rating {float(agent.rating or 0):.2f}/5."
    )
    enriched_bio = " ".join(bio_parts)
    try:
        out = llm_generate(prompt,
            agent_name=agent.name, agent_category=agent.category,
            agent_bio=enriched_bio,
            max_tokens=int(body.get("maxTokens", 400)),
            temperature=float(body.get("temperature", 0.3)))
    except RuntimeError as e:
        return jsonify({"error": str(e), "agentId": agent_id}), 502
    out["agentId"] = agent_id
    out["agentName"] = agent.name
    out["agentCategory"] = agent.category
    return jsonify(out)


def get_transactions(agent_id=None, kinds=None, limit=50, real_only=False) -> list:
    """Read the ChainTransaction audit log, newest first."""
    from models import ChainTransaction as CT
    q = CT.query
    if agent_id is not None:
        q = q.filter_by(agent_id=agent_id)
    if kinds:
        q = q.filter(CT.kind.in_(list(kinds)))
    if real_only:
        q = q.filter(CT.meta.like('%"real": true%'))
    return [tx.to_dict() for tx in q.order_by(CT.ts.desc()).limit(limit).all()]


@app.route("/api/agents/<int:agent_id>/transactions")
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


@app.route("/api/transactions")
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


# ── Escrow session management ───────────────────────────────────────────────────

@app.route("/api/session/<session_id>/cancel", methods=["POST"])
def api_session_cancel(session_id):
    try:
        sid = int(session_id)
    except ValueError:
        return jsonify({"error": "session id must be numeric"}), 400

    oc = _get_onchain()
    if oc:
        try:
            return jsonify(oc.cancel_session(sid))
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    if FACILITATOR_URL:
        try:
            import requests as _req
            r = _req.post(f"{FACILITATOR_URL}/session/{session_id}/cancel", timeout=15)
            return (r.text, r.status_code, r.headers.items())
        except Exception as e:
            return jsonify({"error": str(e)}), 502
    return jsonify({"error": "no on-chain backend configured"}), 503


# ── Admin action endpoints ──────────────────────────────────────────────────────

def _verification_or_404(vrf_id):
    from models import VerificationEntry
    entry = db.session.get(VerificationEntry, vrf_id)
    if not entry:
        return None, _api_error("verification entry not found", 404, code="VERIFICATION_NOT_FOUND")
    return entry, None


def _set_verification_status(vrf_id, status, *, allowed_from=None):
    entry, err = _verification_or_404(vrf_id)
    if err:
        return err
    if allowed_from and entry.status not in allowed_from:
        return _api_error(f"cannot move from '{entry.status}' to '{status}'", code="INVALID_STATE")
    entry.status = status
    if status == "approved" and entry.agent_id:
        from models import Agent as AgentModel
        ag = db.session.get(AgentModel, entry.agent_id)
        if ag:
            ag.verified = True
            ag.verification_tier = entry.tier
    db.session.commit()
    log.info("Verification %s -> %s", vrf_id, status)
    return jsonify({"id": vrf_id, "status": status})


@app.route("/admin/verification-queue/<vrf_id>/approve", methods=["POST"])
@require_api_key
def admin_approve_verification(vrf_id):
    return _set_verification_status(vrf_id, "approved",
                                    allowed_from={"pending", "testing", "human_review"})


@app.route("/admin/verification-queue/<vrf_id>/reject", methods=["POST"])
@require_api_key
def admin_reject_verification(vrf_id):
    return _set_verification_status(vrf_id, "rejected",
                                    allowed_from={"pending", "testing", "human_review"})


@app.route("/admin/verification-queue/<vrf_id>/test-start", methods=["POST"])
@require_api_key
def admin_start_testing(vrf_id):
    return _set_verification_status(vrf_id, "testing", allowed_from={"pending"})


@app.route("/admin/verification-queue/<vrf_id>/escalate", methods=["POST"])
@require_api_key
def admin_escalate_verification(vrf_id):
    return _set_verification_status(vrf_id, "human_review", allowed_from={"pending", "testing"})


def _set_payout_status(pay_id, status, *, allowed_from):
    from models import Payout
    p = db.session.get(Payout, pay_id)
    if not p:
        return _api_error("payout not found", 404, code="PAYOUT_NOT_FOUND")
    if p.status not in allowed_from:
        return _api_error(f"payout is '{p.status}', cannot move to '{status}'", code="INVALID_STATE")
    p.status = status
    db.session.commit()
    log.info("Payout %s -> %s", pay_id, status)
    return jsonify({"id": pay_id, "status": status})


@app.route("/admin/payouts/<pay_id>/release", methods=["POST"])
@require_api_key
def admin_release_payout(pay_id):
    return _set_payout_status(pay_id, "released", allowed_from={"pending", "held"})


@app.route("/admin/payouts/<pay_id>/hold", methods=["POST"])
@require_api_key
def admin_hold_payout(pay_id):
    return _set_payout_status(pay_id, "held", allowed_from={"pending"})


@app.route("/admin/payouts/<pay_id>/refund", methods=["POST"])
@require_api_key
def admin_refund_payout(pay_id):
    return _set_payout_status(pay_id, "refunded", allowed_from={"pending", "held"})


@app.route("/admin/payouts/release-all", methods=["POST"])
@require_api_key
def admin_release_all_payouts():
    from models import Payout
    pending = Payout.query.filter_by(status="pending").all()
    released_ids = []
    for p in pending:
        p.status = "released"
        released_ids.append(p.id)
    db.session.commit()
    log.info("Bulk release: %d payouts", len(released_ids))
    return jsonify({"released": released_ids, "count": len(released_ids)})


def _report_or_404(rpt_id):
    from models import ModerationReport
    r = db.session.get(ModerationReport, rpt_id)
    if not r:
        return None, _api_error("report not found", 404, code="REPORT_NOT_FOUND")
    return r, None


@app.route("/admin/moderation/<rpt_id>/resolve", methods=["POST"])
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


@app.route("/admin/moderation/<rpt_id>/investigate", methods=["POST"])
@require_api_key
def admin_investigate_report(rpt_id):
    r, err = _report_or_404(rpt_id)
    if err:
        return err
    r.status = "investigating"
    db.session.commit()
    log.info("Moderation report under investigation: %s", rpt_id)
    return jsonify({"id": rpt_id, "status": "investigating"})


@app.route("/admin/moderation/<rpt_id>/suspend", methods=["POST"])
@require_api_key
def admin_suspend_agent(rpt_id):
    from models import Agent as AgentModel
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


# ── Order management ─────────────────────────────────────────────────────────



@app.route("/api/orders/<order_id>/complete", methods=["POST"])
def api_order_complete(order_id):
    """Buyer marks an order complete. DB status only: there is no escrow
    contract behind orders yet (milestone escrow is roadmap Phase 2)."""
    from models import Order as OrderModel
    row = db.session.get(OrderModel, order_id)
    if not row:
        return _api_error("order not found", 404, code="ORDER_NOT_FOUND")
    if row.status not in ("in_escrow", "in_progress"):
        return _api_error(f"order is {row.status}", code="INVALID_STATE")
    row.status = "completed"
    db.session.commit()
    log.info("Order %s marked complete", order_id)
    return jsonify({"orderId": order_id, "status": "completed"})




@app.route("/api/orders/<order_id>/start", methods=["POST"])
def api_order_start(order_id):
    """Seller marks a paid order as 'in_progress' to begin execution."""
    from models import Order as OrderModel
    row = db.session.get(OrderModel, order_id)
    if not row:
        return _api_error("order not found", 404, code="ORDER_NOT_FOUND")
    if row.status != "in_escrow":
        return _api_error(f"order cannot start from state '{row.status}'", code="INVALID_STATE")
    row.status = "in_progress"
    db.session.commit()
    log.info("Order %s started by seller", order_id)
    return jsonify({"orderId": order_id, "status": "in_progress"})




# ── Rating API ─────────────────────────────────────────────────────────────────

@app.route("/api/agents/<int:agent_id>/rate", methods=["POST"])
def api_rate_agent(agent_id):
    from models import Agent as AgentModel, Review as ReviewModel
    payload = request.get_json(silent=True) or {}
    try:
        rating = int(payload.get("rating"))
    except (TypeError, ValueError):
        rating = 0
    if not 1 <= rating <= 5:
        return _api_error("rating must be between 1 and 5", field="rating")
    agent = db.session.get(AgentModel, agent_id)
    if not agent:
        return _api_error("agent not found", 404, code="AGENT_NOT_FOUND")
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




# ── Search API ──────────────────────────────────────────────────────────────────

@app.route("/api/search")
def api_search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"results": []})
    rows = _search_filter(_listed_agents_query(), q).limit(50).all()
    results = [
        {"id": a.id, "name": a.name, "category": a.category,
         "description": a.description, "rating": a.rating,
         "verified": a.verified, "billing": a.billing,
         "current_price": a.current_price}
        for a in rows
    ]
    return jsonify({"results": results, "total": len(results), "query": q.lower()})




# ── Health / readiness ──────────────────────────────────────────────────────────

@app.route("/api/health")
def api_health():
    """Liveness probe - always returns 200 if the process is alive."""
    return jsonify({"status": "ok", "service": "agents-list", "ts": int(time.time())})


@app.route("/api/ready")
def api_ready():
    """Readiness probe - checks DB connectivity."""
    try:
        db.session.execute(db.text("SELECT 1"))
        return jsonify({"status": "ready", "db": "ok", "ts": int(time.time())})
    except Exception as e:
        log.error("Readiness check failed: %s", e)
        return jsonify({"status": "unavailable", "db": str(e)}), 503


# ── Error handlers ──────────────────────────────────────────────────────────────

@app.errorhandler(404)
def not_found(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": "not found"}), 404
    return render_template("404.html"), 404


@app.errorhandler(500)
def server_error(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": "internal server error"}), 500
    return render_template("500.html"), 500


@app.cli.command("seed")
def seed_command():
    """Load sample agents for local development."""
    from models import Agent as AgentModel
    from sample_data import seed_sample_agents
    added = seed_sample_agents(db, AgentModel)
    print(f"Added {added} sample agents.")


# ── DB init + seed ─────────────────────────────────────────────────────────────────



with app.app_context():
    try:
        # 1. Baseline schema (idempotent). Models must be imported first so
        #    their tables are registered on the metadata.
        import models  # noqa: F401
        db.create_all()
        # 2. Additive column migrations (SQLite can't add columns via create_all)
        from models import _ensure_columns
        _ensure_columns(app)
        log.info("Database ready.")
    except Exception as _seed_err:
        log.warning("DB seed skipped: %s", _seed_err)


if __name__ == "__main__":
    import sys

    # Default: 5000 on Windows/Linux (matches `flask run` and common bookmarks).
    # On macOS, AirPlay Receiver often binds 5000 — use 8080 unless PORT is set.
    default_port = 8080 if sys.platform == "darwin" else 5000
    port = int(os.environ.get("PORT", default_port))
    print(f"\n  Agent's List -> http://127.0.0.1:{port}/\n", flush=True)
    # Debug only in development so Werkzeug debugger / tracebacks don't leak
    # if someone ever runs this in a shared or exposed environment.
    debug_mode = os.environ.get("FLASK_ENV", "development") == "development"
    app.run(debug=debug_mode, host="127.0.0.1", port=port)
