"""Admin mutation routes: API-key gate and state-transition rules."""
import pytest

from app.models import Payout

HEADERS = {"X-Api-Key": "test-admin-key"}


@pytest.fixture(autouse=True)
def _api_key(app):
    app.config["API_KEY"] = "test-admin-key"


def _payout(db, pid, status):
    db.session.add(Payout(id=pid, seller="seller", agent="agent", amount=10.0,
                          status=status, date="2026-04-20"))
    db.session.commit()


def test_admin_requires_api_key(client):
    resp = client.post("/admin/payouts/release-all")
    assert resp.status_code == 401


def test_release_rejected_when_refunded(client, db):
    _payout(db, "TST-PAY-001", "refunded")
    resp = client.post("/admin/payouts/TST-PAY-001/release", headers=HEADERS)
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "INVALID_STATE"


def test_refund_rejected_when_released(client, db):
    _payout(db, "TST-PAY-002", "released")
    resp = client.post("/admin/payouts/TST-PAY-002/refund", headers=HEADERS)
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "INVALID_STATE"


def test_release_pending_payout(client, db):
    _payout(db, "TST-PAY-003", "pending")
    resp = client.post("/admin/payouts/TST-PAY-003/release", headers=HEADERS)
    assert resp.status_code == 200
    assert db.session.get(Payout, "TST-PAY-003").status == "released"


def test_reject_verification_not_found_shape(client):
    resp = client.post("/admin/verification-queue/TST-VRF-MISSING/reject", headers=HEADERS)
    assert resp.status_code == 404
    assert resp.get_json()["code"] == "VERIFICATION_NOT_FOUND"


def test_resolve_report_not_found_shape(client):
    resp = client.post("/admin/moderation/TST-RPT-MISSING/resolve", headers=HEADERS)
    assert resp.status_code == 404
    assert resp.get_json()["code"] == "REPORT_NOT_FOUND"


def test_approve_verification_marks_agent_verified(client, db, agent):
    from app.models import Agent, VerificationEntry
    db.session.add(VerificationEntry(id="VRF-T1", agent_id=agent, agent_name="Test Agent",
                                     seller="s", tier="basic", status="pending"))
    db.session.commit()
    resp = client.post("/admin/verification-queue/VRF-T1/approve", headers=HEADERS)
    assert resp.status_code == 200
    assert db.session.get(Agent, agent).verified is True
