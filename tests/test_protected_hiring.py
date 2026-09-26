"""Security-critical branches for the named-agent purchase flow."""
from __future__ import annotations

import json

import pytest

from tests.conftest import WALLET

SPECIALIST = "qa.example.eth"
SPECIALIST_ADDRESS = "0x" + "a" * 40


@pytest.fixture()
def hiring_fixture(monkeypatch):
    monkeypatch.setenv("ENS_MODE", "fixture")
    monkeypatch.setenv("ENS_FIXTURES_JSON", json.dumps({SPECIALIST: {
        "address": SPECIALIST_ADDRESS,
        "endpoint": "https://qa.example.test/work",
        "enabled": True,
        "status": "online",
        "resolver": "0x" + "c" * 40,
    }}))
    monkeypatch.setenv("INTERCEPTA_MODE", "fixture")
    monkeypatch.setenv("INTERCEPTA_FIXTURE_DECISION", "ALLOW")
    monkeypatch.setenv("HIRE_LOCAL_APPROVAL", "1")
    monkeypatch.setenv("HIRE_PAYMENT_MODE", "mock")


def _create(client):
    return client.post("/api/hiring/intents", json={
        "ownerId": "world-user-1", "payer": WALLET,
        "agent": SPECIALIST,
        "task": "POST /v1/users returns 201 for a valid request",
        "maxUsdc": 0.10,
    })


def _approve(client, body):
    approval = body["approval"]["approvalId"]
    return client.post(f"/api/hiring/approvals/{approval}/decision", json={
        "ownerId": "world-user-1", "state": "approved",
    })


def test_protected_flow_requires_approval_before_any_payment_or_result(client, hiring_fixture):
    response = _create(client)
    assert response.status_code == 201
    intent = response.get_json()
    work = client.post(f"/api/hiring/intents/{intent['id']}/work",
                       headers={"X-Hire-Demo-Payment": "1"}, json={})
    assert work.status_code == 403
    assert work.get_json()["code"] == "APPROVAL_REQUIRED"

    assert _approve(client, intent).status_code == 200
    required = client.post(f"/api/hiring/intents/{intent['id']}/work", json={})
    assert required.status_code == 402
    assert required.headers.get("PAYMENT-REQUIRED")
    assert required.get_json()["code"] == "PAYMENT_REQUIRED"

    paid = client.post(f"/api/hiring/intents/{intent['id']}/work",
                       headers={"X-Hire-Demo-Payment": "1"}, json={})
    assert paid.status_code == 200, paid.get_json()
    body = paid.get_json()
    assert body["intent"]["status"] == "delivered"
    assert body["receipt"]["provider"] == "local-demo-only"
    assert body["result"]["testCases"]

    duplicate = client.post(f"/api/hiring/intents/{intent['id']}/work",
                            headers={"X-Hire-Demo-Payment": "1"}, json={})
    assert duplicate.status_code == 409
    assert duplicate.get_json()["code"] == "DUPLICATE_PAYMENT"


def test_world_denial_prevents_protected_action(client, hiring_fixture, monkeypatch):
    response = _create(client)
    intent = response.get_json()
    assert client.post(
        f"/api/hiring/approvals/{intent['approval']['approvalId']}/decision",
        json={"ownerId": "world-user-1", "state": "denied", "reason": "owner declined"},
    ).status_code == 200
    monkeypatch.setattr("app.hiring.routes.generate_test_plan",
                        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("ran")))
    work = client.post(f"/api/hiring/intents/{intent['id']}/work",
                       headers={"X-Hire-Demo-Payment": "1"}, json={})
    assert work.status_code == 403
    assert work.get_json()["code"] == "APPROVAL_REQUIRED"


def test_risky_destination_is_denied_before_payment(client, hiring_fixture, monkeypatch):
    monkeypatch.setenv("INTERCEPTA_DENY_ADDRESS", SPECIALIST_ADDRESS)
    monkeypatch.setattr("app.hiring.routes.generate_test_plan",
                        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("ran")))
    response = _create(client)
    assert response.status_code == 409
    body = response.get_json()
    assert body["status"] == "denied"
    assert body["screening"]["decision"] == "DENY"


def test_changed_ens_snapshot_holds_after_approval(client, hiring_fixture, monkeypatch):
    response = _create(client)
    intent = response.get_json()
    assert _approve(client, intent).status_code == 200
    fixtures = json.loads(__import__("os").environ["ENS_FIXTURES_JSON"])
    fixtures[SPECIALIST]["address"] = "0x" + "b" * 40
    monkeypatch.setenv("ENS_FIXTURES_JSON", json.dumps(fixtures))
    monkeypatch.setattr("app.hiring.routes.generate_test_plan",
                        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("ran")))
    work = client.post(f"/api/hiring/intents/{intent['id']}/work",
                       headers={"X-Hire-Demo-Payment": "1"}, json={})
    assert work.status_code == 409
    assert work.get_json()["code"] == "ENS_CHANGED"


def test_legacy_work_and_payment_paths_are_closed(client, agent):
    assert client.post(f"/api/agents/{agent}/generate", json={"prompt": "hello"}).status_code == 410
    assert client.post("/api/x402/pay", json={}).status_code == 410


def test_protected_hiring_is_visible_in_the_browser_entry_points(client, hiring_fixture):
    landing = client.get("/")
    assert landing.status_code == 200
    assert b"Protected named-agent purchase" in landing.data
    assert b"ENSv2" in landing.data
    assert b"x402" in landing.data

    demo = client.get("/hire")
    assert demo.status_code == 200
    assert b"Approve exact purchase" in demo.data
    assert b"Intercepta" in demo.data

    logo = client.get("/static/img/agents-list-mark.png")
    assert logo.status_code == 200
    assert logo.mimetype == "image/png"

    css = client.get("/static/css/main.css")
    assert css.status_code == 200
    assert b"#FAFAF9" in css.data
    assert b"#4F46E5" in css.data
