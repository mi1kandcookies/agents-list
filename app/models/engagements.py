"""Engagements (scoped, funded work for one agent), their milestones and the
money ledger. Amounts are integer micro-USDC."""
from __future__ import annotations

from app.extensions import db
from app.models._util import id_factory, utcnow


class Engagement(db.Model):
    __tablename__ = "engagements"

    id                   = db.Column(db.String(32), primary_key=True, default=id_factory("ENG"))
    agent_id             = db.Column(db.Integer, db.ForeignKey("agents.id"), nullable=False, index=True)
    buyer_human_id       = db.Column(db.Integer, db.ForeignKey("humans.id"), nullable=True, index=True)
    buyer_address        = db.Column(db.String(64), nullable=True)
    parent_engagement_id = db.Column(db.String(32), db.ForeignKey("engagements.id"), nullable=True, index=True)
    depth                = db.Column(db.Integer, nullable=False, default=0)
    outcome              = db.Column(db.Text, nullable=False, default="")
    category             = db.Column(db.String(80), nullable=True)
    sow_json             = db.Column(db.Text, nullable=True)       # canonical JSON; hashed
    sow_hash             = db.Column(db.String(66), nullable=True)
    total_micro          = db.Column(db.BigInteger, nullable=False, default=0)
    currency             = db.Column(db.String(16), nullable=False, default="USDC")
    status               = db.Column(db.String(24), nullable=False, default="draft")
    # draft → scoped → awaiting_approval → funded → in_progress → completed | cancelled | refused
    deadline_at          = db.Column(db.DateTime, nullable=True)
    mandate_id           = db.Column(db.String(32), nullable=True)
    ens_name             = db.Column(db.String(255), nullable=True)
    created_at           = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at           = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    agent      = db.relationship("Agent")
    buyer      = db.relationship("Human")
    parent     = db.relationship("Engagement", remote_side=[id])
    milestones = db.relationship("Milestone", back_populates="engagement",
                                 order_by="Milestone.idx", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Engagement {self.id} {self.status}>"


class Milestone(db.Model):
    __tablename__ = "milestones"
    __table_args__ = (db.UniqueConstraint("engagement_id", "idx", name="uq_milestones_engagement_idx"),)

    id                 = db.Column(db.Integer, primary_key=True)
    engagement_id      = db.Column(db.String(32), db.ForeignKey("engagements.id"), nullable=False, index=True)
    idx                = db.Column(db.Integer, nullable=False)
    title              = db.Column(db.String(200), nullable=False, default="")
    acceptance         = db.Column(db.Text, nullable=False, default="")
    amount_micro       = db.Column(db.BigInteger, nullable=False, default=0)
    status             = db.Column(db.String(16), nullable=False, default="pending")
    # pending → funded → submitted → released | held | refunded
    submitted_at       = db.Column(db.DateTime, nullable=True)
    auto_release_at    = db.Column(db.DateTime, nullable=True)
    released_ledger_id = db.Column(db.String(32), nullable=True)

    engagement = db.relationship("Engagement", back_populates="milestones")

    def __repr__(self):
        return f"<Milestone {self.engagement_id}#{self.idx} {self.status}>"


class LedgerEntry(db.Model):
    __tablename__ = "ledger_entries"

    id            = db.Column(db.String(32), primary_key=True, default=id_factory("LED"))
    engagement_id = db.Column(db.String(32), db.ForeignKey("engagements.id"), nullable=False, index=True)
    milestone_id  = db.Column(db.Integer, db.ForeignKey("milestones.id"), nullable=True)
    kind          = db.Column(db.String(16), nullable=False)   # fund | release | refund | subhire_alloc | hold
    amount_micro  = db.Column(db.BigInteger, nullable=False)
    from_addr     = db.Column(db.String(64), nullable=True)
    to_addr       = db.Column(db.String(64), nullable=True)
    tx_hash       = db.Column(db.String(80), nullable=True)
    status        = db.Column(db.String(16), nullable=False, default="simulated")
    # simulated | pending | confirmed | failed
    approval_id   = db.Column(db.String(32), db.ForeignKey("approvals.id"), nullable=True)
    screening_id  = db.Column(db.String(32), db.ForeignKey("screenings.id"), nullable=True)
    created_at    = db.Column(db.DateTime, nullable=False, default=utcnow)

    def __repr__(self):
        return f"<LedgerEntry {self.id} {self.kind} {self.status}>"
