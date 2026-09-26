"""Screenings: one risk verdict (PAY | CAP | REFUSE | ASK_HUMAN) per payment hop."""
from __future__ import annotations

from app.extensions import db
from app.models._util import id_factory, utcnow


class Screening(db.Model):
    __tablename__ = "screenings"

    id               = db.Column(db.String(32), primary_key=True, default=id_factory("SCR"))
    hop              = db.Column(db.String(32), nullable=False)
    engagement_id    = db.Column(db.String(32), db.ForeignKey("engagements.id"), nullable=True, index=True)
    agent_id         = db.Column(db.Integer, db.ForeignKey("agents.id"), nullable=True)
    chain_address    = db.Column(db.String(64), nullable=False)
    screened_address = db.Column(db.String(64), nullable=True)
    network          = db.Column(db.String(32), nullable=True)
    verdict          = db.Column(db.String(12), nullable=False)
    cap_micro        = db.Column(db.BigInteger, nullable=True)
    reasons          = db.Column(db.JSON, nullable=False, default=list)
    toxic_score      = db.Column(db.Integer, nullable=True)
    traits           = db.Column(db.JSON, nullable=False, default=list)
    risk_group       = db.Column(db.String(32), nullable=True)
    raw              = db.Column(db.JSON, nullable=True)
    provider         = db.Column(db.String(40), nullable=False, default="")
    fail_closed      = db.Column(db.Boolean, nullable=False, default=False)
    latency_ms       = db.Column(db.Integer, nullable=True)
    created_at       = db.Column(db.DateTime, nullable=False, default=utcnow)
    expires_at       = db.Column(db.DateTime, nullable=True)

    def __repr__(self):
        return f"<Screening {self.id} {self.hop} {self.verdict}>"
