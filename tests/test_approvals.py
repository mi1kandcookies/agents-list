from datetime import datetime, timedelta, timezone

import pytest

from app.approvals.actions import action_hash
from app.approvals.executors import ApprovalConsumptionError, consume_approval
from app.approvals.service import ApprovalError, approve_from_claims, create_approval
from app.models import Approval, Human, UsedIdTokenJti
from tests.conftest import WALLET


def _human(db):
    human = Human(id="human-1", world_sub="pairwise-1", wallet=WALLET)
    db.session.add(human)
    db.session.commit()
    return human


def test_approval_binds_nonce_and_is_single_use(app, db):
    with app.app_context():
        _human(db)
        expires = datetime.now(timezone.utc) + timedelta(minutes=5)
        terms = {"actionType": "fund", "amountAtomic": "50000", "payee": WALLET,
                 "screeningVerdictId": "v1"}
        approval = create_approval(human_id="human-1", action_type="fund", terms=terms,
                                  expires_at=expires, payload=terms)
        assert approval.state == "pending"
        claims = {"sub": "pairwise-1", "nonce": approval.action_hash, "auth_time": int(datetime.now(timezone.utc).timestamp()), "jti": "jti-1"}
        approved = approve_from_claims(approval.id, claims)
        assert approved.state == "approved"
        consumed = consume_approval(approval.id, approval.action_hash, human_id="human-1")
        assert consumed.state == "consumed"
        with pytest.raises(ApprovalConsumptionError, match="consumed"):
            consume_approval(approval.id, approval.action_hash, human_id="human-1")
        assert db.session.get(UsedIdTokenJti, "jti-1") is not None


def test_wrong_nonce_and_replayed_jti_are_rejected(app, db):
    with app.app_context():
        _human(db)
        approval = create_approval(human_id="human-1", action_type="release", terms={"amountAtomic": "1"},
                                  expires_at=datetime.now(timezone.utc) + timedelta(minutes=5))
        claims = {"sub": "pairwise-1", "nonce": "wrong", "auth_time": int(datetime.now(timezone.utc).timestamp()), "jti": "jti-2"}
        with pytest.raises(ApprovalError, match="nonce"):
            approve_from_claims(approval.id, claims)
        claims["nonce"] = approval.action_hash
        approve_from_claims(approval.id, claims)
        other = create_approval(human_id="human-1", action_type="release", terms={"amountAtomic": "2"},
                                expires_at=datetime.now(timezone.utc) + timedelta(minutes=5))
        claims["nonce"] = other.action_hash
        with pytest.raises(ApprovalError, match="replay"):
            approve_from_claims(other.id, claims)
