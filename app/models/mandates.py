"""Mandates: signed, narrowing grants of spending authority (root → sub-hires)."""
from __future__ import annotations

from app.extensions import db
from app.models._util import id_factory


class Mandate(db.Model):
    __tablename__ = "mandates"

    id                      = db.Column(db.String(32), primary_key=True, default=id_factory("MND"))
    parent_id               = db.Column(db.String(32), db.ForeignKey("mandates.id"), nullable=True, index=True)
    root_id                 = db.Column(db.String(32), nullable=False, index=True)
    engagement_id           = db.Column(db.String(32), db.ForeignKey("engagements.id"), nullable=False, index=True)
    human_id                = db.Column(db.Integer, db.ForeignKey("humans.id"), nullable=False)
    grantee_agent_public_id = db.Column(db.String(16), nullable=False)
    budget_micro            = db.Column(db.BigInteger, nullable=False)
    spent_micro             = db.Column(db.BigInteger, nullable=False, default=0)
    categories              = db.Column(db.JSON, nullable=False, default=list)
    max_depth               = db.Column(db.Integer, nullable=False)
    depth                   = db.Column(db.Integer, nullable=False, default=0)
    expires_at              = db.Column(db.DateTime, nullable=False)
    token                   = db.Column(db.Text, nullable=False)
    approval_id             = db.Column(db.String(32), db.ForeignKey("approvals.id"), nullable=True)
    revoked_at              = db.Column(db.DateTime, nullable=True)

    parent = db.relationship("Mandate", remote_side=[id])

    def __repr__(self):
        return f"<Mandate {self.id} depth={self.depth}>"
