"""Screening a wallet before it can list an agent (app.screening.service.
screen_listing_wallet), and the /seller/create route that gates on it."""
from __future__ import annotations

import json
from pathlib import Path

from app.screening.intercepta import QUICK_SCAN_PATH, InterceptaClient
from app.screening.service import screen_listing_wallet
from tests.fakes.intercepta import FakeInterceptaHttp

FIXTURES = Path(__file__).parent / "fixtures" / "screening"
WALLET = "0x" + "c3" * 20


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def client_with(http):
    return InterceptaClient(api_key="test-key", base_url="https://intercepta.test",
                            http=http, timeout=4)


def test_clean_wallet_is_allowed():
    http = FakeInterceptaHttp()
    http.route("GET", QUICK_SCAN_PATH.format(address=WALLET), body=fixture("quick_scan_clean"))
    assert screen_listing_wallet(WALLET, client=client_with(http)) == {"ok": True}


def test_sanctioned_wallet_is_refused_with_a_specific_message():
    http = FakeInterceptaHttp()
    http.route("GET", QUICK_SCAN_PATH.format(address=WALLET), body=fixture("quick_scan_sanctioned"))
    result = screen_listing_wallet(WALLET, client=client_with(http))
    assert result["ok"] is False
    assert result["code"] == "LISTING_WALLET_REFUSED"
    assert "sanctioned" in result["message"].lower()
    assert any(r["code"] == "TRAIT_SANCTION_ADDRESS" for r in result["reasons"])


def test_known_scammer_wallet_is_refused_with_a_generic_message():
    """A REFUSE for a non-sanctions reason gets generic wording, not the
    sanctions-specific one."""
    http = FakeInterceptaHttp()
    http.route("GET", QUICK_SCAN_PATH.format(address=WALLET), body=fixture("quick_scan_known_scammer"))
    result = screen_listing_wallet(WALLET, client=client_with(http))
    assert result["ok"] is False
    assert "sanctioned" not in result["message"].lower()


def test_provider_error_fails_closed():
    http = FakeInterceptaHttp()   # no route registered -> 404 from the fake transport
    result = screen_listing_wallet(WALLET, client=client_with(http))
    assert result["ok"] is False
    assert result["code"] != "ok"


def test_seller_create_route_blocks_a_sanctioned_wallet(client, db, monkeypatch):
    import app.screening.service as service_module

    http = FakeInterceptaHttp()
    http.route("GET", QUICK_SCAN_PATH.format(address=WALLET), body=fixture("quick_scan_sanctioned"))
    monkeypatch.setattr(service_module, "InterceptaClient", lambda **kw: client_with(http))

    resp = client.post("/seller/create", json={
        "wallet": WALLET, "name": "Test Agent", "description": "x",
        "category": "Development", "billing": "per_token",
    })
    assert resp.status_code == 403
    body = resp.get_json()
    assert body["code"] == "LISTING_WALLET_REFUSED"
    assert "sanctioned" in body["error"].lower()

    from app.models import Agent
    assert Agent.query.filter_by(name="Test Agent").first() is None


def test_seller_create_route_allows_a_clean_wallet(client, db, monkeypatch):
    import app.screening.service as service_module

    http = FakeInterceptaHttp()
    http.route("GET", QUICK_SCAN_PATH.format(address=WALLET), body=fixture("quick_scan_clean"))
    monkeypatch.setattr(service_module, "InterceptaClient", lambda **kw: client_with(http))

    resp = client.post("/seller/create", json={
        "wallet": WALLET, "name": "Test Agent", "description": "x",
        "category": "Development", "billing": "per_token",
    })
    assert resp.status_code == 201, resp.get_json()

    from app.models import Agent
    assert Agent.query.filter_by(name="Test Agent").first() is not None
