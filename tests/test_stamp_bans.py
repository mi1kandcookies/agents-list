"""Operator stamps, sticky bans and weekly caps.

A stamp is a manifest.publish approval bound to the manifest hash; editing
the manifest requires a re-stamp; bans follow the operator's World ID sub to
every agent they run and every approval they make; the weekly cap is enforced
at consume. Runs the real approval service against the fake World ID provider
and the fake screener."""
from __future__ import annotations

import hashlib

import pytest

from app.approvals import service as approvals
from app.approvals.errors import ApprovalError
from app.extensions import db as _db
from app.seller import stamp
from tests.conftest import WALLET

SUB = "0x" + "5" * 64          # the fake provider's default subject (= `human` fixture)
OTHER_SUB = "0x" + "7" * 64
PAYOUT = WALLET.lower()
FORM = {"model": "Anthropic | claude", "tools": "bash\nbrowser", "mcp_servers": "github",
        "skills": "Writes tests", "price_min_usdc": "0.05", "price_max_usdc": "0.2",
        "payout_address": WALLET}


@pytest.fixture(autouse=True)
def _mandate_key(app):
    from app.mandates import tokens
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
        if row.flow == "device":
            if row.state == "created":
                approvals.start_device(row)
            world_idp.approve_device(row.device_code, **claims)
            approvals.poll(row)
        else:
            approvals.start_web(row)
            code = world_idp.issue_code(nonce=row.nonce, **claims)
            approvals.complete_web(row.state_param, code, None)
        _db.session.expire_all()
        return _db.session.get(Approval, approval_id)
    return _approve


def _login(client, human):
    with client.session_transaction() as s:
        s["human_id"] = human.id
        s["human_sub"] = human.world_sub


def _agent(agent_id):
    from app.models import Agent
    _db.session.expire_all()
    return _db.session.get(Agent, agent_id)


def _stamp(client, approve, agent_id, **claims):
    """Save FORM and stamp it through the page; returns the approval."""
    resp = client.post(f"/seller/agents/{agent_id}/manifest", data={**FORM, "action": "stamp"})
    assert resp.status_code == 302 and "/approvals/APR-" in resp.headers["Location"]
    approval_id = resp.headers["Location"].split("/approvals/")[1].split("/")[0]
    return approve(approval_id, **claims)


@pytest.fixture()
def stamped(client, approve, screener, human, agent):
    _login(client, human)
    approval = _stamp(client, approve, agent)
    assert approval.state == "consumed", approval.failure_detail
    return approval


def _hire(client, agent_id, budget="25"):
    public_id = _agent(agent_id).public_id
    resp = client.post("/api/engagements", json={"agent_id": public_id, "outcome": "Write tests",
                                                 "budget_usdc": budget})
    assert resp.status_code == 201, resp.get_json()
    eid = resp.get_json()["engagement_id"]
    return client.post(f"/api/engagements/{eid}/hire",
                       json={"flow": "device", "confirm_amount_usdc": budget})


# ── stamping ──────────────────────────────────────────────────────────────
def test_stamp_approval_binds_manifest_hash(stamped, agent, screener):
    from app.models import EnsName, Screening
    row = _agent(agent)
    manifest = stamp.current_manifest(row)
    assert manifest["tools"] == ["bash", "browser"] and manifest["payout_address"] == PAYOUT
    h = stamp.manifest_hash(manifest)
    assert stamped.kind == "manifest.publish"
    assert stamped.action["manifest_hash"] == h
    assert stamped.action["payee_address"] == PAYOUT
    assert stamped.action["payee_agent_id"] == row.public_id
    assert row.manifest_hash == h and row.manifest_stamp_approval_id == stamped.id
    assert row.manifest_stamp_sub == SUB and row.manifest_stamped_at is not None
    assert stamp.stamped_manifest(row) == manifest
    assert stamp.stamp_status(row).ok
    stamp.assert_hireable(row)
    onboard = Screening.query.filter_by(hop="payee.onboard", agent_id=row.id).one()
    assert onboard.verdict == "PAY" and onboard.chain_address == PAYOUT
    assert any(c["hop"] == "payee.onboard" for c in screener.calls)
    assert EnsName.query.filter_by(agent_id=row.id, kind="agent").count() == 1


def test_hash_ignores_list_order_but_binds_every_field(agent, db):
    row = _agent(agent)
    base = stamp.build_manifest(row, **{**FORM, "tools": "browser\nbash\nbash"})
    assert base == stamp.build_manifest(row, **FORM)
    for change in ({"model": "other"}, {"price_max_usdc": "0.21"}, {"skills": "More"},
                   {"payout_address": "0x" + "c" * 40}, {"mcp_servers": ""}):
        assert stamp.manifest_hash(stamp.build_manifest(row, **{**FORM, **change})) \
            != stamp.manifest_hash(base)
    with pytest.raises(stamp.ManifestError):
        stamp.build_manifest(row, **{**FORM, "payout_address": "0x123"})
    with pytest.raises(stamp.ManifestError):
        stamp.build_manifest(row, **{**FORM, "price_min_usdc": "1", "price_max_usdc": "0.5"})


def test_unstamped_agent_is_not_hireable(client, screener, agent):
    resp = _hire(client, agent)
    assert resp.status_code == 409 and resp.get_json()["code"] == "NOT_STAMPED"


def test_stamped_agent_can_be_hired(client, stamped, agent):
    resp = _hire(client, agent)
    assert resp.status_code == 202, resp.get_json()


def test_editing_manifest_requires_restamp(client, stamped, agent):
    resp = client.post(f"/seller/agents/{agent}/manifest",
                       data={**FORM, "tools": "bash", "action": "save"})
    assert resp.status_code == 302
    row = _agent(agent)
    assert stamp.stamp_status(row).code == "RESTAMP_REQUIRED"
    with pytest.raises(stamp.NotHireable) as exc:
        stamp.assert_hireable(row)
    assert exc.value.code == "RESTAMP_REQUIRED"
    resp = _hire(client, agent)
    assert resp.status_code == 409 and resp.get_json()["code"] == "RESTAMP_REQUIRED"
    page = client.get(f"/seller/agents/{agent}/manifest").get_data(as_text=True)
    assert 'id="manifest-diff"' in page and "browser" in page and "Re-stamp required" in page


def test_restamp_after_edit_restores_hireability(client, approve, stamped, agent):
    client.post(f"/seller/agents/{agent}/manifest", data={**FORM, "tools": "bash", "action": "save"})
    approval = _stamp(client, approve, agent)
    assert approval.state == "consumed" and approval.id != stamped.id
    assert stamp.stamp_status(_agent(agent)).ok


def test_manifest_change_during_approval_fails_the_stamp(client, approve, screener, human, agent):
    _login(client, human)
    resp = client.post(f"/seller/agents/{agent}/manifest", data={**FORM, "action": "stamp"})
    approval_id = resp.headers["Location"].split("/approvals/")[1].split("/")[0]
    client.post(f"/seller/agents/{agent}/manifest", data={**FORM, "model": "x", "action": "save"})
    assert approve(approval_id).state == "failed"
    assert stamp.stamp_status(_agent(agent)).code == "NOT_STAMPED"


def test_refused_payout_keeps_stamp_but_blocks_listing(client, approve, screener, human, agent):
    screener.set(PAYOUT, "REFUSE")
    _login(client, human)
    approval = _stamp(client, approve, agent)
    assert approval.state == "consumed"
    row = _agent(agent)
    assert row.manifest_stamp_approval_id == approval.id
    assert stamp.stamp_status(row).code == "PAYEE_REFUSED"
    resp = _hire(client, agent)
    assert resp.status_code == 409 and resp.get_json()["code"] == "PAYEE_REFUSED"


def test_manifest_page_requires_login_and_the_operator(client, stamped, agent, db):
    from flask import g
    from app.models import Human
    g.pop("current_human", None)  # tests share one app context, so g outlives requests
    fresh = client.application.test_client()
    assert fresh.get(f"/seller/agents/{agent}/manifest").status_code == 302
    other = Human(world_sub=OTHER_SUB)
    db.session.add(other)
    db.session.commit()
    _login(fresh, other)
    g.pop("current_human", None)
    resp = fresh.post(f"/seller/agents/{agent}/manifest", data={**FORM, "action": "save"})
    assert resp.status_code == 403
    assert stamp.stamp_status(_agent(agent)).ok


# ── bans ──────────────────────────────────────────────────────────────────
def test_banned_operator_blocks_agents_and_approvals(client, approve, stamped, agent, human):
    from app.humans import service as humans
    humans.ban(human, "fraud")
    _db.session.commit()
    row = _agent(agent)
    assert stamp.stamp_status(row).code == "OPERATOR_BANNED"
    assert stamp.agents_operated_by(SUB) == [row]
    resp = _hire(client, agent)
    assert resp.status_code == 409 and resp.get_json()["code"] == "OPERATOR_BANNED"

    # A new approval by the banned human is rejected at sign-in …
    fresh = stamp.request_stamp(row)
    assert approve(fresh.id).failure_code == "BANNED"
    # … and one that was already approved is blocked at consume.
    pending = stamp.request_stamp(row)
    pending.state, pending.human_sub = "approved", SUB
    _db.session.commit()
    with pytest.raises(ApprovalError) as exc:
        approvals.consume(pending.id, kind="manifest.publish")
    assert exc.value.code == "BANNED"
    assert approvals.get(pending.id).state == "blocked"


def test_unban_restores_hireability(client, stamped, agent, human):
    client.post(f"/admin/humans/{human.id}/ban", json={"reason": "review"})
    assert stamp.stamp_status(_agent(agent)).code == "OPERATOR_BANNED"
    client.post(f"/admin/humans/{human.id}/unban", json={})
    assert stamp.stamp_status(_agent(agent)).ok


# ── weekly cap ────────────────────────────────────────────────────────────
def test_weekly_cap_exceeded_at_consume(client, approve, stamped, agent, human):
    resp = client.post(f"/admin/humans/{human.id}/cap", json={"weekly_cap_usdc": "10"})
    assert resp.status_code == 200 and resp.get_json()["weekly_cap_micro"] == 10_000_000
    hire = _hire(client, agent, budget="25")
    assert hire.status_code == 202
    approval = approve(hire.get_json()["approval_id"])
    assert approval.state == "blocked" and approval.failure_code == "CAP_EXCEEDED"


def test_cap_validation_and_reset(client, human):
    bad = client.post(f"/admin/humans/{human.id}/cap", json={"weekly_cap_usdc": "-1"})
    assert bad.status_code == 400
    ok = client.post(f"/admin/humans/{human.id}/cap", json={"weekly_cap_usdc": ""})
    assert ok.status_code == 200 and ok.get_json()["weekly_cap_micro"] is None


def test_admin_mutations_need_api_key(app, client, human):
    app.config["API_KEY"] = "k"
    assert client.post(f"/admin/humans/{human.id}/ban", json={"reason": "x"}).status_code == 401
    resp = client.post(f"/admin/humans/{human.id}/ban", json={"reason": "x"},
                       headers={"X-Api-Key": "k"})
    assert resp.status_code == 200 and resp.get_json()["banned"] is True


# ── pages ─────────────────────────────────────────────────────────────────
def test_admin_humans_page_renders(client, stamped, human):
    html = client.get("/admin/humans").get_data(as_text=True)
    assert hashlib.sha256(SUB.encode()).hexdigest()[:16] in html
    assert SUB not in html                      # only the hashed subject is shown
    assert "Active" in html and "Set cap" in html
    client.post(f"/admin/humans/{human.id}/ban", data={"reason": "chargebacks"})
    html = client.get("/admin/humans").get_data(as_text=True)
    assert "Banned" in html and "chargebacks" in html and "Unban" in html


def test_stamp_shown_on_profile_and_unstamped_hidden_from_marketplace(client, stamped, agent):
    assert "Configuration approved by its operator with World ID" in \
        client.get(f"/agent/{agent}").get_data(as_text=True)
    client.post(f"/seller/agents/{agent}/manifest", data={**FORM, "model": "new", "action": "save"})
    html = client.get(f"/agent/{agent}").get_data(as_text=True)
    assert "Configuration approved by its operator" not in html   # re-stamp pending
    from app.models import Agent
    from app.extensions import db
    name = db.session.get(Agent, agent).name
    assert name not in client.get("/marketplace").get_data(as_text=True)


def test_manifest_page_renders_hashes(client, stamped, agent):
    html = client.get(f"/seller/agents/{agent}/manifest").get_data(as_text=True)
    assert stamped.action["manifest_hash"] in html and "Stamp with World ID" in html
    bad = client.post(f"/seller/agents/{agent}/manifest", data={**FORM, "payout_address": "nope"})
    assert bad.status_code == 400 and "payout address" in bad.get_data(as_text=True)


# ── dev seed ──────────────────────────────────────────────────────────────
def test_seed_stamps_marks_sample_agents(app, db):
    from app.models import Agent
    from app.sample_data import seed_sample_agents
    seed_sample_agents(db, Agent)
    result = app.test_cli_runner().invoke(args=["seed-stamps"])
    assert result.exit_code == 0 and "SIMULATED" in result.output
    rows = Agent.query.all()
    assert rows and all(r.manifest_stamp_sub == stamp.DEV_STAMP_SUB for r in rows)
    assert all(stamp.stamp_status(r).ok and stamp.stamp_status(r).simulated for r in rows)


def test_seed_stamps_refuses_in_production(app, monkeypatch):
    monkeypatch.setitem(app.config, "ENV_NAME", "production")
    monkeypatch.setenv("WORLD_CLIENT_ID", "app_live")
    result = app.test_cli_runner().invoke(args=["seed-stamps"])
    assert result.exit_code == 1 and "Refusing" in result.output
