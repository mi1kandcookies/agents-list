"""Sub-hire chain: a hired agent hires other agents under its mandate
(docs/decisions/0001-custody-chain.md §3, §4, §5, §7).

Runs the real approval, mandate and engagement services against the fake
World ID provider and the fake screener, like tests/test_engagements.py."""
from __future__ import annotations

import base64
import json
import time
from datetime import datetime, timezone

import pytest

from app.approvals import service as approvals
from app.extensions import db as _db
from app.mandates import service as mandates
from app.mandates import tokens
from tests.conftest import WALLET

ROOT_PAYEE = WALLET.lower()
B_ADDR = "0x" + "1b" * 20
C_ADDR = "0x" + "1c" * 20
D_ADDR = "0x" + "1d" * 20
OTHER_SUB = "0x" + "7" * 64


@pytest.fixture(autouse=True)
def _mandate_key(app):
    app.config["MANDATE_SIGNING_KEY"] = tokens.generate_pem()


@pytest.fixture()
def screener(app, fake_screener):
    app.extensions["screener"] = fake_screener
    return fake_screener


@pytest.fixture()
def approve(world_idp):
    from app.models import Approval

    def _approve(approval_id, **claims):
        row = _db.session.get(Approval, approval_id)
        if row.state == "created":
            approvals.start_device(row)
        world_idp.approve_device(row.device_code, **claims)
        approvals.poll(row)
        _db.session.expire_all()
        return _db.session.get(Approval, approval_id)
    return _approve


@pytest.fixture()
def named(monkeypatch):
    """Record on_subhire calls (the real hook is exercised in test_names)."""
    from app.names import service as names
    calls = []
    monkeypatch.setattr(names, "on_subhire", lambda child: calls.append(child.id))
    return calls


def _new_agent(db, name, addr, category="Development", stamped=True):
    """A listed agent, operator-stamped (dev stamp) so it is hireable."""
    from app.models import Agent
    from app.seller.stamp import dev_stamp
    row = Agent(name=name, description="sub agent", category=category, billing="per_minute",
                min_price=0.05, max_price=0.2, current_price=0.1, seller=addr,
                deployer_wallet=addr, payout_address=addr)
    row.tags = []
    row.capabilities = []
    db.session.add(row)
    db.session.commit()
    if stamped:
        dev_stamp(row)
        db.session.commit()
    return row.public_id


@pytest.fixture()
def agents(db, agent):
    from app.models import Agent
    from app.seller.stamp import dev_stamp
    root_agent = db.session.get(Agent, agent)
    dev_stamp(root_agent)
    db.session.commit()
    return {"A": root_agent.public_id,
            "B": _new_agent(db, "Agent B", B_ADDR),
            "C": _new_agent(db, "Agent C", C_ADDR),
            "D": _new_agent(db, "Agent D", D_ADDR)}


@pytest.fixture()
def root(client, approve, screener, agents, human):
    """A funded 25 USDC job for agent A: ``(engagement_id, root mandate token)``."""
    from app.models import Engagement, Mandate
    resp = client.post("/api/engagements", json={
        "agent_id": agents["A"], "outcome": "Ship the reporting feature", "budget_usdc": "25"})
    eid = resp.get_json()["engagement_id"]
    apr = client.post(f"/api/engagements/{eid}/hire",
                      json={"flow": "device", "confirm_amount_usdc": "25"}).get_json()
    assert approve(apr["approval_id"]).state == "consumed"
    eng = _db.session.get(Engagement, eid)
    return eid, _db.session.get(Mandate, eng.mandate_id).token


def _subhire(client, eid, token, agent_id, budget="10", category="Development", **extra):
    return client.post(f"/api/engagements/{eid}/subhire",
                       headers={"Authorization": f"Mandate {token}"} if token else {},
                       json={"agent_id": agent_id, "outcome": "Write the export tests",
                             "budget_usdc": budget, "category": category, **extra})


def _mandate(mandate_id):
    from app.models import Mandate
    _db.session.expire_all()
    return _db.session.get(Mandate, mandate_id)


def _children(eid):
    from app.models import Engagement
    return Engagement.query.filter_by(parent_engagement_id=eid).all()


def _allocs():
    from app.models import LedgerEntry
    return LedgerEntry.query.filter_by(kind="subhire_alloc").all()


# ── root mandate at consume time ──────────────────────────────────────────
def test_root_mandate_is_minted_when_funding_commits(client, root, agents, human):
    from app.models import Engagement
    eid, token = root
    eng = _db.session.get(Engagement, eid)   # no GET /api/engagements/<id> happened
    row = _mandate(eng.mandate_id)
    assert row.depth == 0 and row.budget_micro == 25_000_000 and row.human_id == human.id
    assert tokens.decode(token)["sub"] == agents["A"]


def test_mandate_token_only_for_api_token_callers(client, root, monkeypatch):
    eid, token = root
    assert "mandate_token" not in client.get(f"/api/engagements/{eid}").get_json()
    monkeypatch.setenv("MCP_API_TOKEN", "s3cret")
    assert client.get(f"/api/engagements/{eid}").status_code == 401
    body = client.get(f"/api/engagements/{eid}",
                      headers={"Authorization": "Bearer s3cret"}).get_json()
    assert body["mandate_token"] == token
    assert token not in client.get(f"/jobs/{eid}").get_data(as_text=True)


# ── PAY ───────────────────────────────────────────────────────────────────
def test_pay_creates_child_ledger_and_spend(client, root, agents, screener, named):
    eid, token = root
    resp = _subhire(client, eid, token, agents["B"])
    assert resp.status_code == 201, resp.get_json()
    child = resp.get_json()
    assert child["parent_engagement_id"] == eid and child["depth"] == 1
    assert child["status"] == "funded" and child["agent_id"] == agents["B"]
    assert child["total_micro"] == child["allocated_micro"] == 10_000_000
    assert child["screening"]["verdict"] == "PAY" and not child["capped"]
    assert screener.calls[-1]["hop"] == "subhire.hop"
    assert screener.calls[-1]["chain_address"] == B_ADDR

    [alloc] = child["ledger"]
    assert alloc["kind"] == "subhire_alloc" and alloc["amount_micro"] == 10_000_000
    assert alloc["screening_id"] == child["screening"]["id"] and alloc["approval_id"] is None
    assert {m["status"] for m in child["milestones"]} == {"funded"}

    claims = mandates.verify_chain(child["mandate_token"])
    kid = _mandate(claims["jti"])
    root_row = _mandate(tokens.decode(token)["jti"])
    assert (kid.parent_id, kid.depth, kid.budget_micro, kid.categories) == \
        (root_row.id, 1, 10_000_000, ["Development"])
    assert kid.grantee_agent_public_id == agents["B"] and kid.human_id == root_row.human_id
    assert root_row.spent_micro == 10_000_000
    # counted once: spent by the parent, not also reserved by the child
    assert mandates.remaining_micro(root_row) == 15_000_000
    assert named == [child["engagement_id"]]
    # the sub-job is not hireable (funded from the parent, not the vault)
    hire = client.post(f"/api/engagements/{child['engagement_id']}/hire",
                       json={"flow": "device", "confirm_amount_usdc": "10"})
    assert hire.status_code == 409


def test_names_hook_failure_is_not_fatal(client, root, agents, screener, monkeypatch):
    from app.names import service as names

    def boom(child):
        raise RuntimeError("sidecar down")
    monkeypatch.setattr(names, "on_subhire", boom)
    eid, token = root
    assert _subhire(client, eid, token, agents["B"]).status_code == 201
    assert len(_allocs()) == 1


def test_cap_allocates_up_to_the_cap(client, root, agents, screener, named):
    eid, token = root
    screener.cap_micro = 4_000_000
    screener.set(B_ADDR, "CAP")
    body = _subhire(client, eid, token, agents["B"]).get_json()
    assert body["capped"] and body["cap_micro"] == 4_000_000
    assert body["requested_micro"] == 10_000_000 and body["allocated_micro"] == 4_000_000
    assert body["total_micro"] == 4_000_000 and body["screening"]["verdict"] == "CAP"
    assert [a.amount_micro for a in _allocs()] == [4_000_000]
    assert _mandate(tokens.decode(token)["jti"]).spent_micro == 4_000_000


# ── mandate limits ────────────────────────────────────────────────────────
def test_over_budget_is_refused(client, root, agents, screener):
    eid, token = root
    resp = _subhire(client, eid, token, agents["B"], budget="25.01")
    assert resp.status_code == 403 and resp.get_json()["code"] == "MANDATE_EXCEEDED"
    assert _subhire(client, eid, token, agents["B"], budget="20").status_code == 201
    resp = _subhire(client, eid, token, agents["C"], budget="5.01")
    assert resp.status_code == 403 and resp.get_json()["code"] == "MANDATE_EXCEEDED"
    assert len(_children(eid)) == 1 and len(_allocs()) == 1


def test_category_outside_parent_is_refused(client, root, agents, screener):
    eid, token = root
    calls = len(screener.calls)
    resp = _subhire(client, eid, token, agents["B"], category="Design")
    assert resp.status_code == 403 and resp.get_json()["code"] == "CATEGORY_NOT_ALLOWED"
    assert not _children(eid) and not _allocs() and len(screener.calls) == calls


def test_each_hop_narrows_and_depth_is_capped(client, root, agents, screener):
    eid, token = root                                   # MANDATE_MAX_DEPTH defaults to 2
    b = _subhire(client, eid, token, agents["B"], budget="10").get_json()
    # B cannot hand C more than B holds
    over = _subhire(client, b["engagement_id"], b["mandate_token"], agents["C"], budget="10.01")
    assert over.status_code == 403 and over.get_json()["code"] == "MANDATE_EXCEEDED"
    c = _subhire(client, b["engagement_id"], b["mandate_token"], agents["C"], budget="4").get_json()
    assert c["depth"] == 2 and c["parent_engagement_id"] == b["engagement_id"]
    assert _mandate(tokens.decode(b["mandate_token"])["jti"]).spent_micro == 4_000_000
    resp = _subhire(client, c["engagement_id"], c["mandate_token"], agents["D"], budget="1")
    assert resp.status_code == 403 and resp.get_json()["code"] == "DEPTH_EXCEEDED"
    assert len(_allocs()) == 2


def test_unstamped_agent_cannot_be_subhired(client, root, agents, screener, db):
    eid, token = root
    unstamped = _new_agent(db, "Agent E", "0x" + "1e" * 20, stamped=False)
    calls = len(screener.calls)
    resp = _subhire(client, eid, token, unstamped)
    assert resp.status_code == 409 and resp.get_json()["code"] == "NOT_STAMPED"
    assert not _children(eid) and not _allocs() and len(screener.calls) == calls


def test_agent_cannot_subhire_itself(client, root, agents, screener):
    eid, token = root
    resp = _subhire(client, eid, token, agents["A"])
    assert resp.status_code == 400 and resp.get_json()["field"] == "agent_id"


# ── screening ─────────────────────────────────────────────────────────────
def test_refuse_blocks_with_no_allocation(client, root, agents, screener, named):
    eid, token = root
    screener.set(B_ADDR, "REFUSE")
    resp = _subhire(client, eid, token, agents["B"])
    assert resp.status_code == 403
    body = resp.get_json()
    assert body["code"] == "SCREENING_REFUSED" and body["screening"]["verdict"] == "REFUSE"
    assert body["screening"]["reasons"][0]["code"] == "FAKE_REFUSE"
    assert not _children(eid) and not _allocs() and named == []
    assert _mandate(tokens.decode(token)["jti"]).spent_micro == 0
    assert client.get(f"/api/engagements/{eid}").get_json()["status"] == "funded"


def test_forged_screening_in_body_is_ignored(client, root, agents, screener):
    eid, token = root
    screener.set(B_ADDR, "REFUSE")
    forged = {"id": "SCR-000000000000", "verdict": "PAY", "fail_closed": False}
    resp = _subhire(client, eid, token, agents["B"], screening=forged,
                    screening_id=forged["id"], verdict="PAY")
    assert resp.status_code == 403 and resp.get_json()["code"] == "SCREENING_REFUSED"
    assert not _allocs()


def test_ask_human_asks_the_root_human_and_approval_allocates(client, root, agents, screener,
                                                              approve, human, named):
    from app.models import Approval
    eid, token = root
    root_id = tokens.decode(token)["jti"]
    screener.set(B_ADDR, "ASK_HUMAN")
    resp = _subhire(client, eid, token, agents["B"], budget="6")
    assert resp.status_code == 202, resp.get_json()
    apr = resp.get_json()
    assert apr["kind"] == "subhire.fund" and apr["state"] == "pending" and apr["user_code"]
    child_id = apr["child_engagement_id"]
    action = _db.session.get(Approval, apr["approval_id"]).action
    assert action["engagement_id"] == child_id and action["parent_mandate_id"] == root_id
    assert (action["payee_address"], action["payee_agent_id"]) == (B_ADDR, agents["B"])
    assert action["amount_micro"] == 6_000_000 and action["screening_ack"] is True
    assert action["screening_id"] == apr["screening"]["id"]
    # nothing moves before the human approves
    assert not _allocs() and _mandate(root_id).spent_micro == 0 and named == []
    assert client.get(f"/api/engagements/{child_id}").get_json()["status"] == "awaiting_approval"

    row = approve(apr["approval_id"])
    assert row.state == "consumed" and row.human_sub == human.world_sub
    child = client.get(f"/api/engagements/{child_id}").get_json()
    assert child["status"] == "funded" and child["mandate"]
    [alloc] = child["ledger"]
    assert (alloc["kind"], alloc["amount_micro"], alloc["approval_id"]) == \
        ("subhire_alloc", 6_000_000, apr["approval_id"])
    assert _mandate(root_id).spent_micro == 6_000_000
    assert named == [child_id]


def test_ask_human_rejects_any_other_human(client, root, agents, screener, approve):
    eid, token = root
    screener.set(B_ADDR, "ASK_HUMAN")
    apr = _subhire(client, eid, token, agents["B"]).get_json()
    row = approve(apr["approval_id"], sub=OTHER_SUB)
    assert (row.state, row.failure_code) == ("rejected", "WRONG_HUMAN")
    assert not _allocs() and _mandate(tokens.decode(token)["jti"]).spent_micro == 0


# ── mandate authentication ────────────────────────────────────────────────
def _b64(obj) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


def test_missing_or_tampered_mandate_is_401(client, root, agents, screener):
    eid, token = root
    for headers in ({}, {"Authorization": "Bearer x"}, {"Authorization": "Mandate "}):
        resp = client.post(f"/api/engagements/{eid}/subhire", headers=headers,
                           json={"agent_id": agents["B"], "outcome": "x", "budget_usdc": 1,
                                 "category": "Development"})
        assert resp.status_code == 401 and resp.get_json()["code"] == "MANDATE_INVALID"

    header, payload, sig = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    claims["cap"]["budget_micro"] = 250_000_000
    tampered = ".".join((header, _b64(claims), sig))
    resp = _subhire(client, eid, tampered, agents["B"])
    assert resp.status_code == 401 and resp.get_json()["code"] == "MANDATE_INVALID"
    # validly signed but never issued: not on record
    forged = tokens.encode(claims)
    resp = _subhire(client, eid, forged, agents["B"])
    assert resp.status_code == 401 and resp.get_json()["code"] == "MANDATE_INVALID"
    assert not _allocs()


def test_expired_mandate_is_401(client, root, agents, screener):
    eid, token = root
    row = _mandate(tokens.decode(token)["jti"])
    now = int(time.time())
    claims = {**tokens.decode(token), "iat": now - 120, "nbf": now - 120, "exp": now - 60}
    row.token = tokens.encode(claims)
    row.expires_at = datetime.fromtimestamp(now - 60, tz=timezone.utc)
    _db.session.commit()
    resp = _subhire(client, eid, row.token, agents["B"])
    assert resp.status_code == 401 and resp.get_json()["code"] == "MANDATE_EXPIRED"


def test_revoked_mandate_is_403(client, root, agents, screener):
    eid, token = root
    b = _subhire(client, eid, token, agents["B"]).get_json()
    mandates.revoke(tokens.decode(token)["jti"])          # revokes B's child mandate too
    for parent, tok in ((eid, token), (b["engagement_id"], b["mandate_token"])):
        resp = _subhire(client, parent, tok, agents["C"], budget="1")
        assert resp.status_code == 403 and resp.get_json()["code"] == "MANDATE_REVOKED"
    assert len(_allocs()) == 1


def test_mandate_must_belong_to_the_engagement(client, root, agents, screener):
    eid, token = root
    b = _subhire(client, eid, token, agents["B"]).get_json()
    resp = _subhire(client, b["engagement_id"], token, agents["C"], budget="1")
    assert resp.status_code == 403 and resp.get_json()["code"] == "MANDATE_INVALID"


# ── chain page ────────────────────────────────────────────────────────────
def test_chain_page_renders_the_tree(client, root, agents, screener, human):
    eid, token = root
    b = _subhire(client, eid, token, agents["B"], budget="10").get_json()
    screener.set(C_ADDR, "ASK_HUMAN")
    pending = _subhire(client, eid, token, agents["C"], budget="3").get_json()

    html = client.get(f"/jobs/{eid}/chain").get_data(as_text=True)
    assert "HUM-" + tokens.hum_hash(human.world_sub)[:12] in html
    assert human.world_sub not in html and token not in html
    assert "Root mandate" in html and tokens.decode(token)["jti"] in html
    assert "10.00 USDC spent / 25.00 USDC" in html
    assert f'href="/jobs/{b["engagement_id"]}"' in html
    assert f'href="/jobs/{pending["child_engagement_id"]}"' in html
    assert 'data-verdict="PAY"' in html and 'data-verdict="ASK_HUMAN"' in html
    assert "waiting for the human" in html and "Sub-hire allocated to Agent B" in html
    assert 'data-depth="1"' in html
    assert html.index("Escrow funded for") < html.index("Sub-hire allocated to Agent B")

    child_html = client.get(f"/jobs/{b['engagement_id']}/chain").get_data(as_text=True)
    assert "(this job)" in child_html and tokens.decode(token)["jti"] in child_html
    detail = client.get(f"/jobs/{b['engagement_id']}").get_data(as_text=True)
    assert f"/jobs/{b['engagement_id']}/chain" in detail and "Approve &amp; fund" not in detail
    assert client.get("/jobs/ENG-NOPE/chain").status_code == 404


# ── MCP tool against the real endpoint ────────────────────────────────────
class _FlaskSession:
    """requests.Session stand-in that routes the MCP client into the app."""

    def __init__(self, client):
        self.client = client

    def request(self, method, url, *, params=None, json=None, headers=None, timeout=None):
        path = "/" + url.split("://", 1)[1].split("/", 1)[1]
        resp = self.client.open(path, method=method, query_string=params, json=json,
                                headers=headers)
        body = resp.get_json(silent=True)

        class _Response:
            status_code = resp.status_code
            text = resp.get_data(as_text=True)

            def json(self):
                if body is None:
                    raise ValueError("no json")
                return body
        return _Response()


def test_mcp_subhire_tool_uses_the_real_endpoint(client, root, agents, screener, monkeypatch):
    from agentslist_mcp import tools
    from agentslist_mcp.client import AgentListClient
    eid, token = root
    monkeypatch.setenv("MCP_API_TOKEN", "s3cret")
    mcp = AgentListClient("http://app.test", "s3cret", session=_FlaskSession(client))
    status = tools.get_engagement_status(mcp, eid)
    assert status["mandate_token"] == token                 # the hired agent's authority
    out = tools.subhire(mcp, eid, agents["B"], 7, "Development", status["mandate_token"],
                        outcome="Write the export tests")
    assert out["ok"] and out["status"] == "funded" and out["allocated_micro"] == 7_000_000
    assert mandates.verify_chain(out["mandate_token"])["sub"] == agents["B"]
    denied = tools.subhire(mcp, eid, agents["C"], 1, "Design", token)
    assert denied["ok"] is False and denied["code"] == "CATEGORY_NOT_ALLOWED"
