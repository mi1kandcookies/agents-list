"""Request validation on write endpoints."""
from tests.conftest import WALLET


def _permit(**overrides):
    body = {
        "from": WALLET, "to": WALLET, "value": 1_000_000, "validBefore": 9_999_999_999,
        "nonce": "0x" + "1" * 64, "v": 27, "r": "0x" + "1" * 64, "s": "0x" + "1" * 64,
        "agentId": 1,
    }
    body.update(overrides)
    return body


def test_x402_pay_rejects_invalid_wallet(client):
    resp = client.post("/api/x402/pay", json=_permit(**{"from": "0x123", "to": "0x456", "value": 0}))
    assert resp.status_code == 410
    body = resp.get_json()
    assert body["code"] == "LEGACY_PAYMENT_DISABLED"


def test_x402_pay_rejects_zero_value(client, agent):
    resp = client.post("/api/x402/pay", json=_permit(value=0, agentId=agent))
    assert resp.status_code == 410
    assert resp.get_json()["code"] == "LEGACY_PAYMENT_DISABLED"


def test_x402_pay_without_facilitator_records_pending_order(client, agent):
    resp = client.post("/api/x402/pay", json=_permit(agentId=agent, task="build it"))
    assert resp.status_code == 410
    assert resp.get_json()["code"] == "LEGACY_PAYMENT_DISABLED"


def test_agent_register_requires_valid_wallet(client):
    payload = {"wallet": "0x123", "name": "ab", "endpointURL": "foo"}
    resp = client.post("/api/agents/register", json=payload)
    assert resp.status_code == 400
    assert resp.get_json()["field"] == "wallet"


def test_checkout_requires_buyer_wallet(client, agent):
    resp = client.post(f"/checkout/{agent}", json={"task": "x", "amount": 5})
    assert resp.status_code == 400
    assert resp.get_json()["field"] == "buyer"


def test_rating_bounds(client, agent):
    assert client.post(f"/api/agents/{agent}/rate", json={"rating": 9}).status_code == 400
    resp = client.post(f"/api/agents/{agent}/rate", json={"rating": 4, "feedback": "solid"})
    assert resp.status_code == 200
    assert resp.get_json() == {"agentId": agent, "rating": 4.0, "reviews": 1}


def test_dispute_lands_in_moderation_queue(client, db, agent):
    from app.models import ModerationReport
    resp = client.post("/api/dispute/submit", json={
        "agentId": agent, "severity": 1, "reason": "did not deliver", "affectedUser": WALLET})
    assert resp.status_code == 201
    assert db.session.get(ModerationReport, resp.get_json()["reportId"]).status == "open"


def test_seller_create_requires_wallet(client):
    resp = client.post("/seller/create", json={
        "name": "X Agent", "description": "d", "category": "Development", "billing": "per_token"})
    assert resp.status_code == 400
    assert resp.get_json()["field"] == "wallet"


def test_seller_listing_gets_stable_public_agent_id(client, db):
    from app.common.agent_ids import is_valid_agent_id
    from app.models import Agent
    response = client.post("/seller/create", json={
        "wallet": WALLET, "name": "Scoped QA Agent", "description": "Writes plans",
        "category": "Development", "billing": "per_token",
    })
    assert response.status_code == 201, response.get_json()
    row = Agent.query.filter_by(name="Scoped QA Agent").one()
    assert is_valid_agent_id(row.public_id)
    public = client.get(f"/api/agents/public/{row.public_id}")
    assert public.status_code == 200
    assert public.get_json()["public_id"] == row.public_id


def test_public_agent_endpoint_rejects_unchecked_identifier(client):
    response = client.get("/api/agents/public/1")
    assert response.status_code == 400
    assert response.get_json()["code"] == "INVALID_AGENT_ID"
