import json


def test_ens_name_endpoint_returns_live_authorization_records(client, monkeypatch):
    monkeypatch.setenv("ENS_MODE", "fixture")
    monkeypatch.setenv("ENS_FIXTURES_JSON", json.dumps({
        "qa.example.eth": {
            "address": "0x" + "a" * 40,
            "endpoint": "https://qa.example.test/work",
            "enabled": True,
            "status": "online",
            "resolver": "0x" + "c" * 40,
        },
    }))
    response = client.get("/api/names/resolve?name=qa.example.eth")
    assert response.status_code == 200
    body = response.get_json()
    assert body["records"]["address"] == "0x" + "a" * 40
    assert body["records"]["endpoint"] == "https://qa.example.test/work"


def test_ens_name_endpoint_fails_closed_on_unknown_name(client, monkeypatch):
    monkeypatch.setenv("ENS_MODE", "fixture")
    monkeypatch.setenv("ENS_FIXTURES_JSON", "{}")
    response = client.get("/api/names/resolve?name=missing.example.eth")
    assert response.status_code == 409
    assert response.get_json()["code"] == "ENS_RESOLUTION_FAILED"
