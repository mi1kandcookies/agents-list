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


def test_refund_rejected_when_released(client, db):
    _payout(db, "TST-PAY-002", "released")
    resp = client.post("/admin/payouts/TST-PAY-002/refund", headers=HEADERS)
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "INVALID_STATE"


# Releasing legacy order payouts from admin marked money released without any
# verified human approval, bypassing the custody chain. Those routes are gone
# (410); jobs pay out through milestone releases, each with its own approval.
@pytest.mark.parametrize("status", ["pending", "held", "refunded"])
def test_release_payout_is_disabled(client, db, status):
    _payout(db, "TST-PAY-003", status)
    resp = client.post("/admin/payouts/TST-PAY-003/release", headers=HEADERS)
    assert resp.status_code == 410
    assert resp.get_json()["code"] == "LEGACY_PAYOUT_DISABLED"
    assert db.session.get(Payout, "TST-PAY-003").status == status


def test_release_all_payouts_is_disabled(client, db):
    _payout(db, "TST-PAY-004", "pending")
    resp = client.post("/admin/payouts/release-all", headers=HEADERS)
    assert resp.status_code == 410
    assert resp.get_json()["code"] == "LEGACY_PAYOUT_DISABLED"
    assert db.session.get(Payout, "TST-PAY-004").status == "pending"


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
