from datetime import datetime, timedelta, timezone

from app.approvals.service import approve_from_claims, get_approval
from app.models import Human
from tests.conftest import WALLET


def test_engagement_sow_and_milestone_lifecycle(client, app, db, monkeypatch):
    monkeypatch.setenv("INTERCEPTA_MODE", "fixture")
    monkeypatch.setenv("INTERCEPTA_FIXTURE_DECISION", "ALLOW")
    with app.app_context():
        db.session.add(Human(id="human-eng", world_sub="world-eng", wallet=WALLET))
        db.session.commit()
        response = client.post("/api/engagements", json={
            "humanId": "human-eng", "title": "API test plan", "brief": "Test the users API",
            "budgetAtomic": 100_000, "milestones": [
                {"title": "Draft cases", "amountAtomic": 100_000,
                 "acceptanceCriteria": ["positive and negative cases"]},
            ],
        })
        assert response.status_code == 201, response.get_json()
        body = response.get_json()
        assert body["sowHash"].startswith("0x") and len(body["sowHash"]) == 66
        assert body["milestones"][0]["status"] == "pending"
        milestone_id = body["milestones"][0]["id"]
        approval_response = client.post(
            f"/api/engagements/{body['id']}/milestones/{milestone_id}/approval",
            json={"actionType": "fund", "payee": WALLET},
        )
        assert approval_response.status_code == 201, approval_response.get_json()
        approval_body = approval_response.get_json()
        approval = get_approval(approval_body["approvalId"])
        now = int(datetime.now(timezone.utc).timestamp())
        approve_from_claims(approval.id, {"sub": "world-eng", "nonce": approval.action_hash,
                                         "auth_time": now, "jti": "eng-jti-1"})
        funded = client.post(f"/api/engagements/{body['id']}/milestones/{milestone_id}/fund", json={
            "approvalId": approval.id, "actionHash": approval.action_hash, "payee": WALLET,
        })
        assert funded.status_code == 200, funded.get_json()
        submitted = client.post(f"/api/engagements/{body['id']}/milestones/{milestone_id}/submit", json={
            "evidenceHash": "0x" + "1" * 64, "deliverable": "test cases",
        })
        assert submitted.status_code == 200


def test_engagement_rechecks_provider_before_consuming_approval(client, app, db, monkeypatch):
    monkeypatch.setenv("INTERCEPTA_MODE", "fixture")
    monkeypatch.setenv("INTERCEPTA_FIXTURE_DECISION", "ALLOW")
    with app.app_context():
        db.session.add(Human(id="human-eng", world_sub="world-eng", wallet=WALLET))
        db.session.commit()
        response = client.post("/api/engagements", json={
            "humanId": "human-eng", "title": "API test plan", "brief": "Test the users API",
            "budgetAtomic": 100_000, "milestones": [{"title": "Draft", "amountAtomic": 100_000,
                                                        "acceptanceCriteria": ["cases"]}],
        })
        body = response.get_json()
        milestone_id = body["milestones"][0]["id"]
        approved = client.post(
            f"/api/engagements/{body['id']}/milestones/{milestone_id}/approval",
            json={"actionType": "fund", "payee": WALLET},
        ).get_json()
        approval = get_approval(approved["approvalId"])
        approve_from_claims(approval.id, {"sub": "world-eng", "nonce": approval.action_hash,
                                          "auth_time": int(datetime.now(timezone.utc).timestamp()),
                                          "jti": "eng-jti-2"})
        monkeypatch.setenv("INTERCEPTA_DENY_ADDRESS", WALLET)
        blocked = client.post(f"/api/engagements/{body['id']}/milestones/{milestone_id}/fund", json={
            "approvalId": approval.id, "actionHash": approval.action_hash, "payee": WALLET,
        })
        assert blocked.status_code == 409
        assert blocked.get_json()["code"] == "FUND_BLOCKED"
        assert get_approval(approval.id).state == "approved"
