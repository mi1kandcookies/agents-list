"""Approvals: one fresh, verified human sign-off bound to one action hash,
plus its audit trail and the id-token replay guard."""
from __future__ import annotations

import json

from app.extensions import db
from app.models._util import id_factory, utcnow


class Approval(db.Model):
    __tablename__ = "approvals"

    id                        = db.Column(db.String(32), primary_key=True, default=id_factory("APR"))
    kind                      = db.Column(db.String(32), nullable=False)
    action_json               = db.Column(db.Text, nullable=False)    # canonical(action)
    action_hash               = db.Column(db.String(66), nullable=False)
    nonce                     = db.Column(db.String(64), nullable=False)
    flow                      = db.Column(db.String(8), nullable=False)   # web | device
    state                     = db.Column(db.String(16), nullable=False, default="created")
    # created | pending | approved | consumed | denied | expired | cancelled | rejected | blocked | failed
    state_param               = db.Column(db.String(64), nullable=True, unique=True, index=True)
    pkce_verifier             = db.Column(db.String(128), nullable=True)
    device_code               = db.Column(db.String(255), nullable=True)
    user_code                 = db.Column(db.String(32), nullable=True)
    verification_uri          = db.Column(db.String(512), nullable=True)
    verification_uri_complete = db.Column(db.String(1024), nullable=True)
    poll_interval             = db.Column(db.Integer, nullable=True)
    next_poll_at              = db.Column(db.DateTime, nullable=True)
    expires_at                = db.Column(db.DateTime, nullable=False)
    human_sub                 = db.Column(db.String(255), nullable=True)
    id_token_jti              = db.Column(db.String(255), nullable=True, unique=True, index=True)
    auth_time                 = db.Column(db.BigInteger, nullable=True)   # unix seconds (token claim)
    acr                       = db.Column(db.String(255), nullable=True)
    failure_code              = db.Column(db.String(40), nullable=True)
    failure_detail            = db.Column(db.Text, nullable=True)
    consumed_at               = db.Column(db.DateTime, nullable=True)
    engagement_id             = db.Column(db.String(32), db.ForeignKey("engagements.id"), nullable=True, index=True)
    milestone_id              = db.Column(db.Integer, db.ForeignKey("milestones.id"), nullable=True)
    agent_id                  = db.Column(db.Integer, db.ForeignKey("agents.id"), nullable=True)
    screening_id              = db.Column(db.String(32), db.ForeignKey("screenings.id"), nullable=True)
    created_at                = db.Column(db.DateTime, nullable=False, default=utcnow)

    events = db.relationship("ApprovalEvent", back_populates="approval",
                             order_by="ApprovalEvent.id", cascade="all, delete-orphan")

    @property
    def action(self) -> dict:
        return json.loads(self.action_json)

    def __repr__(self):
        return f"<Approval {self.id} {self.kind} {self.state}>"


class ApprovalEvent(db.Model):
    __tablename__ = "approval_events"

    id          = db.Column(db.Integer, primary_key=True)
    approval_id = db.Column(db.String(32), db.ForeignKey("approvals.id"), nullable=False, index=True)
    event       = db.Column(db.String(40), nullable=False)
    detail      = db.Column(db.JSON, nullable=True)
    at          = db.Column(db.DateTime, nullable=False, default=utcnow)

    approval = db.relationship("Approval", back_populates="events")

    def __repr__(self):
        return f"<ApprovalEvent {self.approval_id} {self.event}>"


class UsedIdTokenJti(db.Model):
    """Every accepted id-token ``jti``; a repeat is a replay."""
    __tablename__ = "used_id_token_jtis"

    jti     = db.Column(db.String(255), primary_key=True)
    seen_at = db.Column(db.DateTime, nullable=False, default=utcnow)
