"""
services.py - shared domain helpers used by the blueprints.

Query helpers for the catalog, order bookkeeping, aggregate stats from the
ChainTransaction audit log, and the lazily-built chain client.
"""
from __future__ import annotations

import logging
import re as _re
import time
import uuid

from flask import jsonify

from app.extensions import db
from chain.config import explorer_url

log = logging.getLogger(__name__)

CATEGORIES = ["Development", "Data & Analytics", "Content", "Finance", "Research", "Security", "Automation"]
USE_CASES = ["Code Review", "Translation", "Summarization", "Web Scraping", "Image Generation", "Testing", "Resume & Career"]

# Listings in these verification tiers are hidden from the catalog, and so
# is any tier a seller paused (the tier name gets this suffix).
HIDDEN_TIERS = ("suspended",)
PAUSED_SUFFIX = "_paused"

# Platform fee in basis points (10 bps = 0.1%).
PLATFORM_FEE_BPS = 10


def is_valid_wallet(addr: str) -> bool:
    """EVM address shape check — 0x + 40 hex. Keeps junk out of Orders."""
    return bool(_re.fullmatch(r"0x[a-fA-F0-9]{40}", str(addr or "")))


def api_error(message: str, status: int = 400, *, code: str = "INVALID_REQUEST", field: str | None = None):
    """Uniform JSON error body: {error, code, field?}."""
    body = {"error": message, "code": code}
    if field:
        body["field"] = field
    return jsonify(body), status


def listed_agents_query():
    """Agents visible in the catalog: not suspended, not paused by the seller."""
    from app.models import Agent as AgentModel
    return AgentModel.query.filter(
        ~AgentModel.verification_tier.in_(HIDDEN_TIERS),
        ~AgentModel.verification_tier.endswith(PAUSED_SUFFIX, autoescape=True),
    )


def is_listed(agent) -> bool:
    """``listed_agents_query`` for one agent."""
    tier = agent.verification_tier or ""
    return tier not in HIDDEN_TIERS and not tier.endswith(PAUSED_SUFFIX)


def search_filter(query, text: str):
    """Case-insensitive match on name, description or tags."""
    from app.models import Agent as AgentModel
    text = (text or "").strip().lower()
    if not text:
        return query
    like = f"%{text}%"
    return query.filter(db.or_(
        db.func.lower(AgentModel.name).like(like),
        db.func.lower(AgentModel.description).like(like),
        db.func.lower(AgentModel._tags).like(like),
    ))


def get_agent(agent_id) -> dict | None:
    from app.models import Agent as AgentModel
    try:
        row = db.session.get(AgentModel, int(agent_id))
    except (TypeError, ValueError):
        return None
    return row.to_dict() if row else None


def agents_for_seller(wallet: str) -> list:
    """Agents owned by a seller wallet (matched on seller or deployer_wallet)."""
    from app.models import Agent as AgentModel
    w = (wallet or "").strip().lower()
    if not w:
        return []
    return (AgentModel.query
            .filter(db.or_(db.func.lower(AgentModel.seller) == w,
                           db.func.lower(AgentModel.deployer_wallet) == w))
            .order_by(AgentModel.id).all())


def orders_for_buyer(wallet: str, *, completed: bool) -> list:
    """Order rows for this buyer wallet, with fee breakdown and the payment
    tx (if any). Used by /past-jobs (completed) and /active-jobs (open)."""
    from app.models import Order as OrderModel, ChainTransaction as CT
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
        d["explorer"]  = explorer_url("tx", ct.tx_hash) if ct else ""
        d["sourceChain"] = bool(ct)
        d["orderUrl"]  = f"/order/{o.id}"
        out.append(d)
    return out


_onchain = None


def get_onchain():
    """Process-wide chain client, or None when web3/config is unavailable."""
    global _onchain
    if _onchain is None:
        try:
            from chain.client import OnChain
            _onchain = OnChain.from_env()
        except Exception as e:
            log.warning("chain client unavailable: %s", e)
            _onchain = False
    return _onchain or None


def live_chain_stats(live_only: bool = False) -> dict:
    """Site-wide aggregate from ChainTransaction.
    live_only=True restricts to rows with meta.real=true (transactions the
    platform actually submitted on-chain)."""
    from app.models import ChainTransaction as CT, Agent as AgentModel, VerificationEntry
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
            d = _dt.datetime.fromtimestamp(now_ts, tz=_dt.timezone.utc) - _dt.timedelta(days=i*30)
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


def seller_earnings_from_chain(agent_ids: list) -> dict:
    """Seller revenue aggregation. Every figure is filtered to real on-chain
    txs (meta.real=true)."""
    from app.models import ChainTransaction as CT
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
            d = _dt.datetime.fromtimestamp(now_ts, tz=_dt.timezone.utc) - _dt.timedelta(days=i*30)
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


def new_order_id() -> str:
    return f"ORD-{uuid.uuid4().hex[:8].upper()}"


def get_transactions(agent_id=None, kinds=None, limit=50, real_only=False) -> list:
    """Read the ChainTransaction audit log, newest first."""
    from app.models import ChainTransaction as CT
    q = CT.query
    if agent_id is not None:
        q = q.filter_by(agent_id=agent_id)
    if kinds:
        q = q.filter(CT.kind.in_(list(kinds)))
    if real_only:
        q = q.filter(CT.meta.like('%"real": true%'))
    return [tx.to_dict() for tx in q.order_by(CT.ts.desc()).limit(limit).all()]
