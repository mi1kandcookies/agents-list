"""Delivery: completed work handed to the buyer's Inbox."""
from __future__ import annotations

import json

from app.extensions import db
from app.models._util import id_factory, utcnow


class Delivery(db.Model):
    __tablename__ = "deliveries"

    id              = db.Column(db.String(32), primary_key=True, default=id_factory("DLV"))
    engagement_id   = db.Column(db.String(32), db.ForeignKey("engagements.id"), nullable=False, index=True)
    agent_id        = db.Column(db.Integer, db.ForeignKey("agents.id"), nullable=False)
    buyer_human_id  = db.Column(db.Integer, db.ForeignKey("humans.id"), nullable=True, index=True)
    title           = db.Column(db.String(200), nullable=False, default="")
    summary         = db.Column(db.Text, nullable=False, default="")
    attestation_json = db.Column(db.Text, nullable=False, default="{}")
    logs_json       = db.Column(db.Text, nullable=False, default="[]")
    milestones_json = db.Column(db.Text, nullable=False, default="[]")
    pdf_path        = db.Column(db.String(255), nullable=True)   # under app/static
    available_at    = db.Column(db.DateTime, nullable=False, default=utcnow)
    created_at      = db.Column(db.DateTime, nullable=False, default=utcnow)
    read_at         = db.Column(db.DateTime, nullable=True)

    engagement = db.relationship("Engagement")
    agent = db.relationship("Agent")

    @property
    def attestation(self) -> dict:
        return json.loads(self.attestation_json or "{}")

    @property
    def logs(self) -> list:
        return json.loads(self.logs_json or "[]")

    @property
    def milestones_met(self) -> list:
        return json.loads(self.milestones_json or "[]")
