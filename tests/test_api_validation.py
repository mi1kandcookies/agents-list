"""Request validation on write endpoints."""
import pytest

from tests.conftest import WALLET


def _permit(**overrides):
    body = {
        "from": WALLET, "to": WALLET, "value": 1_000_000, "validBefore": 9_999_999_999,
        "nonce": "0x" + "1" * 64, "v": 27, "r": "0x" + "1" * 64, "s": "0x" + "1" * 64,
        "agentId": 1,
    }
    body.update(overrides)
    return body


# /api/x402/pay settled any signed authorization without an approval; it is
# closed (410) whatever the body, valid or not, and records nothing.
@pytest.mark.parametrize("overrides", [
    {"from": "0x123", "to": "0x456", "value": 0},
    {"value": 0},
    {"task": "build it"},
])
def test_x402_pay_is_closed(client, db, agent, overrides):
    from app.models import Order
    resp = client.post("/api/x402/pay", json=_permit(agentId=agent, **overrides))
    assert resp.status_code == 410
    assert resp.get_json()["code"] == "LEGACY_PAYMENT_DISABLED"
    assert Order.query.count() == 0


def test_order_completion_is_closed(client, db, agent):
    """Marking an order complete (the old payout trigger) no longer changes it."""
    from app.models import Order
    db.session.add(Order(id="ORD-TEST0001", agent_id=agent, buyer=WALLET, amount=5.0,
                         status="in_escrow", task="t", date="2026-01-01"))
    db.session.commit()
    resp = client.post("/api/orders/ORD-TEST0001/complete")
    assert resp.status_code == 410
    assert resp.get_json()["code"] == "LEGACY_ORDER_COMPLETION_DISABLED"
    db.session.expire_all()
    assert db.session.get(Order, "ORD-TEST0001").status == "in_escrow"


def test_unprotected_generation_is_closed(client, agent, monkeypatch):
    """The old LLM preview cannot bypass the protected task path."""
    def should_not_run(*args, **kwargs):
        raise AssertionError("legacy generation must not invoke the LLM")

    monkeypatch.setattr("app.llm.generate", should_not_run)
    resp = client.post(f"/api/agents/{agent}/generate", json={"prompt": "do work"})
    assert resp.status_code == 410
    assert resp.get_json()["code"] == "LEGACY_GENERATION_DISABLED"


def test_agent_register_requires_valid_wallet(client):
    payload = {"wallet": "0x123", "name": "ab", "endpointURL": "foo"}
    resp = client.post("/api/agents/register", json=payload)
    assert resp.status_code == 400
    assert resp.get_json()["field"] == "wallet"


def test_legacy_checkout_is_closed(client, db, agent):
    from app.models import Order
    resp = client.post(f"/checkout/{agent}", json={"task": "x", "amount": 5})
    assert resp.status_code == 410
    assert resp.get_json()["code"] == "LEGACY_CHECKOUT_DISABLED"
    assert Order.query.count() == 0


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
