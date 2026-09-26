"""Engagements: SOW, hire → fund approval → escrow, submit, release with a
fresh approval each time, screening outcomes, and the /jobs pages.

Runs the real approval service (§3) against the fake World ID provider and
the fake screener (installed as ``app.extensions["screener"]``), so
consume()'s re-screen and replay guards are exercised too."""
from __future__ import annotations

import sys

import pytest

from app.approvals import service as approvals
from app.approvals.errors import ApprovalError
from app.approvals.executors import get_executor
from app.engagements.sow import build_sow, sow_hash
from app.extensions import db as _db
from tests.conftest import WALLET

PAYEE = WALLET.lower()


@pytest.fixture(autouse=True)
def _mandate_key(app):
    """Funded engagements mint a root mandate; never touch the instance key."""
    from app.mandates import tokens
    app.config["MANDATE_SIGNING_KEY"] = tokens.generate_pem()


@pytest.fixture()
def screener(app, fake_screener):
    app.extensions["screener"] = fake_screener
    return fake_screener


@pytest.fixture()
def approve(world_idp):
    """Complete the World ID sign-in for an approval (device or web flow);
    the approval service then runs the executor. Returns the approval row."""
    from app.models import Approval

    def _approve(approval_id, **claims):
        row = _db.session.get(Approval, approval_id)
        if row.flow == "device":
            if row.state == "created":
                approvals.start_device(row)
            world_idp.approve_device(row.device_code, **claims)
            row = approvals.poll(row)
        else:
            approvals.start_web(row)
            code = world_idp.issue_code(nonce=row.nonce, **claims)
            row = approvals.complete_web(row.state_param, code, None)
        _db.session.expire_all()
        return _db.session.get(Approval, approval_id)
    return _approve


@pytest.fixture()
def agent_public_id(db, agent):
    from app.models import Agent
    return db.session.get(Agent, agent).public_id


def _create(client, agent_public_id, **overrides):
    body = {"agent_id": agent_public_id, "outcome": "Add a CSV export to the reports page",
            "budget_usdc": "25",
            "milestones": [{"title": "Draft", "acceptance": "Outline approved", "amount_usdc": 10},
                           {"title": "Final", "acceptance": "Tests pass", "amount_usdc": "15.00"}]}
    body.update(overrides)
    return client.post("/api/engagements", json=body)


def _engagement(client, agent_public_id, **overrides) -> dict:
    resp = _create(client, agent_public_id, **overrides)
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()


def _hire(client, eid, **body):
    return client.post(f"/api/engagements/{eid}/hire",
                       json={"flow": "device", "confirm_amount_usdc": "25", **body})


def _funded(client, approve, agent_public_id) -> str:
    eid = _engagement(client, agent_public_id)["engagement_id"]
    apr = _hire(client, eid).get_json()
    assert approve(apr["approval_id"]).state == "consumed"
    return eid


def _release(client, eid, idx, **body):
    return client.post(f"/api/engagements/{eid}/milestones/{idx}/release", json=body)


def _ledger(eid):
    from app.models import LedgerEntry
    return LedgerEntry.query.filter_by(engagement_id=eid).order_by(LedgerEntry.created_at).all()


# ── SOW ───────────────────────────────────────────────────────────────────
def test_sow_hash_is_stable_and_binds_every_term():
    args = dict(agent_public_id="AGT-SH8W-D5VP-P", outcome="Ship  the\nexport", budget_micro=25_000_000,
                milestones=[{"title": "A", "acceptance": "a", "amount_micro": 25_000_000}],
                deadline=1_900_000_000, category="Development")
    first = sow_hash(build_sow(**args))
    assert first == sow_hash(build_sow(**dict(reversed(list(args.items())))))
    assert build_sow(**args)["outcome"] == "Ship the export"   # whitespace-normalized
    assert first == sow_hash(build_sow(**{**args, "outcome": "Ship the export"}))
    for change in ({"budget_micro": 25_000_001}, {"deadline": 1_900_000_001},
                   {"milestones": [{"title": "B", "acceptance": "a", "amount_micro": 25_000_000}]}):
        assert sow_hash(build_sow(**{**args, **change})) != first


def test_create_engagement_returns_sow_milestones_and_preview(client, screener, agent_public_id):
    body = _engagement(client, agent_public_id)
    assert body["status"] == "scoped" and body["engagement_id"].startswith("ENG-")
    assert body["total_micro"] == 25_000_000
    assert [m["amount_micro"] for m in body["milestones"]] == [10_000_000, 15_000_000]
    assert body["sow_hash"] == sow_hash(body["sow"])
    assert body["sow"]["agent_id"] == agent_public_id
    assert body["screening"]["verdict"] == "PAY"
    assert screener.calls[-1]["chain_address"] == PAYEE
    same = _engagement(client, agent_public_id)
    assert same["sow_hash"] == body["sow_hash"] and same["engagement_id"] != body["engagement_id"]


def test_default_single_milestone(client, screener, agent_public_id):
    body = _engagement(client, agent_public_id, milestones=None, budget_usdc=7.5)
    assert [(m["idx"], m["amount_micro"]) for m in body["milestones"]] == [(0, 7_500_000)]


@pytest.mark.parametrize("overrides, code, field", [
    ({"agent_id": "AGT-0000-0000-X"}, "INVALID_AGENT_ID", "agent_id"),
    ({"budget_usdc": "30"}, "MILESTONES_MISMATCH", "milestones"),
    ({"budget_usdc": "25.0000001"}, "INVALID_REQUEST", "budget_usdc"),
    ({"budget_usdc": -5}, "INVALID_REQUEST", "budget_usdc"),
    ({"outcome": "  "}, "INVALID_REQUEST", "outcome"),
])
def test_create_validation(client, screener, agent_public_id, overrides, code, field):
    resp = _create(client, agent_public_id, **overrides)
    assert resp.status_code == 400
    assert (resp.get_json()["code"], resp.get_json()["field"]) == (code, field)


def test_unknown_agent_is_404(client, screener, db, agent):
    from app.common.agent_ids import from_db_id
    resp = _create(client, from_db_id(agent + 999))
    assert resp.status_code == 404 and resp.get_json()["code"] == "AGENT_NOT_FOUND"


# ── hire → fund ───────────────────────────────────────────────────────────
def test_hire_approve_fund(client, approve, screener, agent_public_id):
    eng = _engagement(client, agent_public_id)
    eid = eng["engagement_id"]
    resp = _hire(client, eid, confirm_amount_usdc="25.00")
    assert resp.status_code == 202
    apr = resp.get_json()
    assert apr["kind"] == "engagement.fund" and apr["state"] == "pending"
    assert apr["user_code"] and apr["action_hash"].startswith("0x")
    assert apr["screening"]["verdict"] == "PAY"
    assert ["Amount", "25.00 USDC"] in apr["summary"]
    assert screener.calls[-1]["hop"] == "engagement.fund"

    from app.models import Approval
    action = _db.session.get(Approval, apr["approval_id"]).action
    assert action["sow_hash"] == eng["sow_hash"] and action["amount_micro"] == 25_000_000
    assert action["payee_address"] == PAYEE and action["payee_agent_id"] == agent_public_id
    assert [m["amount_micro"] for m in action["milestones"]] == [10_000_000, 15_000_000]
    assert action["screening_id"] == apr["screening"]["id"] and action["screening_ack"] is False
    assert client.get(f"/api/engagements/{eid}").get_json()["status"] == "awaiting_approval"
    assert not _ledger(eid)                                  # nothing moves before approval

    calls_before = len(screener.calls)
    row = approve(apr["approval_id"])
    assert row.state == "consumed"
    assert screener.calls[calls_before]["hop"] == "engagement.fund"   # consume() re-screened
    result = approvals.result_of(row)
    assert result["ok"] and result["redirect"] == f"/jobs/{eid}"
    body = client.get(f"/api/engagements/{eid}").get_json()
    assert body["status"] == "funded" and body["escrow_mode"] == "simulated"
    assert {m["status"] for m in body["milestones"]} == {"funded"}
    [fund] = body["ledger"]
    assert fund["kind"] == "fund" and fund["amount_micro"] == 25_000_000
    assert fund["status"] == "simulated" and fund["simulated"] and fund["explorer"] is None
    assert result["ledger_ids"] == [fund["id"]]
    [a] = body["approvals"]
    assert a["state"] == "consumed" and a["result"]["ledger_ids"] == [fund["id"]]


def test_hire_confirm_amount_must_match(client, screener, agent_public_id):
    eid = _engagement(client, agent_public_id)["engagement_id"]
    resp = _hire(client, eid, confirm_amount_usdc="24.99")
    assert resp.status_code == 400 and resp.get_json()["code"] == "AMOUNT_MISMATCH"


def test_refuse_blocks_hire_with_no_approval_or_ledger(client, screener, agent_public_id):
    from app.models import Approval
    eid = _engagement(client, agent_public_id)["engagement_id"]
    screener.set(PAYEE, "REFUSE")
    resp = _hire(client, eid)
    assert resp.status_code == 403
    body = resp.get_json()
    assert body["code"] == "SCREENING_REFUSED" and body["screening"]["verdict"] == "REFUSE"
    assert Approval.query.count() == 0 and not _ledger(eid)
    assert client.get(f"/api/engagements/{eid}").get_json()["status"] == "refused"


def test_client_supplied_screening_is_ignored(client, screener, agent_public_id):
    from app.models import Approval
    eid = _engagement(client, agent_public_id)["engagement_id"]
    screener.set(PAYEE, "REFUSE")
    forged = {"id": "SCR-000000000000", "verdict": "PAY", "fail_closed": False}
    resp = _hire(client, eid, screening=forged, screening_id=forged["id"], verdict="PAY")
    assert resp.status_code == 403 and resp.get_json()["code"] == "SCREENING_REFUSED"
    assert Approval.query.count() == 0


def test_client_supplied_screening_is_ignored_on_release(client, approve, screener,
                                                        agent_public_id):
    eid = _funded(client, approve, agent_public_id)
    screener.cap_micro = 1_000_000
    screener.set(PAYEE, "CAP")
    apr = _release(client, eid, 0, screening={"id": "SCR-000000000000", "verdict": "PAY"},
                   amount_micro=10_000_000).get_json()
    assert ["Amount", "1.00 USDC"] in apr["summary"] and apr["screening"]["verdict"] == "CAP"


def test_ask_human_sets_screening_ack(client, world_idp, screener, agent_public_id):
    from app.models import Approval
    eid = _engagement(client, agent_public_id)["engagement_id"]
    screener.set(PAYEE, "ASK_HUMAN")
    apr = _hire(client, eid).get_json()
    assert _db.session.get(Approval, apr["approval_id"]).action["screening_ack"] is True
    assert any("warning acknowledged" in value for _, value in apr["summary"])


def test_cap_below_the_job_total_blocks_hire(client, screener, agent_public_id):
    from app.models import Approval
    eid = _engagement(client, agent_public_id)["engagement_id"]
    screener.set(PAYEE, "CAP")                              # cap 10 USDC < 25 USDC total
    resp = _hire(client, eid)
    assert resp.status_code == 403 and "10.00 USDC" in resp.get_json()["error"]
    assert Approval.query.count() == 0
    assert client.get(f"/api/engagements/{eid}").get_json()["status"] == "scoped"


def test_screening_unavailable_fails_closed(client, world_idp, agent_public_id, monkeypatch):
    monkeypatch.setitem(sys.modules, "app.screening.service", None)   # import fails
    eid = _engagement(client, agent_public_id)["engagement_id"]
    resp = _hire(client, eid)
    assert resp.status_code == 403 and resp.get_json()["screening"]["fail_closed"] is True
    # a fail-closed refusal is not a judgement on the payee: the job stays hireable
    assert client.get(f"/api/engagements/{eid}").get_json()["status"] == "scoped"


def test_screener_without_key_fails_closed(client, world_idp, agent_public_id, monkeypatch):
    monkeypatch.delenv("INTERCEPTA_API_KEY", raising=False)
    eid = _engagement(client, agent_public_id)["engagement_id"]
    resp = _hire(client, eid)
    assert resp.status_code == 403 and resp.get_json()["code"] == "SCREENING_REFUSED"


def test_approval_service_unavailable_is_503(client, screener, agent_public_id, monkeypatch):
    monkeypatch.setitem(sys.modules, "app.approvals.service", None)
    eid = _engagement(client, agent_public_id)["engagement_id"]
    resp = _hire(client, eid)
    assert resp.status_code == 503 and resp.get_json()["code"] == "APPROVALS_UNAVAILABLE"


def test_double_fund_is_rejected(client, approve, screener, agent_public_id):
    eid = _engagement(client, agent_public_id)["engagement_id"]
    first = _hire(client, eid).get_json()
    second = _hire(client, eid).get_json()
    assert first["approval_id"] != second["approval_id"]
    assert approve(first["approval_id"]).state == "consumed"
    row = approve(second["approval_id"])
    assert row.state == "failed" and "already funded" in row.failure_detail
    with pytest.raises(ApprovalError) as exc:
        approvals.consume(first["approval_id"], kind="engagement.fund")
    assert exc.value.code == "APPROVAL_CONSUMED"
    assert [e.kind for e in _ledger(eid)] == ["fund"]
    resp = _hire(client, eid)
    assert resp.status_code == 409 and resp.get_json()["code"] == "NOT_HIREABLE"


def test_fund_executor_rejects_changed_terms(client, approve, screener, agent_public_id):
    from app.models import Agent
    eid = _engagement(client, agent_public_id)["engagement_id"]
    apr = _hire(client, eid).get_json()
    agent = Agent.query.filter_by(public_id=agent_public_id).one()
    agent.payout_address = "0x" + "d" * 40          # payee swapped after approval
    _db.session.commit()
    row = approve(apr["approval_id"])
    assert row.state == "failed" and "payee" in row.failure_detail and not _ledger(eid)


def test_rescreen_refusal_at_consume_blocks_funding(client, approve, screener, agent_public_id):
    eid = _engagement(client, agent_public_id)["engagement_id"]
    apr = _hire(client, eid).get_json()
    screener.set(PAYEE, "REFUSE")                  # payee turns bad after the approval
    row = approve(apr["approval_id"])
    assert (row.state, row.failure_code) == ("blocked", "SCREENING_REFUSED")
    assert not _ledger(eid)


# ── submit / release ──────────────────────────────────────────────────────
def test_release_needs_a_fresh_approval_each_time(client, approve, screener, agent_public_id):
    eid = _funded(client, approve, agent_public_id)
    sub = client.post(f"/api/engagements/{eid}/milestones/0/submit", json={"evidence": "PR #42"})
    assert sub.status_code == 200 and sub.get_json()["milestone"]["status"] == "submitted"
    assert sub.get_json()["evidence_hash"].startswith("0x")

    a = _release(client, eid, 0, flow="device")
    assert a.status_code == 202
    a = a.get_json()
    assert a["kind"] == "milestone.release" and a["milestone_idx"] == 0
    assert screener.calls[-1]["hop"] == "milestone.release"
    assert client.get(f"/api/engagements/{eid}").get_json()["milestones"][0]["status"] == "submitted"
    assert approve(a["approval_id"]).state == "consumed"

    body = client.get(f"/api/engagements/{eid}").get_json()
    assert body["milestones"][0]["status"] == "released" and body["status"] == "in_progress"
    release = [e for e in body["ledger"] if e["kind"] == "release"]
    assert [(e["amount_micro"], e["to"]) for e in release] == [(10_000_000, PAYEE)]

    # the same approval can't be used again, nor replayed through the executor
    with pytest.raises(ApprovalError) as exc:
        approvals.consume(a["approval_id"], kind="milestone.release")
    assert exc.value.code == "APPROVAL_CONSUMED"
    from app.models import Approval
    row = _db.session.get(Approval, a["approval_id"])
    assert not get_executor("milestone.release")(row, row.action).ok
    assert _release(client, eid, 0).status_code == 409

    # milestone #2 needs its own approval
    b = _release(client, eid, 1, flow="web").get_json()
    assert b["approval_id"] != a["approval_id"] and b["milestone_idx"] == 1
    assert approve(b["approval_id"]).state == "consumed"
    body = client.get(f"/api/engagements/{eid}").get_json()
    assert body["status"] == "completed"
    assert sum(e["amount_micro"] for e in body["ledger"] if e["kind"] == "release") == 25_000_000


def test_release_refused_by_screening(client, approve, screener, agent_public_id):
    from app.models import Approval
    eid = _funded(client, approve, agent_public_id)
    screener.set(PAYEE, "REFUSE")
    resp = _release(client, eid, 0)
    assert resp.status_code == 403 and resp.get_json()["code"] == "SCREENING_REFUSED"
    assert Approval.query.filter_by(kind="milestone.release").count() == 0
    assert [e.kind for e in _ledger(eid)] == ["fund"]
    assert client.get(f"/api/engagements/{eid}").get_json()["status"] == "funded"


def test_cap_releases_partially_and_holds_the_rest(client, approve, screener, agent_public_id):
    eid = _funded(client, approve, agent_public_id)
    screener.cap_micro = 4_000_000
    screener.set(PAYEE, "CAP")
    a = _release(client, eid, 1).get_json()
    assert ["Amount", "4.00 USDC"] in a["summary"]
    row = approve(a["approval_id"])
    result = approvals.result_of(row)
    assert row.state == "consumed" and "11.00 USDC held" in result["summary"]
    assert len(result["ledger_ids"]) == 2
    body = client.get(f"/api/engagements/{eid}").get_json()
    m = body["milestones"][1]
    assert (m["status"], m["released_micro"]) == ("held", 4_000_000)
    kinds = [(e["kind"], e["amount_micro"]) for e in body["ledger"]]
    assert kinds == [("fund", 25_000_000), ("release", 4_000_000), ("hold", 11_000_000)]

    screener.set(PAYEE, "PAY")      # the rest needs another approval
    b = _release(client, eid, 1).get_json()
    assert ["Amount", "11.00 USDC"] in b["summary"]
    assert approve(b["approval_id"]).state == "consumed"
    m = client.get(f"/api/engagements/{eid}").get_json()["milestones"][1]
    assert (m["status"], m["released_micro"]) == ("released", 15_000_000)


def test_submit_requires_funding_and_evidence(client, approve, screener, agent_public_id):
    eid = _engagement(client, agent_public_id)["engagement_id"]
    resp = client.post(f"/api/engagements/{eid}/milestones/0/submit", json={"evidence": "done"})
    assert resp.status_code == 409
    eid = _funded(client, approve, agent_public_id)
    assert client.post(f"/api/engagements/{eid}/milestones/0/submit", json={}).status_code == 400
    assert client.post(f"/api/engagements/{eid}/milestones/9/submit",
                       json={"evidence": "x"}).status_code == 404


# ── receipts (on-chain escrow faked) ──────────────────────────────────────
@pytest.fixture()
def onchain_escrow(app):
    from tests.fakes.escrow import FakeEscrow
    escrow = FakeEscrow(mode="onchain")
    app.extensions["agents_list.escrow"] = escrow
    return escrow


def test_receipt_poll_pending_then_confirmed(client, approve, screener, onchain_escrow,
                                             agent_public_id):
    eid = _funded(client, approve, agent_public_id)
    [fund] = client.get(f"/api/engagements/{eid}").get_json()["ledger"]
    assert fund["status"] == "confirmed"          # FakeEscrow confirms by default
    assert fund["explorer"].endswith(fund["tx_hash"])
    assert onchain_escrow.calls[0][1]["ref"] == f"{eid}:fund#0"

    eid2 = _funded(client, approve, agent_public_id)
    tx_hash = _ledger(eid2)[0].tx_hash
    onchain_escrow.statuses[tx_hash] = "pending"
    assert client.get(f"/api/engagements/{eid2}").get_json()["ledger"][0]["status"] == "pending"
    onchain_escrow.statuses[tx_hash] = "confirmed"
    assert client.get(f"/api/engagements/{eid2}").get_json()["ledger"][0]["status"] == "confirmed"


def test_failed_fund_receipt_rolls_back(client, approve, screener, onchain_escrow,
                                        agent_public_id):
    eid = _funded(client, approve, agent_public_id)
    onchain_escrow.statuses[_ledger(eid)[0].tx_hash] = "failed"
    body = client.get(f"/api/engagements/{eid}").get_json()
    assert body["status"] == "scoped" and body["ledger"][0]["status"] == "failed"
    assert {m["status"] for m in body["milestones"]} == {"pending"}


def test_failed_release_receipt_rolls_back_release_and_hold(client, approve, screener,
                                                            onchain_escrow, agent_public_id):
    eid = _funded(client, approve, agent_public_id)
    screener.cap_micro = 4_000_000
    screener.set(PAYEE, "CAP")
    client.post(f"/api/engagements/{eid}/milestones/0/submit", json={"evidence": "done"})
    a = _release(client, eid, 0).get_json()
    assert approve(a["approval_id"]).state == "consumed"
    release = next(e for e in _ledger(eid) if e.kind == "release")
    onchain_escrow.statuses[release.tx_hash] = "failed"
    body = client.get(f"/api/engagements/{eid}").get_json()
    assert {e["kind"]: e["status"] for e in body["ledger"]} == {
        "fund": "confirmed", "release": "failed", "hold": "failed"}
    m = body["milestones"][0]
    assert (m["status"], m["released_micro"], m["released_ledger_id"]) == ("submitted", 0, None)
    # it can be released again with a new approval, under a new escrow ref
    b = _release(client, eid, 0).get_json()
    assert approve(b["approval_id"]).state == "consumed"
    refs = [kw["ref"] for name, kw in onchain_escrow.calls if name == "release"]
    assert refs == [f"{eid}:m0:release@0#0", f"{eid}:m0:release@0#1"]


# ── mandate hook ──────────────────────────────────────────────────────────
def test_root_mandate_issued_after_funding(client, approve, screener, human, agent_public_id):
    from app.models import Mandate
    eid = _funded(client, approve, agent_public_id)
    body = client.get(f"/api/engagements/{eid}").get_json()
    row = _db.session.get(Mandate, body["mandate"]["mandate_id"])
    assert row.budget_micro == 25_000_000 and row.grantee_agent_public_id == agent_public_id
    assert row.categories == ["Development"] and row.human_id == human.id
    assert body["chain_url"] == f"/api/engagements/{eid}/chain"
    assert client.get(body["chain_url"]).status_code == 200
    # idempotent
    assert client.get(f"/api/engagements/{eid}").get_json()["mandate"]["mandate_id"] == row.id
    assert Mandate.query.count() == 1


# ── auth ──────────────────────────────────────────────────────────────────
def test_bearer_token_enforced_when_configured(client, screener, agent_public_id, monkeypatch):
    monkeypatch.setenv("MCP_API_TOKEN", "s3cret")
    assert _create(client, agent_public_id).status_code == 401
    resp = client.post("/api/engagements", headers={"Authorization": "Bearer s3cret"},
                       json={"agent_id": agent_public_id, "outcome": "x", "budget_usdc": 1})
    assert resp.status_code == 201


# ── pages ─────────────────────────────────────────────────────────────────
def test_agent_page_hire_links_into_jobs(client, agent):
    html = client.get(f"/agent/{agent}").get_data(as_text=True)
    assert f'href="/jobs/new?agent={agent}"' in html


def test_jobs_pages(client, approve, screener, agent):
    resp = client.post(f"/jobs/new?agent={agent}",
                       data={"outcome": "Write docs", "budget_usdc": "12", "deadline": "",
                             "milestones": "Draft | Reviewed | 5\nFinal | Merged | 7"})
    assert resp.status_code == 302
    eid = resp.headers["Location"].rsplit("/", 1)[-1]
    detail = client.get(f"/jobs/{eid}").get_data(as_text=True)
    assert "Approve &amp; fund 12.00 USDC" in detail and "Approve &amp; release" not in detail

    hire = client.post(f"/jobs/{eid}/hire")
    assert hire.status_code == 302 and hire.headers["Location"].startswith("/approvals/APR-")
    assert approve(hire.headers["Location"].rsplit("/", 1)[-1]).state == "consumed"
    detail = client.get(f"/jobs/{eid}").get_data(as_text=True)
    assert detail.count("Approve &amp; release") == 2
    assert "simulated, not on chain" in detail and "5.00 USDC" in detail

    rel = client.post(f"/jobs/{eid}/milestones/0/release")
    assert rel.status_code == 302 and rel.headers["Location"].startswith("/approvals/APR-")
    listing = client.get("/jobs").get_data(as_text=True)
    assert eid in listing and "12.00 USDC" in listing

    bad = client.post(f"/jobs/new?agent={agent}", data={"outcome": "x", "budget_usdc": "12",
                                                        "milestones": "only | two"})
    assert bad.status_code == 400 and "title | acceptance | amount" in bad.get_data(as_text=True)
    assert client.get("/jobs/ENG-NOPE").status_code == 404


def test_jobs_page_shows_refusal(client, screener, agent):
    resp = client.post(f"/jobs/new?agent={agent}", data={"outcome": "x", "budget_usdc": "3"})
    eid = resp.headers["Location"].rsplit("/", 1)[-1]
    screener.set(PAYEE, "REFUSE")
    page = client.post(f"/jobs/{eid}/hire")
    assert page.status_code == 403 and "refused by risk screening" in page.get_data(as_text=True)
