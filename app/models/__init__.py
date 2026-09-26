"""
models.py: SQLAlchemy ORM models for Agent's List.

Tables
------
agents              : Listed agents
orders              : Buyer orders (becomes Engagement in Phase 2)
verification_entries: Verification queue entries
payouts             : Seller payouts tracked by the admin panel
moderation_reports  : User-filed reports reviewed by admins
reviews             : Buyer ratings and feedback per agent

Custody-chain tables (docs/decisions/0001-custody-chain.md §11) live in
sibling modules and are re-exported at the bottom of this file.

Sample data for local development is loaded explicitly with `flask seed`.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from sqlalchemy import event
from sqlalchemy.orm.attributes import set_committed_value
from app.common.agent_ids import from_db_id
from app.extensions import db
from chain.config import explorer_url


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
    # Custody chain (docs/decisions/0001-custody-chain.md §11)
    public_id           = db.Column(db.String(16), nullable=True, unique=True, index=True)  # AGT-XXXX-XXXX-C
    payout_address      = db.Column(db.String(64), nullable=True)
    screening_address   = db.Column(db.String(64), nullable=True)   # mainnet address screened for Sepolia payouts
    manifest_json       = db.Column(db.Text, nullable=True)
    manifest_hash       = db.Column(db.String(66), nullable=True)
    manifest_stamped_at = db.Column(db.DateTime, nullable=True)
    manifest_stamp_approval_id = db.Column(db.String(32), nullable=True)
    manifest_stamp_sub  = db.Column(db.String(255), nullable=True)
    ens_name            = db.Column(db.String(255), nullable=True)

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
            "seller": self.seller,
            "seller_rating": self.seller_rating,
            "tasks_completed": self.tasks_completed,
            "avg_completion_time": self.avg_completion_time,
            "ens_name": self.ens_name,
            "model_provider": self.model_provider,
            "model_name": self.model_name,
            "deployer_wallet": self.deployer_wallet,
            "input_price_per_1m": self.input_price_per_1m,
            "output_price_per_1m": self.output_price_per_1m,
            "input_price_display": round((self.input_price_per_1m or 0) / 1_000_000, 2),
            "output_price_display": round((self.output_price_per_1m or 0) / 1_000_000, 2),
            "tags": self.tags,
            "capabilities": self.capabilities,
            **self._screening_fields(),
            **self._stamp_fields(),
        }

    def _screening_fields(self) -> dict:
        """Latest payee.onboard evidence for listing risk cards.

        A missing row is deliberately represented as un-screened; templates
        must never turn the absence of provider evidence into a green badge.
        """
        from app.seller.stamp import onboarding_screening
        row = onboarding_screening(self)
        if row is None:
            return {
                "screened_payout": False,
                "screening_verdict": None,
                "screening_id": None,
                "screening_toxic_score": None,
                "screening_traits": [],
                "screening_reasons": [],
                "screening_fail_closed": True,
            }
        return {
            "screened_payout": row.verdict == "PAY",
            "screening_verdict": row.verdict,
            "screening_id": row.id,
            "screening_toxic_score": row.toxic_score,
            "screening_traits": list(row.traits or []),
            "screening_reasons": list(row.reasons or []),
            "screening_fail_closed": bool(row.fail_closed),
        }

    def _stamp_fields(self) -> dict:
        """Operator stamp state for cards and the API (app/seller/stamp.py)."""
        from app.seller.stamp import stamp_status
        status = stamp_status(self)
        return {"public_id": self.public_id, "manifest_hash": self.manifest_hash,
                "operator_stamped": status.ok, "stamp_code": status.code,
                "stamp_reason": status.reason}

    def __repr__(self):
        return f"<Agent {self.id} {self.name!r}>"


@event.listens_for(Agent, "after_insert")
def _assign_public_id(mapper, connection, target):
    """Derive public_id from the new primary key (same rule as the 0002 backfill)."""
    if target.public_id is None:
        public_id = from_db_id(target.id)
        connection.execute(
            Agent.__table__.update().where(Agent.__table__.c.id == target.id).values(public_id=public_id)
        )
        set_committed_value(target, "public_id", public_id)


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
            "explorer": explorer_url("tx", self.tx_hash) if self.tx_hash else None,
        }


# ── Custody chain ─────────────────────────────────────────────────────────────

from app.models.humans import Human  # noqa: E402,F401
from app.models.screenings import Screening  # noqa: E402,F401
from app.models.engagements import Engagement, LedgerEntry, Milestone  # noqa: E402,F401
from app.models.approvals import Approval, ApprovalEvent, UsedIdTokenJti  # noqa: E402,F401
from app.models.mandates import Mandate  # noqa: E402,F401
from app.models.ens_names import EnsName  # noqa: E402,F401
from app.models.hire_intents import HireIntent  # noqa: E402,F401
