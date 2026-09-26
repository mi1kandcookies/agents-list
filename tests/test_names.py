"""Names: hooks, retry, tree API and page, with the sidecar faked."""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest
import requests

from app.names import service
from app.names.client import SidecarClient, SidecarError

PAYOUT = "0x" + "c" * 40
ROOT = "agentslist-app.eth"


class FakeSidecar:
    configured = True

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.fail: dict[str, Exception] = {}

    def _call(self, method: str, **body) -> dict:
        self.calls.append((method, body))
        if method in self.fail:
            raise self.fail.pop(method)
        digest = hashlib.sha256(f"{len(self.calls)}:{method}".encode()).hexdigest()
        return {"status": "revoked" if method == "revoke" else "active", "owner": "0xop",
                "txs": [{"step": method, "hash": "0x" + digest}]}

    def create_agent(self, **kw):
        return self._call("agent", **kw)

    def create_job(self, **kw):
        return self._call("job", **kw)

    def create_subjob(self, **kw):
        return self._call("subjob", **kw)

    def revoke(self, name, grantee=None):
        return self._call("revoke", name=name, grantee=grantee)

    def methods(self):
        return [m for m, _ in self.calls]


@pytest.fixture()
def sidecar(app):
    fake = FakeSidecar()
    app.extensions["names_client"] = fake
    yield fake
    app.extensions.pop("names_client", None)


@pytest.fixture()
def agent_row(db, agent):
    from app.models import Agent
    row = db.session.get(Agent, agent)
    row.payout_address = PAYOUT
    db.session.commit()
    return row


def _engagement(db, agent_row, *, parent=None, deadline_days=7):
    from app.models import Engagement
    eng = Engagement(agent_id=agent_row.id, outcome="work", sow_hash="0x" + "ab" * 32,
                     mandate_id="MND-TEST", status="funded",
                     parent_engagement_id=parent.id if parent else None,
                     deadline_at=datetime.now(timezone.utc) + timedelta(days=deadline_days))
    db.session.add(eng)
    db.session.commit()
    return eng


# ── Agent names ──────────────────────────────────────────────────────────────

def test_agent_published(db, agent_row, sidecar):
    row = service.on_agent_published(agent_row)
    assert row.name == f"test-agent.{ROOT}"
    assert row.status == "active" and row.kind == "agent" and row.parent_name == ROOT
    assert row.records == {"context": "Does tests"}
    assert row.node.startswith("0x") and len(row.node) == 66
    assert len(row.tx_hashes) == 1
    assert agent_row.ens_name == row.name
    method, body = sidecar.calls[0]
    assert method == "agent" and body["label"] == "test-agent"
    assert body["agent_public_id"] == agent_row.public_id
    assert db.session.get(type(row), ROOT).status == "active"
    # Publishing again reuses the active name.
    assert service.on_agent_published(agent_row).name == row.name
    assert sidecar.methods() == ["agent"]


def test_agent_records_from_manifest(db, agent_row, sidecar):
    agent_row.manifest_json = '{"endpoints": {"mcp": "https://a.example/mcp"}, "erc8004_agent_id": "12"}'
    row = service.on_agent_published(agent_row)
    assert row.records["mcp"] == "https://a.example/mcp"
    assert row.records["erc8004_agent_id"] == "12"


def test_unconfigured_sidecar_leaves_pending(db, agent_row, app, client):
    app.extensions["names_client"] = SidecarClient("", "")
    row = service.on_agent_published(agent_row)
    assert row.status == "pending" and agent_row.ens_name is None
    fake = FakeSidecar()
    app.extensions["names_client"] = fake
    resp = client.post(f"/api/names/{row.name}/retry")
    assert resp.status_code == 200 and resp.get_json()["status"] == "active"


def test_sidecar_failure_is_not_fatal(db, agent_row, sidecar, client):
    sidecar.fail["agent"] = SidecarError("down", "UNREACHABLE")
    row = service.on_agent_published(agent_row)
    assert row.status == "failed"
    resp = client.post(f"/api/names/{row.name}/retry")
    assert resp.get_json()["status"] == "active"


def test_unexpected_error_never_raises(db, agent_row, sidecar):
    sidecar.fail["agent"] = RuntimeError("boom")
    assert service.on_agent_published(agent_row) is None


# ── Job and sub-job names ────────────────────────────────────────────────────

def test_engagement_funded_issues_job_name(db, agent_row, sidecar):
    eng = _engagement(db, agent_row)
    row = service.on_engagement_funded(eng)
    assert sidecar.methods() == ["agent", "job"]          # agent name issued first
    assert row.name == f"{eng.id.lower()}.test-agent.{ROOT}"
    assert row.status == "active" and row.kind == "job" and row.engagement_id == eng.id
    assert eng.ens_name == row.name
    _, body = sidecar.calls[1]
    assert body["parent"] == f"test-agent.{ROOT}"
    assert body["expiry"] == int(eng.deadline_at.replace(tzinfo=timezone.utc).timestamp())  # naive UTC
    assert body["grantee"] == PAYOUT
    assert body["records"] == {"sow_hash": eng.sow_hash, "mandate": "MND-TEST", "status": "funded"}


def test_job_waits_for_agent_name_then_retries_both(db, agent_row, sidecar, client):
    sidecar.fail["agent"] = SidecarError("down")
    eng = _engagement(db, agent_row)
    row = service.on_engagement_funded(eng)
    assert row.status == "pending"
    assert sidecar.methods() == ["agent"]
    resp = client.post(f"/api/names/{row.name}/retry")
    assert resp.get_json()["status"] == "active"
    assert sidecar.methods() == ["agent", "agent", "job"]


def test_subhire_issues_wildcard_subjob(db, agent_row, sidecar):
    parent = _engagement(db, agent_row, deadline_days=3)
    job = service.on_engagement_funded(parent)
    child = _engagement(db, agent_row, parent=parent, deadline_days=30)
    row = service.on_subhire(child)
    assert row.name == f"{child.id.lower()}.{job.name}"
    assert row.kind == "subjob" and row.status == "active"
    _, body = sidecar.calls[-1]
    assert body["parent"] == job.name
    assert body["expiry"] == job.expiry                   # capped at the job's expiry
    assert "grantee" not in body


def test_subhire_without_parent_job_is_skipped(db, agent_row, sidecar):
    eng = _engagement(db, agent_row)
    assert service.on_subhire(eng) is None
    assert sidecar.calls == []


# ── Settlement ───────────────────────────────────────────────────────────────

def test_settled_revokes_job_and_subtree(db, agent_row, sidecar):
    parent = _engagement(db, agent_row)
    job = service.on_engagement_funded(parent)
    sub = service.on_subhire(_engagement(db, agent_row, parent=parent))
    row = service.on_settled(parent)
    assert row.status == "revoked"
    assert sidecar.calls[-1] == ("revoke", {"name": job.name, "grantee": PAYOUT})
    assert sub.status == "revoked"


def test_settle_failure_then_retry_revokes(db, agent_row, sidecar, client):
    eng = _engagement(db, agent_row)
    job = service.on_engagement_funded(eng)
    sidecar.fail["revoke"] = SidecarError("rpc", "CHAIN_ERROR", 502)
    assert service.on_settled(eng).status == "failed"
    resp = client.post(f"/api/names/{job.name}/retry")
    assert resp.get_json()["status"] == "revoked"
    assert sidecar.methods()[-2:] == ["revoke", "revoke"]


def test_settling_a_name_that_never_reached_the_chain(db, agent_row, app):
    app.extensions["names_client"] = SidecarClient("", "")
    eng = _engagement(db, agent_row)
    service.on_engagement_funded(eng)
    assert service.on_settled(eng).status == "revoked"


# ── Routes ───────────────────────────────────────────────────────────────────

def test_tree_api_and_page(db, agent_row, sidecar, client):
    eng = _engagement(db, agent_row)
    job = service.on_engagement_funded(eng)
    tree = client.get("/api/names/tree").get_json()
    assert tree["name"] == ROOT and tree["kind"] == "root"
    agent_node = tree["children"][0]
    assert agent_node["name"] == f"test-agent.{ROOT}"
    assert agent_node["children"][0]["name"] == job.name
    assert agent_node["children"][0]["expires_at"]
    sub = client.get(f"/api/names/tree?root=test-agent.{ROOT}").get_json()
    assert sub["name"] == f"test-agent.{ROOT}"
    page = client.get("/names")
    assert page.status_code == 200
    assert job.name.encode() in page.data


def test_tree_page_without_names(client):
    assert client.get("/names").status_code == 200
    assert client.get("/api/names/tree").get_json()["children"] == []


def test_retry_unknown_and_auth(client, app):
    assert client.post("/api/names/nope.eth/retry").status_code == 404
    app.config["API_KEY"] = "k"
    assert client.post("/api/names/nope.eth/retry").status_code == 401
    assert client.post("/api/names/nope.eth/retry", headers={"X-Api-Key": "k"}).status_code == 404


# ── Client ───────────────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.ok = 200 <= status < 300

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def test_client_sends_token_and_maps_errors(monkeypatch):
    c = SidecarClient("http://127.0.0.1:8787/", "tok")
    seen = {}

    def fake_request(method, url, **kw):
        seen.update(method=method, url=url, headers=kw["headers"], json=kw["json"])
        return _Resp(200, {"name": "x", "txs": []})

    monkeypatch.setattr(c.session, "request", fake_request)
    c.revoke("a.agentslist-app.eth")
    assert seen["url"] == "http://127.0.0.1:8787/names/revoke"
    assert seen["headers"] == {"X-Sidecar-Token": "tok"}

    monkeypatch.setattr(c.session, "request", lambda *a, **k: _Resp(409, {"error": "taken", "code": "NAME_TAKEN"}))
    with pytest.raises(SidecarError) as err:
        c.create_agent(agent_public_id="AGT-1", label="a", records={})
    assert (err.value.code, err.value.status) == ("NAME_TAKEN", 409)

    monkeypatch.setattr(c.session, "request", lambda *a, **k: _Resp(502, ValueError("html")))
    with pytest.raises(SidecarError, match="HTTP 502"):
        c.health()

    def boom(*a, **k):
        raise requests.ConnectionError("refused")
    monkeypatch.setattr(c.session, "request", boom)
    with pytest.raises(SidecarError) as err:
        c.tree()
    assert err.value.code == "UNREACHABLE"

    with pytest.raises(SidecarError) as err:
        SidecarClient(None, "tok").health()
    assert err.value.code == "NOT_CONFIGURED"
