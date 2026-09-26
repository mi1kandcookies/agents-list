"""
models.py: SQLAlchemy ORM models for AgentHire.

Tables
------
agents              : Registered AI agents (mirrors AGENTS list)
orders              : Buyer orders / escrow sessions (mirrors ORDERS)
verification_entries: Verification queue entries (mirrors VERIFICATION_QUEUE)
auction_bids        : Open auction bids (mirrors _AUCTION_BIDS)
payouts             : Seller payouts tracked by the admin panel
moderation_reports  : User-filed reports reviewed by admins
reviews             : Buyer ratings and feedback per agent

Seed
----
Call seed_db(app) once at startup to populate the DB from the in-memory
mock data if the tables are empty. This gives instant persistence without
a manual migration step.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from extensions import db


# ── Agent ─────────────────────────────────────────────────────────────────────

class Agent(db.Model):
    __tablename__ = "agents"

    id                  = db.Column(db.Integer, primary_key=True)
    name                = db.Column(db.String(120), nullable=False)
    description         = db.Column(db.Text, nullable=False, default="")
    long_description    = db.Column(db.Text, nullable=False, default="")
    category            = db.Column(db.String(80), nullable=False)
    use_case            = db.Column(db.String(80), nullable=False, default="")
    verified            = db.Column(db.Boolean, nullable=False, default=False)
    verification_tier   = db.Column(db.String(20), nullable=False, default="none")
    featured            = db.Column(db.Boolean, nullable=False, default=False)
    rating              = db.Column(db.Float, nullable=False, default=0.0)
    reviews             = db.Column(db.Integer, nullable=False, default=0)
    billing             = db.Column(db.String(20), nullable=False)   # per_token | per_minute
    min_price           = db.Column(db.Float, nullable=False, default=0.001)
    max_price           = db.Column(db.Float, nullable=False, default=0.010)
    current_price       = db.Column(db.Float, nullable=False, default=0.001)
    surge_active        = db.Column(db.Boolean, nullable=False, default=False)
    surge_multiplier    = db.Column(db.Float, nullable=False, default=1.0)
    seller              = db.Column(db.String(120), nullable=False, default="")
    seller_rating       = db.Column(db.Float, nullable=False, default=0.0)
    tasks_completed     = db.Column(db.Integer, nullable=False, default=0)
    avg_completion_time = db.Column(db.String(20), nullable=False, default=" - ")
    # On-chain identity (each agent is an independently-deployed entity)
    model_provider      = db.Column(db.String(40), nullable=True)
    model_name          = db.Column(db.String(80), nullable=True)
    deployer_wallet     = db.Column(db.String(64), nullable=True)
    # I/O token pricing in USDC micro-units per 1M tokens (0 = unset)
    input_price_per_1m  = db.Column(db.Integer, nullable=False, default=0)
    output_price_per_1m = db.Column(db.Integer, nullable=False, default=0)
    # JSON-encoded lists
    _tags               = db.Column("tags", db.Text, nullable=False, default="[]")
    _capabilities       = db.Column("capabilities", db.Text, nullable=False, default="[]")
    created_at          = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    @property
    def tags(self) -> list[str]:
        return json.loads(self._tags)

    @tags.setter
    def tags(self, value: list[str]):
        self._tags = json.dumps(value)

    @property
    def capabilities(self) -> list[str]:
        return json.loads(self._capabilities)

    @capabilities.setter
    def capabilities(self, value: list[str]):
        self._capabilities = json.dumps(value)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "long_description": self.long_description,
            "category": self.category,
            "use_case": self.use_case,
            "verified": self.verified,
            "verification_tier": self.verification_tier,
            "featured": self.featured,
            "rating": self.rating,
            "reviews": self.reviews,
            "billing": self.billing,
            "min_price": self.min_price,
            "max_price": self.max_price,
            "current_price": self.current_price,
            "surge_active": self.surge_active,
            "surge_multiplier": self.surge_multiplier,
            "seller": self.seller,
            "seller_rating": self.seller_rating,
            "tasks_completed": self.tasks_completed,
            "avg_completion_time": self.avg_completion_time,
            "model_provider": self.model_provider,
            "model_name": self.model_name,
            "deployer_wallet": self.deployer_wallet,
            "input_price_per_1m": self.input_price_per_1m,
            "output_price_per_1m": self.output_price_per_1m,
            "input_price_display": round((self.input_price_per_1m or 0) / 1_000_000, 2),
            "output_price_display": round((self.output_price_per_1m or 0) / 1_000_000, 2),
            "tags": self.tags,
            "capabilities": self.capabilities,
        }

    def __repr__(self):
        return f"<Agent {self.id} {self.name!r}>"


# ── Order ─────────────────────────────────────────────────────────────────────

class Order(db.Model):
    __tablename__ = "orders"

    id       = db.Column(db.String(20), primary_key=True)   # e.g. "ORD-001"
    agent_id = db.Column(db.Integer, db.ForeignKey("agents.id"), nullable=False)
    agent    = db.relationship("Agent", backref="orders")
    buyer    = db.Column(db.String(80), nullable=False, default="0x0000...0000")
    amount   = db.Column(db.Float, nullable=False, default=0.0)
    status   = db.Column(db.String(20), nullable=False, default="in_escrow")
    # completed | in_progress | in_escrow | cancelled | disputed
    task     = db.Column(db.Text, nullable=False, default="")
    date     = db.Column(db.String(20), nullable=False, default="")
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "agent": self.agent.name if self.agent else "",
            "agent_id": self.agent_id,
            "buyer": self.buyer,
            "amount": self.amount,
            "status": self.status,
            "task": self.task,
            "date": self.date,
        }

    def __repr__(self):
        return f"<Order {self.id} {self.status}>"


# ── VerificationEntry ─────────────────────────────────────────────────────────

class VerificationEntry(db.Model):
    __tablename__ = "verification_entries"

    id                = db.Column(db.String(20), primary_key=True)  # e.g. "VRF-001"
    agent_id          = db.Column(db.Integer, db.ForeignKey("agents.id"), nullable=True)
    agent_name        = db.Column(db.String(120), nullable=False)
    seller            = db.Column(db.String(120), nullable=False)
    tier              = db.Column(db.String(20), nullable=False, default="basic")
    status            = db.Column(db.String(30), nullable=False, default="pending")
    # pending | testing | human_review | approved | rejected
    submitted         = db.Column(db.String(20), nullable=False, default="")
    safety_score      = db.Column(db.Integer, nullable=True)
    performance_score = db.Column(db.Integer, nullable=True)
    reliability_score = db.Column(db.Integer, nullable=True)
    created_at        = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "agent": self.agent_name,
            "agent_id": self.agent_id,
            "seller": self.seller,
            "tier": self.tier,
            "status": self.status,
            "submitted": self.submitted,
            "safety_score": self.safety_score,
            "performance_score": self.performance_score,
            "reliability_score": self.reliability_score,
        }

    def __repr__(self):
        return f"<VerificationEntry {self.id} {self.status}>"


# ── AuctionBid ────────────────────────────────────────────────────────────────

class AuctionBid(db.Model):
    __tablename__ = "auction_bids"

    id                = db.Column(db.Integer, primary_key=True, autoincrement=True)
    on_chain_bid_id   = db.Column(db.String(80), nullable=True)   # set when on-chain tx succeeds
    user              = db.Column(db.String(80), nullable=True)
    deposit_amount    = db.Column(db.BigInteger, nullable=False)   # in USDC micro-units
    token_budget      = db.Column(db.BigInteger, nullable=False)
    max_price_per_token = db.Column(db.BigInteger, nullable=False)
    category_id       = db.Column(db.Integer, nullable=False, default=0)
    min_tier          = db.Column(db.Integer, nullable=False, default=1)
    expires_at        = db.Column(db.BigInteger, nullable=False)   # unix timestamp
    settled           = db.Column(db.Boolean, nullable=False, default=False)
    cancelled         = db.Column(db.Boolean, nullable=False, default=False)
    tx_hash           = db.Column(db.String(80), nullable=True)
    created_at        = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "bidId": self.on_chain_bid_id or str(self.id),
            "user": self.user,
            "depositAmount": str(self.deposit_amount),
            "tokenBudget": str(self.token_budget),
            "maxPricePerToken": str(self.max_price_per_token),
            "categoryId": str(self.category_id),
            "minTier": self.min_tier,
            "expiresAt": self.expires_at,
            "settled": self.settled,
            "cancelled": self.cancelled,
            "txHash": self.tx_hash,
        }

    def __repr__(self):
        return f"<AuctionBid {self.id} settled={self.settled}>"


# ── Payout ────────────────────────────────────────────────────────────────────

class Payout(db.Model):
    __tablename__ = "payouts"

    id        = db.Column(db.String(20), primary_key=True)   # e.g. "PAY-001"
    seller    = db.Column(db.String(120), nullable=False)
    agent     = db.Column(db.String(120), nullable=False)
    amount    = db.Column(db.Float, nullable=False, default=0.0)
    status    = db.Column(db.String(20), nullable=False, default="pending")
    # pending | released | held
    date      = db.Column(db.String(20), nullable=False, default="")
    order_id  = db.Column(db.String(20), nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "seller": self.seller,
            "agent": self.agent,
            "amount": self.amount,
            "status": self.status,
            "date": self.date,
            "order_id": self.order_id,
        }

    def __repr__(self):
        return f"<Payout {self.id} {self.status}>"


# ── ModerationReport ──────────────────────────────────────────────────────────

class ModerationReport(db.Model):
    __tablename__ = "moderation_reports"

    id       = db.Column(db.String(20), primary_key=True)   # e.g. "RPT-001"
    agent    = db.Column(db.String(120), nullable=False)
    agent_id = db.Column(db.Integer, nullable=True)
    reporter = db.Column(db.String(80), nullable=False, default="")
    reason   = db.Column(db.Text, nullable=False, default="")
    status   = db.Column(db.String(20), nullable=False, default="open")
    # open | investigating | resolved | suspended
    date     = db.Column(db.String(20), nullable=False, default="")
    notes    = db.Column(db.Text, nullable=False, default="")
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "agent": self.agent,
            "agent_id": self.agent_id,
            "reporter": self.reporter,
            "reason": self.reason,
            "status": self.status,
            "date": self.date,
            "notes": self.notes,
        }

    def __repr__(self):
        return f"<ModerationReport {self.id} {self.status}>"


# ── Review ────────────────────────────────────────────────────────────────────

class Review(db.Model):
    __tablename__ = "reviews"

    id        = db.Column(db.Integer, primary_key=True, autoincrement=True)
    agent_id  = db.Column(db.Integer, db.ForeignKey("agents.id"), nullable=False, index=True)
    user      = db.Column(db.String(80), nullable=False, default="")
    rating    = db.Column(db.Integer, nullable=False, default=5)
    comment   = db.Column(db.Text, nullable=False, default="")
    date      = db.Column(db.String(20), nullable=False, default="")
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "agent_id": self.agent_id,
            "user": self.user,
            "rating": self.rating,
            "comment": self.comment,
            "date": self.date,
        }

    def __repr__(self):
        return f"<Review agent={self.agent_id} {self.rating}*>"


# ── OnchainProfile ────────────────────────────────────────────────────────────
# Mirrors what lives in ReputationContract + StakingSlashing for each agent.
# When the real on-chain backend is configured, reads fall through to it;
# otherwise the simulation layer reads/writes these rows as ground truth.

class OnchainProfile(db.Model):
    __tablename__ = "onchain_profiles"

    agent_id          = db.Column(db.Integer, db.ForeignKey("agents.id"), primary_key=True)
    wallet_address    = db.Column(db.String(64), nullable=False, default="")
    # Reputation side
    score             = db.Column(db.Integer, nullable=False, default=500)    # 0–1000
    tier              = db.Column(db.Integer, nullable=False, default=1)      # 1/2/3
    tasks_completed   = db.Column(db.Integer, nullable=False, default=0)
    rep_incident_count= db.Column(db.Integer, nullable=False, default=0)
    last_decay_ts     = db.Column(db.BigInteger, nullable=False, default=0)
    # Staking side (USDC micro-units, 6 decimals)
    staked_amount     = db.Column(db.BigInteger, nullable=False, default=0)
    stake_incident_count = db.Column(db.Integer, nullable=False, default=0)
    banned            = db.Column(db.Boolean, nullable=False, default=False)
    # Listing side
    accepting_work    = db.Column(db.Boolean, nullable=False, default=True)
    created_at        = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "agentId": self.agent_id,
            "wallet": self.wallet_address,
            "score": self.score,
            "tier": self.tier,
            "tasksCompleted": self.tasks_completed,
            "repIncidentCount": self.rep_incident_count,
            "lastDecayTs": self.last_decay_ts,
            "stakedUSDC": str(self.staked_amount),
            "stakedUSDCDisplay": round(self.staked_amount / 1_000_000, 2),
            "stakeIncidentCount": self.stake_incident_count,
            "banned": self.banned,
            "acceptingWork": self.accepting_work,
        }


# ── Transaction ───────────────────────────────────────────────────────────────
# Full on-chain activity log. Populated from real chain events when a node
# listener is wired up, or by the simulator for demo data.

class ChainTransaction(db.Model):
    __tablename__ = "chain_transactions"

    id           = db.Column(db.Integer, primary_key=True, autoincrement=True)
    tx_hash      = db.Column(db.String(80), nullable=False, index=True)
    block_number = db.Column(db.BigInteger, nullable=False, default=0)
    ts           = db.Column(db.BigInteger, nullable=False, index=True)   # unix seconds
    kind         = db.Column(db.String(24), nullable=False, index=True)
    # deposit | settle | refund | stake | unstake | slash | register |
    # listing_update | incident | bid_post | bid_claim | bid_cancel
    agent_id     = db.Column(db.Integer, db.ForeignKey("agents.id"), nullable=True, index=True)
    from_addr    = db.Column(db.String(64), nullable=False, default="")
    to_addr      = db.Column(db.String(64), nullable=False, default="")
    amount_usdc  = db.Column(db.BigInteger, nullable=False, default=0)    # micro-units
    meta         = db.Column(db.Text, nullable=False, default="{}")        # JSON blob
    created_at   = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "txHash": self.tx_hash,
            "blockNumber": self.block_number,
            "ts": self.ts,
            "kind": self.kind,
            "agentId": self.agent_id,
            "from": self.from_addr,
            "to": self.to_addr,
            "amountUSDC": str(self.amount_usdc),
            "amountUSDCDisplay": round(self.amount_usdc / 1_000_000, 4),
            "meta": json.loads(self.meta or "{}"),
        }


# ── PricePoint ────────────────────────────────────────────────────────────────
# One sample per agent per minute-ish. Drives the price-history chart and
# the surge-pricing engine's rolling-window lookups.

class PricePoint(db.Model):
    __tablename__ = "price_points"

    id              = db.Column(db.Integer, primary_key=True, autoincrement=True)
    agent_id        = db.Column(db.Integer, db.ForeignKey("agents.id"), nullable=False, index=True)
    ts              = db.Column(db.BigInteger, nullable=False, index=True)
    price_per_token = db.Column(db.Float, nullable=False)         # USDC
    utilization     = db.Column(db.Float, nullable=False, default=0.0)
    surge           = db.Column(db.Float, nullable=False, default=1.0)

    def to_dict(self) -> dict:
        return {
            "ts": self.ts,
            "price": self.price_per_token,
            "utilization": self.utilization,
            "surge": self.surge,
        }


# ── Seed helper ───────────────────────────────────────────────────────────────

def seed_db(app) -> None:
    """
    Populate the database from in-memory mock data on first run.
    Safe to call every startup: no-ops if tables already have rows.
    """
    with app.app_context():
        db.create_all()

        # Import mock data from app module to avoid circular imports at module level
        from app import (
            AGENTS as _AGENTS,
            ORDERS as _ORDERS,
            VERIFICATION_QUEUE as _VQ,
            PAYOUTS_SEED as _PAY,
            MODERATION_SEED as _MOD,
            REVIEWS_SEED as _REV,
        )

        if Agent.query.count() == 0:
            for a in _AGENTS:
                row = Agent(
                    id=a["id"], name=a["name"], description=a["description"],
                    long_description=a.get("long_description", a["description"]),
                    category=a["category"], use_case=a.get("use_case", ""),
                    verified=a["verified"], verification_tier=a.get("verification_tier", "none"),
                    featured=a["featured"], rating=a["rating"], reviews=a["reviews"],
                    billing=a["billing"], min_price=a["min_price"], max_price=a["max_price"],
                    current_price=a["current_price"], surge_active=a["surge_active"],
                    surge_multiplier=a["surge_multiplier"], seller=a["seller"],
                    seller_rating=a["seller_rating"], tasks_completed=a["tasks_completed"],
                    avg_completion_time=a.get("avg_completion_time", " - "),
                )
                row.tags = a.get("tags", [])
                row.capabilities = a.get("capabilities", [])
                db.session.add(row)
            db.session.commit()

        if Order.query.count() == 0:
            for o in _ORDERS:
                db.session.add(Order(
                    id=o["id"], agent_id=o["agent_id"], buyer=o["buyer"],
                    amount=o["amount"], status=o["status"],
                    task=o.get("task", ""), date=o.get("date", ""),
                ))
            db.session.commit()

        if VerificationEntry.query.count() == 0:
            for v in _VQ:
                db.session.add(VerificationEntry(
                    id=v["id"], agent_id=v.get("agent_id"),
                    agent_name=v["agent"], seller=v["seller"],
                    tier=v["tier"], status=v["status"], submitted=v["submitted"],
                    safety_score=v.get("safety_score"),
                    performance_score=v.get("performance_score"),
                    reliability_score=v.get("reliability_score"),
                ))
            db.session.commit()

        if Payout.query.count() == 0:
            for p in _PAY:
                db.session.add(Payout(
                    id=p["id"], seller=p["seller"], agent=p["agent"],
                    amount=p["amount"], status=p["status"],
                    date=p["date"], order_id=p.get("order_id"),
                ))
            db.session.commit()

        if ModerationReport.query.count() == 0:
            for r in _MOD:
                db.session.add(ModerationReport(
                    id=r["id"], agent=r["agent"], agent_id=r.get("agent_id"),
                    reporter=r["reporter"], reason=r["reason"],
                    status=r["status"], date=r["date"],
                ))
            db.session.commit()

        if Review.query.count() == 0:
            for rv in _REV:
                db.session.add(Review(
                    agent_id=rv["agent_id"], user=rv["user"],
                    rating=rv["rating"], comment=rv["comment"],
                    date=rv["date"],
                ))
            db.session.commit()
