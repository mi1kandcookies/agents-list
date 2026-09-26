"""Humans: verified people who approve custody-chain actions, keyed by the
identity provider's pairwise ``sub``."""
from __future__ import annotations

from app.extensions import db
from app.models._util import utcnow


class Human(db.Model):
    __tablename__ = "humans"

    id               = db.Column(db.Integer, primary_key=True)
    world_sub        = db.Column(db.String(255), nullable=False, unique=True, index=True)
    created_at       = db.Column(db.DateTime, nullable=False, default=utcnow)
    last_seen_at     = db.Column(db.DateTime, nullable=True)
    banned_at        = db.Column(db.DateTime, nullable=True)
    ban_reason       = db.Column(db.Text, nullable=True)
    weekly_cap_micro = db.Column(db.BigInteger, nullable=True)  # None → HUMAN_WEEKLY_CAP_USDC

    @property
    def banned(self) -> bool:
        return self.banned_at is not None

    def __repr__(self):
        return f"<Human {self.id}>"
