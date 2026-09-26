"""Approval lifecycle, action binding and step-up (docs/decisions/0001-custody-chain.md §3)."""
from __future__ import annotations

import time
from urllib.parse import parse_qs, urlsplit

import pytest

from app.approvals import actions, service
from app.approvals.executors import EXECUTORS, ExecutionResult

MANIFEST = {"manifest_hash": "0x" + "a" * 64}
OTHER_SUB = "0x" + "6" * 64


@pytest.fixture()
def calls(monkeypatch):
    """Fake executors for manifest.publish and engagement.fund; records calls."""
    calls = []

    def fake(approval, action):
        calls.append((approval.id, action))
        return ExecutionResult(ok=True, summary="done", ledger_ids=["LED-TEST"])

    monkeypatch.setitem(EXECUTORS, "manifest.publish", fake)
    monkeypatch.setitem(EXECUTORS, "engagement.fund", fake)
    return calls


def _reload(db, approval_id):
    from app.models import Approval
    db.session.expire_all()
    return db.session.get(Approval, approval_id)


def _events(approval):
    return [e.event for e in approval.events]


def _start_web(client, approval_id) -> dict:
    resp = client.get(f"/approvals/{approval_id}/start")
    assert resp.status_code == 302, resp.data
    return {k: v[0] for k, v in parse_qs(urlsplit(resp.headers["Location"]).query).items()}


def _callback(client, params: dict, **query):
    return client.get("/auth/world/callback", query_string={"state": params["state"], **query})


def _engagement(db, agent, human, **kw):
    from app.models import Engagement
    eng = Engagement(agent_id=agent, buyer_human_id=human.id if human else None, outcome="x",
                     total_micro=5_000_000, **kw)
    db.session.add(eng)
    db.session.commit()
    return eng


# ── web flow ────────────────────────────────────────────────────────────────

def test_web_round_trip_approves_consumes_and_executes_once(client, db, world_idp, calls):
    approval = service.create_approval("manifest.publish", MANIFEST, flow="web")
    assert approval.state == "created" and approval.id.startswith("APR-")
    action = approval.action
    assert action["approval_id"] == approval.id and approval.action_hash == actions.action_hash(action)

    params = _start_web(client, approval.id)
    assert params["nonce"] == actions.action_nonce(action) == approval.nonce
    assert params["prompt"] == "login" and params["max_age"] == "0"
    assert params["code_challenge_method"] == "S256" and params["code_challenge"] and params["state"]
    assert _reload(db, approval.id).state == "pending"

    code = world_idp.issue_code(nonce=params["nonce"])
    resp = _callback(client, params, code=code)
    assert resp.status_code == 302 and resp.headers["Location"].endswith(f"/approvals/{approval.id}")

    row = _reload(db, approval.id)
    assert row.state == "consumed" and row.consumed_at is not None
    assert row.human_sub == "0x" + "5" * 64 and row.id_token_jti and row.auth_time
    assert len(calls) == 1 and calls[0][0] == approval.id and calls[0][1] == action
    assert _events(row)[:4] == ["created", "pending", "approved", "consumed"]
    # PKCE verifier went to the token endpoint with client_secret_basic.
    token_req = [r for r in world_idp.requests if r["url"].endswith("/token")][0]
    assert token_req["data"]["code_verifier"] and token_req["headers"]["Authorization"].startswith("Basic ")

    page = client.get(f"/approvals/{approval.id}")
    assert page.status_code == 200 and b"Approved" in page.data and b"done" in page.data
    api = client.get(f"/api/approvals/{approval.id}").get_json()
    assert api["state"] == "consumed" and api["result"]["ledger_ids"] == ["LED-TEST"]
    assert api["summary"][0] == ["Action", "Publish manifest"] and "device_code" not in api


def test_access_denied_denies_without_executing(client, db, world_idp, calls):
    approval = service.create_approval("manifest.publish", MANIFEST, flow="web")
    params = _start_web(client, approval.id)
    _callback(client, params, error="access_denied")
    row = _reload(db, approval.id)
    assert row.state == "denied" and calls == []
    assert b"Denied" in client.get(f"/approvals/{approval.id}").data


def test_expired_even_if_a_valid_token_arrives_later(client, db, world_idp, calls, monkeypatch):
    approval = service.create_approval("manifest.publish", MANIFEST, flow="web")
    params = _start_web(client, approval.id)
    code = world_idp.issue_code(nonce=params["nonce"])
    monkeypatch.setattr(service, "clock", lambda: time.time() + service.ttl_seconds() + 5)
    _callback(client, params, code=code)
    row = _reload(db, approval.id)
    assert row.state == "expired" and calls == [] and row.human_sub is None
    assert not [r for r in world_idp.requests if r["url"].endswith("/token")]  # code never redeemed
    assert b"Expired" in client.get(f"/approvals/{approval.id}").data


def test_nonce_for_another_action_is_rejected(client, db, world_idp, calls):
    a = service.create_approval("manifest.publish", MANIFEST, flow="web")
    b = service.create_approval("manifest.publish", MANIFEST, flow="web")
    assert a.nonce != b.nonce
    params_b = _start_web(client, b.id)
    _callback(client, params_b, code=world_idp.issue_code(nonce=a.nonce))
    row = _reload(db, b.id)
    assert row.state == "rejected" and row.failure_code == "NONCE_MISMATCH" and calls == []
    page = client.get(f"/approvals/{b.id}").data
    assert b"Rejected" in page and b"NONCE_MISMATCH" in page


def test_second_consume_is_blocked_as_replay(client, db, world_idp, calls):
    approval = service.create_approval("manifest.publish", MANIFEST, flow="web")
    params = _start_web(client, approval.id)
    _callback(client, params, code=world_idp.issue_code(nonce=params["nonce"]))
    assert _reload(db, approval.id).state == "consumed"

    with pytest.raises(service.ApprovalError) as err:
        service.consume(approval.id, kind="manifest.publish")
    assert err.value.code == "APPROVAL_CONSUMED" and err.value.http_status == 409
    row = _reload(db, approval.id)
    assert "replay_blocked" in _events(row) and row.state == "consumed" and len(calls) == 1
    assert b"Replay blocked" in client.get(f"/approvals/{approval.id}").data


def test_reused_jti_is_rejected(client, db, world_idp, calls):
    a = service.create_approval("manifest.publish", MANIFEST, flow="web")
    b = service.create_approval("manifest.publish", MANIFEST, flow="web")
    pa, pb = _start_web(client, a.id), _start_web(client, b.id)
    _callback(client, pa, code=world_idp.issue_code(nonce=pa["nonce"], jti="jti-1"))
    _callback(client, pb, code=world_idp.issue_code(nonce=pb["nonce"], jti="jti-1"))
    assert _reload(db, a.id).state == "consumed"
    row = _reload(db, b.id)
    assert row.state == "rejected" and row.failure_code == "REPLAYED_TOKEN" and len(calls) == 1
    assert b"Replay blocked" in client.get(f"/approvals/{b.id}").data


def test_different_sub_on_claimed_engagement_is_wrong_human(client, db, world_idp, calls, agent, human):
    eng = _engagement(db, agent, human)
    approval = service.create_approval("engagement.fund",
                                       {"engagement_id": eng.id, "amount_micro": 5_000_000},
                                       flow="web")
    params = _start_web(client, approval.id)
    _callback(client, params, code=world_idp.issue_code(nonce=params["nonce"], sub=OTHER_SUB))
    row = _reload(db, approval.id)
    assert row.state == "rejected" and row.failure_code == "WRONG_HUMAN" and calls == []


def test_first_approval_claims_the_engagement(client, db, world_idp, calls, agent):
    from app.models import Engagement, Human
    eng = _engagement(db, agent, None)
    approval = service.create_approval("engagement.fund",
                                       {"engagement_id": eng.id, "amount_micro": 5_000_000},
                                       flow="web")
    params = _start_web(client, approval.id)
    _callback(client, params, code=world_idp.issue_code(nonce=params["nonce"], sub=OTHER_SUB))
    assert _reload(db, approval.id).state == "consumed"
    buyer = db.session.get(Human, db.session.get(Engagement, eng.id).buyer_human_id)
    assert buyer.world_sub == OTHER_SUB


def test_banned_sub_is_rejected(client, db, world_idp, calls, human):
    from app.humans import service as humans
    humans.ban(human, "abuse")
    db.session.commit()
    approval = service.create_approval("manifest.publish", MANIFEST, flow="web")
    params = _start_web(client, approval.id)
    _callback(client, params, code=world_idp.issue_code(nonce=params["nonce"], sub=human.world_sub))
    row = _reload(db, approval.id)
    assert row.state == "rejected" and row.failure_code == "BANNED" and calls == []


def test_banned_or_over_cap_buyer_is_blocked_before_the_provider(db, agent, human, monkeypatch):
    monkeypatch.setenv("HUMAN_WEEKLY_CAP_USDC", "1")
    eng = _engagement(db, agent, human)
    over = service.create_approval("engagement.fund",
                                   {"engagement_id": eng.id, "amount_micro": 2_000_000}, flow="device")
    assert over.state == "blocked" and over.failure_code == "CAP_EXCEEDED"
    with pytest.raises(service.ApprovalStateError):
        service.start_device(over)  # blocked approvals never reach the provider
    assert over.device_code is None

    from app.humans import service as humans
    humans.ban(human, "abuse")
    db.session.commit()
    banned = service.create_approval("engagement.fund",
                                     {"engagement_id": eng.id, "amount_micro": 1}, flow="web")
    assert banned.state == "blocked" and banned.failure_code == "BANNED"
    with pytest.raises(service.ApprovalStateError):
        service.start_web(banned)


@pytest.mark.parametrize("verdict, state", [("PAY", "consumed"), ("REFUSE", "blocked"),
                                            ("ASK_HUMAN", "blocked")])
def test_consume_rescreens_the_payee(client, app, db, world_idp, calls, agent, fake_screener,
                                     verdict, state):
    payee = "0x" + "ab" * 20
    app.extensions["screener"] = fake_screener
    fake_screener.set(payee, verdict)
    eng = _engagement(db, agent, None)
    approval = service.create_approval(
        "engagement.fund", {"engagement_id": eng.id, "amount_micro": 5_000_000,
                            "payee_address": payee}, flow="web")
    params = _start_web(client, approval.id)
    _callback(client, params, code=world_idp.issue_code(nonce=params["nonce"]))
    row = _reload(db, approval.id)
    assert row.state == state and "rescreened" in _events(row)
    assert fake_screener.calls[-1]["hop"] == "engagement.fund"
    assert fake_screener.calls[-1]["chain_address"] == payee
    assert len(calls) == (1 if verdict == "PAY" else 0)
    if state == "blocked":
        assert row.failure_code == "SCREENING_REFUSED"


def test_bound_screening_is_shown_and_refuse_blocks(client, db, agent):
    from app.models import Screening
    rows = {}
    for verdict in ("PAY", "REFUSE"):
        rows[verdict] = Screening(hop="engagement.fund", chain_address="0x" + "ab" * 20,
                                  verdict=verdict, reasons=[], traits=[], provider="fake")
        db.session.add(rows[verdict])
    db.session.commit()
    eng = _engagement(db, agent, None)
    fields = {"engagement_id": eng.id, "amount_micro": 1}

    ok = service.create_approval("engagement.fund", {**fields, "screening_id": rows["PAY"].id},
                                 flow="device")
    data = client.get(f"/api/approvals/{ok.id}").get_json()
    assert data["state"] == "created" and data["screening"]["verdict"] == "PAY"
    assert {"cap_micro", "reasons", "fail_closed"} <= set(data["screening"])

    refused = service.create_approval("engagement.fund",
                                      {**fields, "screening_id": rows["REFUSE"].id}, flow="device")
    assert refused.state == "blocked" and refused.failure_code == "SCREENING_REFUSED"


def test_callback_from_another_browser_is_refused(client, app, db, world_idp, calls):
    approval = service.create_approval("manifest.publish", MANIFEST, flow="web")
    params = _start_web(client, approval.id)
    other = app.test_client()
    resp = _callback(other, params, code=world_idp.issue_code(nonce=params["nonce"]))
    assert resp.status_code == 400
    assert _reload(db, approval.id).state == "pending" and calls == []


def test_cancel(client, db, world_idp, calls):
    approval = service.create_approval("manifest.publish", MANIFEST, flow="web")
    params = _start_web(client, approval.id)
    resp = client.post(f"/api/approvals/{approval.id}/cancel", json={"reason": "changed my mind"})
    assert resp.status_code == 200 and resp.get_json()["state"] == "cancelled"
    # A token arriving afterwards changes nothing.
    _callback(client, params, code=world_idp.issue_code(nonce=params["nonce"]))
    row = _reload(db, approval.id)
    assert row.state == "cancelled" and calls == []
    assert client.post(f"/api/approvals/{approval.id}/cancel").status_code == 200  # idempotent
    assert b"Cancelled" in client.get(f"/approvals/{approval.id}").data
    assert client.post("/api/approvals/APR-NOPE/cancel").status_code == 404


def test_executor_error_fails_and_rolls_back_its_writes(client, db, world_idp, monkeypatch):
    from app.models import Human

    def boom(approval, action):
        db.session.add(Human(world_sub="written-by-executor"))
        db.session.flush()
        raise RuntimeError("chain unavailable")

    monkeypatch.setitem(EXECUTORS, "manifest.publish", boom)
    approval = service.create_approval("manifest.publish", MANIFEST, flow="web")
    params = _start_web(client, approval.id)
    _callback(client, params, code=world_idp.issue_code(nonce=params["nonce"]))
    row = _reload(db, approval.id)
    assert row.state == "failed" and row.failure_code == "EXECUTOR_ERROR" and row.consumed_at
    assert db.session.query(Human).filter_by(world_sub="written-by-executor").count() == 0
    with pytest.raises(service.ApprovalError) as err:
        service.consume(approval.id, kind="manifest.publish")
    assert err.value.code == "APPROVAL_CONSUMED"


# ── device flow ─────────────────────────────────────────────────────────────

def test_device_flow_without_nonce_uses_server_binding(client, db, world_idp, calls, monkeypatch):
    approval = service.create_approval("manifest.publish", MANIFEST, flow="device")
    assert client.get(f"/approvals/{approval.id}/start").status_code == 302
    row = _reload(db, approval.id)
    assert row.state == "pending" and row.user_code and row.device_code
    assert world_idp.devices[row.device_code]["nonce"] == approval.nonce  # nonce was sent

    page = client.get(f"/approvals/{approval.id}").data
    assert row.user_code.encode() in page and b"/activate?user_code=" in page

    def token_polls():
        return len([r for r in world_idp.requests if r["url"].endswith("/token")])

    assert client.get(f"/api/approvals/{approval.id}").get_json()["state"] == "pending"
    polls = token_polls()
    client.get(f"/api/approvals/{approval.id}")  # inside the poll interval: provider not asked
    assert token_polls() == polls

    world_idp.approve_device(row.device_code, nonce=None)  # provider omits the nonce
    monkeypatch.setattr(service, "clock", lambda: time.time() + 6)
    data = client.get(f"/api/approvals/{approval.id}").get_json()
    assert data["state"] == "consumed" and len(calls) == 1
    assert "nonce_absent" in _events(_reload(db, approval.id))


def test_device_flow_nonce_for_another_action_is_rejected(client, db, world_idp, calls):
    approval = service.create_approval("manifest.publish", MANIFEST, flow="device")
    service.start_device(approval)
    world_idp.approve_device(approval.device_code, nonce="x" * 43)
    assert service.poll(approval).state == "rejected"
    assert _reload(db, approval.id).failure_code == "NONCE_MISMATCH" and calls == []


def test_device_flow_denied(client, db, world_idp, calls):
    approval = service.create_approval("manifest.publish", MANIFEST, flow="device")
    service.start_device(approval)
    world_idp.deny_device(approval.device_code)
    assert client.get(f"/api/approvals/{approval.id}").get_json()["state"] == "denied"
    assert calls == []


# ── sign-in ─────────────────────────────────────────────────────────────────

def test_login_logout(client, db, world_idp):
    resp = client.get("/login?next=/marketplace")
    assert resp.status_code == 302
    params = {k: v[0] for k, v in parse_qs(urlsplit(resp.headers["Location"]).query).items()}
    assert params["prompt"] == "login" and params["max_age"] == "0"
    resp = _callback(client, params, code=world_idp.issue_code(nonce=params["nonce"]))
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/marketplace")
    with client.session_transaction() as sess:
        assert sess["human_sub"] == "0x" + "5" * 64 and sess["human_id"]
    assert client.get("/login?next=/past-jobs").headers["Location"].endswith("/past-jobs")

    client.get("/logout")
    with client.session_transaction() as sess:
        assert "human_id" not in sess


def test_login_required(app, world_idp):
    from app.identity.session import current_human, login_required

    @login_required
    def private():
        return current_human().world_sub

    app.add_url_rule("/private-test", "private_test", private)
    app.add_url_rule("/api/private-test", "api_private_test", private)
    client = app.test_client()
    resp = client.get("/private-test?x=1")
    assert resp.status_code == 302 and "/login?next=/private-test" in resp.headers["Location"]
    assert client.get("/api/private-test").status_code == 401

    location = client.get("/login").headers["Location"]
    params = {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}
    _callback(client, params, code=world_idp.issue_code(nonce=params["nonce"]))
    assert client.get("/private-test").data.decode() == "0x" + "5" * 64


def test_login_rejects_open_redirect(client, world_idp):
    resp = client.get("/login?next=//evil.example/")
    params = {k: v[0] for k, v in parse_qs(urlsplit(resp.headers["Location"]).query).items()}
    resp = _callback(client, params, code=world_idp.issue_code(nonce=params["nonce"]))
    assert resp.headers["Location"] in ("/", "http://localhost/")
