"""Server-owned purchase intents for agent-to-agent task payments."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from app.extensions import db
from app.models._util import id_factory, utcnow


class HireIntent(db.Model):
    __tablename__ = "hire_intents"

    id                    = db.Column(db.String(32), primary_key=True, default=id_factory("HIT"))
    # The hash authenticates the immutable terms; it is not a row identity.
    # Two fresh intents can intentionally quote the same task and terms.
    intent_hash           = db.Column(db.String(66), nullable=False, index=True)
    agent_id              = db.Column(db.Integer, db.ForeignKey("agents.id"), nullable=False, index=True)
    agent_public_id       = db.Column(db.String(16), nullable=False)
    ens_name              = db.Column(db.String(255), nullable=True)
    endpoint_path         = db.Column(db.String(255), nullable=False)
    task_text             = db.Column(db.Text, nullable=False)
    task_hash             = db.Column(db.String(66), nullable=False)
    network               = db.Column(db.String(32), nullable=False)
    token_address         = db.Column(db.String(64), nullable=False)
    pay_to                = db.Column(db.String(64), nullable=False)
    amount_micro          = db.Column(db.BigInteger, nullable=False)
    expires_at             = db.Column(db.DateTime, nullable=False)
    state                 = db.Column(db.String(24), nullable=False, default="created", index=True)
    payer_agent_public_id = db.Column(db.String(16), nullable=True)
    mandate_id            = db.Column(db.String(32), db.ForeignKey("mandates.id"), nullable=True)
    payment_nonce         = db.Column(db.String(66), nullable=True, unique=True)
    payment_entry_id      = db.Column(db.String(32), db.ForeignKey("ledger_entries.id"), nullable=True)
    deliverable_json      = db.Column(db.Text, nullable=True)
    failure_code          = db.Column(db.String(40), nullable=True)
    created_at            = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at            = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    agent = db.relationship("Agent")

    @property
    def deliverable(self) -> dict | None:
        if not self.deliverable_json:
            return None
        try:
            value = json.loads(self.deliverable_json)
        except (TypeError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def is_expired(self, now: datetime | None = None) -> bool:
        now = now or utcnow()
        expires = self.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=now.tzinfo)
        return expires <= now

    def to_dict(self) -> dict:
        expires = self.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return {
            "id": self.id,
            "intent_hash": self.intent_hash,
            "agent_id": self.agent_public_id,
            "ens_name": self.ens_name,
            "endpoint": self.endpoint_path,
            "task_hash": self.task_hash,
            "network": self.network,
            "token": self.token_address,
            "pay_to": self.pay_to,
            "amount_micro": int(self.amount_micro),
            "expires_at": int(expires.timestamp()),
            "state": self.state,
            "payer_agent_id": self.payer_agent_public_id,
            "mandate_id": self.mandate_id,
            "payment_nonce": self.payment_nonce,
            "payment_entry_id": self.payment_entry_id,
            "failure_code": self.failure_code,
            "deliverable": self.deliverable,
        }
