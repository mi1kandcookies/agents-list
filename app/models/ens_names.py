"""Names issued for agents and jobs by the names sidecar."""
from __future__ import annotations

from app.extensions import db
from app.models._util import utcnow


class EnsName(db.Model):
    __tablename__ = "ens_names"

    name          = db.Column(db.String(255), primary_key=True)
    node          = db.Column(db.String(66), nullable=True)
    kind          = db.Column(db.String(8), nullable=False)      # root | agent | job | subjob
    parent_name   = db.Column(db.String(255), db.ForeignKey("ens_names.name"), nullable=True, index=True)
    engagement_id = db.Column(db.String(32), db.ForeignKey("engagements.id"), nullable=True)
    agent_id      = db.Column(db.Integer, db.ForeignKey("agents.id"), nullable=True)
    owner         = db.Column(db.String(64), nullable=True)
    expiry        = db.Column(db.BigInteger, nullable=True)      # unix seconds
    records       = db.Column(db.JSON, nullable=False, default=dict)
    status        = db.Column(db.String(8), nullable=False, default="pending")
    # pending | active | revoked | failed
    tx_hashes     = db.Column(db.JSON, nullable=False, default=list)
    updated_at    = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    def __repr__(self):
        return f"<EnsName {self.name} {self.status}>"
