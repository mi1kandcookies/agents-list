"""Agent name labels: chosen when listing, checked live, used when the name
is issued, and shown on the operator's manifest page. Sidecar faked."""
from __future__ import annotations

import pytest

from app.names import service
from app.names.client import SidecarClient
from tests.conftest import WALLET
from tests.test_names import ROOT, FakeSidecar

LISTING = {"name": "CodeReview Pro", "description": "Reviews code", "category": "Development",
           "billing": "per_token", "wallet": WALLET}


@pytest.fixture()
def sidecar(app):
    fake = FakeSidecar()
    app.extensions["names_client"] = fake
    yield fake
    app.extensions.pop("names_client", None)


def _agent(db, agent_id):
    from app.models import Agent
    db.session.expire_all()
    return db.session.get(Agent, agent_id)


def _login(client, human):
    with client.session_transaction() as s:
        s["human_id"] = human.id
        s["human_sub"] = human.world_sub


# ── validation ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("label, code", [
    ("", "LABEL_REQUIRED"),
    ("a" * 41, "LABEL_TOO_LONG"),
    ("-lead", "LABEL_INVALID"),
    ("trail-", "LABEL_INVALID"),
    ("under_score", "LABEL_INVALID"),
    ("dot.ted", "LABEL_INVALID"),
    ("émoji", "LABEL_INVALID"),
    ("xn--abc", "LABEL_INVALID"),        # ENSIP-15: no "--" in positions 3 and 4
    ("admin", "LABEL_RESERVED"),
    ("agentslist-app", "LABEL_RESERVED"),
])
def test_invalid_or_reserved_labels(app, label, code):
    result = service.check_label(label)
    assert result["available"] is False and result["code"] == code and result["error"]


def test_valid_label_is_normalized(app):
    result = service.check_label("  My-Agent-2 ")
    assert result == {"label": "my-agent-2", "name": f"my-agent-2.{ROOT}", "root": ROOT,
                      "available": True}
    assert service.check_label("a" * 40)["available"]


def test_label_taken_by_issued_name_or_other_listing(db, agent, sidecar):
    row = _agent(db, agent)
    row.payout_address = WALLET
    service.on_agent_published(row)                       # issues test-agent.<root>
    assert service.check_label("test-agent")["code"] == "LABEL_TAKEN"
    assert service.check_label("test-agent", agent)["available"]   # its own name
    row.ens_label = "claimed"
    db.session.commit()
    assert service.check_label("claimed")["code"] == "LABEL_TAKEN"
    assert service.check_label("claimed", agent)["available"]


# ── availability endpoint ──────────────────────────────────────────────────

def test_available_endpoint(client, db, agent):
    ok = client.get("/api/names/available", query_string={"label": "Fresh-Name"})
    assert ok.status_code == 200
    assert ok.get_json() == {"label": "fresh-name", "name": f"fresh-name.{ROOT}", "root": ROOT,
                             "available": True}
    bad = client.get("/api/names/available", query_string={"label": "www"}).get_json()
    assert bad["available"] is False and bad["code"] == "LABEL_RESERVED"
    assert client.get("/api/names/available").status_code == 400
    _agent(db, agent).ens_label = "mine"
    db.session.commit()
    assert not client.get("/api/names/available?label=mine").get_json()["available"]
    assert client.get(f"/api/names/available?label=mine&agent_id={agent}").get_json()["available"]


def test_available_endpoint_same_origin_or_token(client, monkeypatch):
    monkeypatch.setenv("MCP_API_TOKEN", "s3cret")
    url = "/api/names/available?label=fresh"
    assert client.get(url).status_code == 401
    assert client.get(url, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 401
    assert client.get(url, headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200
    assert client.get(url, headers={"Authorization": "Bearer s3cret"}).status_code == 200


# ── listing form ───────────────────────────────────────────────────────────

def test_create_form_shows_name_field_and_root(client):
    html = client.get("/seller/create").get_data(as_text=True)
    assert 'id="create-ens-label"' in html and 'id="ens-preview-name"' in html
    assert f".{ROOT}" in html and "/api/names/available" in html


def test_create_stores_chosen_label(client, db):
    resp = client.post("/seller/create", json={**LISTING, "ens_label": "Reviewer-1"})
    assert resp.status_code == 201
    assert _agent(db, resp.get_json()["agentId"]).ens_label == "reviewer-1"


def test_create_defaults_label_to_slug(client, db):
    resp = client.post("/seller/create", json=LISTING)
    assert _agent(db, resp.get_json()["agentId"]).ens_label == "codereview-pro"
    # A second listing with the same name gets no claimed label (suffixed at publish).
    again = client.post("/seller/create", json=LISTING)
    assert _agent(db, again.get_json()["agentId"]).ens_label is None


@pytest.mark.parametrize("label, code", [("bad_label", "LABEL_INVALID"), ("api", "LABEL_RESERVED")])
def test_create_rejects_bad_label(client, db, label, code):
    from app.models import Agent
    resp = client.post("/seller/create", json={**LISTING, "ens_label": label})
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["field"] == "ens_label" and body["code"] == code
    assert Agent.query.count() == 0


def test_create_rejects_taken_label(client, db):
    client.post("/seller/create", json={**LISTING, "ens_label": "taken"})
    resp = client.post("/seller/create", json={**LISTING, "name": "Other", "ens_label": "taken"})
    assert resp.status_code == 400 and resp.get_json()["code"] == "LABEL_TAKEN"


# ── issuing ────────────────────────────────────────────────────────────────

def test_published_name_uses_chosen_label(db, agent, sidecar):
    row = _agent(db, agent)
    row.payout_address, row.ens_label = WALLET, "picked"
    name = service.on_agent_published(row)
    assert name.name == f"picked.{ROOT}" and name.status == "active"
    assert sidecar.calls[0][1]["label"] == "picked"
    assert row.ens_name == f"picked.{ROOT}"


def test_published_name_falls_back_to_slug(db, agent, sidecar):
    row = _agent(db, agent)
    row.payout_address = WALLET
    assert service.on_agent_published(row).name == f"test-agent.{ROOT}"


# ── manifest page ──────────────────────────────────────────────────────────

def test_manifest_page_sets_label_before_stamp(client, db, agent, human):
    _login(client, human)
    html = client.get(f"/seller/agents/{agent}/manifest").get_data(as_text=True)
    assert 'data-status="unissued"' in html and f"test-agent.{ROOT}" in html
    assert "after you stamp the manifest" in html and 'id="mf-ens-label"' in html
    resp = client.post(f"/seller/agents/{agent}/manifest", data={"action": "ens_label", "ens_label": "renamed"})
    assert resp.status_code == 302 and "saved=name" in resp.headers["Location"]
    assert _agent(db, agent).ens_label == "renamed"
    bad = client.post(f"/seller/agents/{agent}/manifest", data={"action": "ens_label", "ens_label": "no way"})
    assert bad.status_code == 400
    page = bad.get_data(as_text=True)
    assert "Agent name:" in page and 'value="no way"' in page
    assert _agent(db, agent).ens_label == "renamed"


def test_manifest_page_pending_name_can_be_renamed(app, client, db, agent, human):
    _login(client, human)
    app.extensions["names_client"] = SidecarClient("", "")    # sidecar not running
    row = _agent(db, agent)
    row.payout_address = WALLET
    service.on_agent_published(row)
    html = client.get(f"/seller/agents/{agent}/manifest").get_data(as_text=True)
    assert 'data-status="pending"' in html and "Not on Sepolia yet" in html
    assert 'value="retry_name"' in html and 'id="mf-ens-label"' in html
    client.post(f"/seller/agents/{agent}/manifest", data={"action": "ens_label", "ens_label": "second"})
    from app.models import EnsName
    assert db.session.get(EnsName, f"test-agent.{ROOT}") is None
    assert service.agent_name_view(_agent(db, agent))["name"] == f"second.{ROOT}"


def test_manifest_page_active_name_is_read_only(client, db, agent, human, sidecar):
    _login(client, human)
    row = _agent(db, agent)
    row.payout_address = WALLET
    issued = service.on_agent_published(row)
    html = client.get(f"/seller/agents/{agent}/manifest").get_data(as_text=True)
    assert 'data-status="active"' in html and 'id="mf-ens-label"' not in html
    assert f"https://sepolia.app.ens.domains/{issued.name}" in html
    assert f"https://sepolia.etherscan.io/tx/{issued.tx_hashes[-1]}" in html
    resp = client.post(f"/seller/agents/{agent}/manifest", data={"action": "ens_label", "ens_label": "other"})
    assert resp.status_code == 400 and "cannot be renamed" in resp.get_data(as_text=True)


def test_manifest_page_failed_name_retry(client, db, agent, human, sidecar):
    from app.names.client import SidecarError
    _login(client, human)
    row = _agent(db, agent)
    row.payout_address = WALLET
    sidecar.fail["agent"] = SidecarError("boom")
    assert service.on_agent_published(row).status == "failed"
    html = client.get(f"/seller/agents/{agent}/manifest").get_data(as_text=True)
    assert 'data-status="failed"' in html and 'value="retry_name"' in html
    resp = client.post(f"/seller/agents/{agent}/manifest", data={"action": "retry_name"})
    assert resp.status_code == 302
    assert service.agent_name_view(_agent(db, agent))["status"] == "active"


# ── public profile ─────────────────────────────────────────────────────────

def test_profile_shows_name_only_when_active(app, client, db, agent, sidecar):
    row = _agent(db, agent)
    row.payout_address, row.ens_label = WALLET, "shown"
    db.session.commit()
    assert f"shown.{ROOT}" not in client.get(f"/agent/{agent}").get_data(as_text=True)
    service.on_agent_published(row)
    html = client.get(f"/agent/{agent}").get_data(as_text=True)
    assert html.count(f"shown.{ROOT}") == 1
