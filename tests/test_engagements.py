from datetime import datetime, timedelta, timezone

from app.approvals.service import approve_from_claims, create_approval
from app.engagements.service import create_engagement
from app.models import Human
from tests.conftest import WALLET


def test_engagement_sow_and_milestone_lifecycle(client, app, db):
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
        approval = create_approval(
            human_id="human-eng", action_type="fund",
            terms={"actionType": "fund", "engagementId": body["id"], "amountAtomic": "100000"},
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        now = int(datetime.now(timezone.utc).timestamp())
        approve_from_claims(approval.id, {"sub": "world-eng", "nonce": approval.action_hash,
                                         "auth_time": now, "jti": "eng-jti-1"})
        funded = client.post(f"/api/engagements/{body['id']}/milestones/{body['milestones'][0]['id']}/fund", json={
            "approvalId": approval.id, "actionHash": approval.action_hash,
            "payee": WALLET, "screening": {"decision": "PAY", "verdictId": "screen-1"},
        })
        assert funded.status_code == 200, funded.get_json()
        milestone_id = body["milestones"][0]["id"]
        submitted = client.post(f"/api/engagements/{body['id']}/milestones/{milestone_id}/submit", json={
            "evidenceHash": "0x" + "1" * 64, "deliverable": "test cases",
        })
        assert submitted.status_code == 200
