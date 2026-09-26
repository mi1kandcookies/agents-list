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
    """The test agent, operator-stamped so it is hireable (see test_stamp_bans)."""
    from app.models import Agent
    from app.seller.stamp import dev_stamp
    row = db.session.get(Agent, agent)
    dev_stamp(row)
    db.session.commit()
    return row.public_id


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
    assert build_sow(**args)["outcome"] == "Ship the\nexport"   # whitespace-normalized
    assert first == sow_hash(build_sow(**{**args, "outcome": " Ship the \n\n export "}))
    for change in ({"budget_micro": 25_000_001}, {"deadline": 1_900_000_001},
                   {"milestones": [{"title": "B", "acceptance": "a", "amount_micro": 25_000_000}]}):
        assert sow_hash(build_sow(**{**args, **change})) != first


SOURCE = {"filename": "competitor-map.pdf", "sha256": "ab" * 32}


def test_source_document_is_part_of_the_sow_hash():
    args = dict(agent_public_id="AGT-SH8W-D5VP-P", outcome="Ship the export", budget_micro=25_000_000)
    plain = build_sow(**args)
    assert "source_document" not in plain           # unchanged for jobs typed by hand
    with_doc = build_sow(**args, source_document=SOURCE)
    assert with_doc["source_document"] == SOURCE
    assert sow_hash(with_doc) != sow_hash(plain)
    other = build_sow(**args, source_document={**SOURCE, "sha256": "cd" * 32})
    assert sow_hash(other) != sow_hash(with_doc)
    # a path in the filename is reduced to its basename; the digest is lowercased
    assert build_sow(**args, source_document={"filename": "C:\\docs\\competitor-map.pdf",
                                              "sha256": "AB" * 32})["source_document"] == SOURCE


def test_engagement_from_uploaded_sow_binds_the_document(client, world_idp, screener, agent_public_id):
    plain = _engagement(client, agent_public_id)
    body = _engagement(client, agent_public_id, source_document=SOURCE)
    assert body["sow"]["source_document"] == SOURCE
    assert body["sow_hash"] == sow_hash(body["sow"]) != plain["sow_hash"]
    apr = _hire(client, body["engagement_id"]).get_json()
    from app.models import Approval
    action = _db.session.get(Approval, apr["approval_id"]).action
    assert action["sow_hash"] == body["sow_hash"]   # World ID approves this exact document


@pytest.mark.parametrize("source, field", [
    ({"filename": "a.pdf", "sha256": "xyz"}, "source_document.sha256"),
    ({"filename": "", "sha256": "ab" * 32}, "source_document.filename"),
    ("a.pdf", "source_document"),
])
def test_source_document_validation(client, screener, agent_public_id, source, field):
    resp = _create(client, agent_public_id, source_document=source)
    assert resp.status_code == 400 and resp.get_json()["field"] == field


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


def test_numeric_agent_id_and_date_deadline(client, screener, agent, agent_public_id):
    import time
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    body = _engagement(client, agent_public_id, agent_id=agent, deadline=today,
                       milestones=[{"title": "All", "acceptance": "- one\n- two", "amount_usdc": 25}])
    assert body["agent_id"] == agent_public_id
    assert time.time() < body["deadline_at"] <= time.time() + 86_400   # end of today, UTC
    assert body["milestones"][0]["acceptance"] == "- one\n- two"
    assert _create(client, agent_public_id, agent_id=str(agent)).status_code == 201
    assert _create(client, agent_public_id, agent_id=agent + 999).status_code == 404


def test_api_agents_lists_agt_ids(client, agent, agent_public_id):
    [row] = client.get("/api/agents").get_json()["agents"]
    assert row["agent_id"] == agent_public_id and row["id"] == agent


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
    assert ["Amount", "25 USDC"] in apr["summary"]
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
    assert ["Amount", "1 USDC"] in apr["summary"] and apr["screening"]["verdict"] == "CAP"


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
    assert resp.status_code == 403 and "10 USDC" in resp.get_json()["error"]
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


def test_fund_executor_rejects_changed_terms(client, approve, screener, agent_public_id,
                                            monkeypatch):
    from app.approvals.executors import BEFORE_CONSUME
    from app.models import Agent
    # A payee swap also voids the stamp, which the pre-consume check blocks
    # (next test); switch that off to exercise the executor's own check.
    monkeypatch.setitem(BEFORE_CONSUME, "engagement.fund", [])
    eid = _engagement(client, agent_public_id)["engagement_id"]
    apr = _hire(client, eid).get_json()
    agent = Agent.query.filter_by(public_id=agent_public_id).one()
    agent.payout_address = "0x" + "d" * 40          # payee swapped after approval
    _db.session.commit()
    row = approve(apr["approval_id"])
    assert row.state == "failed" and "payee" in row.failure_detail and not _ledger(eid)


def test_agent_edited_during_approval_blocks_funding(client, approve, screener, agent_public_id):
    """The stamp is re-checked at consume: an edit (or a ban) while the
    approval is open stops funding before the executor runs."""
    from app.models import Agent
    eid = _engagement(client, agent_public_id)["engagement_id"]
    apr = _hire(client, eid).get_json()
    agent = Agent.query.filter_by(public_id=agent_public_id).one()
    agent.payout_address = "0x" + "d" * 40          # not re-stamped
    _db.session.commit()
    row = approve(apr["approval_id"])
    assert (row.state, row.failure_code) == ("blocked", "RESTAMP_REQUIRED")
    assert row.consumed_at is None and not _ledger(eid)
    assert client.get(f"/api/engagements/{eid}").get_json()["status"] == "scoped"


def test_operator_banned_during_approval_blocks_funding(client, approve, screener,
                                                        agent_public_id, human):
    from app.humans import service as humans
    from app.models import Agent, Screening
    agent = Agent.query.filter_by(public_id=agent_public_id).one()
    agent.manifest_stamp_sub = human.world_sub       # this human operates the agent,
    _db.session.add(Screening(hop="payee.onboard", agent_id=agent.id,   # payee screened PAY
                              chain_address=PAYEE, verdict="PAY"))
    _db.session.commit()
    eid = _engagement(client, agent_public_id)["engagement_id"]
    apr = _hire(client, eid).get_json()
    humans.ban(human, "fraud")                       # banned while the approval is open
    _db.session.commit()
    row = approve(apr["approval_id"], sub="0x" + "8" * 64)   # a different buyer approves
    assert (row.state, row.failure_code) == ("blocked", "OPERATOR_BANNED") and not _ledger(eid)


def test_rescreen_refusal_at_consume_blocks_funding(client, approve, screener, agent_public_id):
    eid = _engagement(client, agent_public_id)["engagement_id"]
    apr = _hire(client, eid).get_json()
    screener.set(PAYEE, "REFUSE")                  # payee turns bad after the approval
    row = approve(apr["approval_id"])
    assert (row.state, row.failure_code) == ("blocked", "SCREENING_REFUSED")
    assert not _ledger(eid)


def test_bound_payer_screening_is_rechecked_before_funding(client, approve, screener,
                                                            agent_public_id, db):
    from app.models import Approval, Engagement

    payer = "0x" + "c" * 40
    eid = _engagement(client, agent_public_id)["engagement_id"]
    eng = db.session.get(Engagement, eid)
    eng.buyer_address = payer
    db.session.commit()

    apr = _hire(client, eid).get_json()
    action = db.session.get(Approval, apr["approval_id"]).action
    assert action["payer_screening_id"].startswith("SCR-")
    screener.set(payer, "REFUSE")
    row = approve(apr["approval_id"])
    assert (row.state, row.failure_code) == ("blocked", "SCREENING_REFUSED")
    assert not _ledger(eid)


def test_bound_payer_screening_is_rechecked_before_release(client, approve, screener,
                                                            agent_public_id, db):
    from app.models import Approval, Engagement

    eid = _funded(client, approve, agent_public_id)
    payer = "0x" + "d" * 40
    eng = db.session.get(Engagement, eid)
    eng.buyer_address = payer
    db.session.commit()
    assert client.post(f"/api/engagements/{eid}/milestones/0/submit",
                       json={"evidence": "delivered"}).status_code == 200

    apr = _release(client, eid, 0).get_json()
    action = db.session.get(Approval, apr["approval_id"]).action
    assert action["payer_screening_id"].startswith("SCR-")
    screener.set(payer, "REFUSE")
    row = approve(apr["approval_id"])
    assert (row.state, row.failure_code) == ("blocked", "SCREENING_REFUSED")
    assert not [entry for entry in _ledger(eid) if entry.kind == "release"]


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
    assert ["Amount", "4 USDC"] in a["summary"]
    row = approve(a["approval_id"])
    result = approvals.result_of(row)
    assert row.state == "consumed" and "11 USDC held" in result["summary"]
    assert len(result["ledger_ids"]) == 2
    body = client.get(f"/api/engagements/{eid}").get_json()
    m = body["milestones"][1]
    assert (m["status"], m["released_micro"]) == ("held", 4_000_000)
    kinds = [(e["kind"], e["amount_micro"]) for e in body["ledger"]]
    assert kinds == [("fund", 25_000_000), ("release", 4_000_000), ("hold", 11_000_000)]

    screener.set(PAYEE, "PAY")      # the rest needs another approval
    b = _release(client, eid, 1).get_json()
    assert ["Amount", "11 USDC"] in b["summary"]
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


def test_same_origin_browser_allowed_without_token(client, screener, agent_public_id, monkeypatch):
    """Our own pages call the JSON API from the browser without the MCP token."""
    monkeypatch.setenv("MCP_API_TOKEN", "s3cret")
    body = {"agent_id": agent_public_id, "outcome": "x", "budget_usdc": 1}
    ok = client.post("/api/engagements", json=body, headers={"Sec-Fetch-Site": "same-origin"})
    assert ok.status_code == 201
    eid = ok.get_json()["engagement_id"]
    # ...but a same-origin browser never receives the hired agent's mandate token
    got = client.get(f"/api/engagements/{eid}", headers={"Sec-Fetch-Site": "same-origin"}).get_json()
    assert "mandate_token" not in got
    # Origin fallback (browsers without Sec-Fetch-Site)
    assert client.post("/api/engagements", json=body,
                       headers={"Origin": "http://localhost"}).status_code == 201


def test_cross_site_browser_still_needs_token(client, screener, agent_public_id, monkeypatch):
    monkeypatch.setenv("MCP_API_TOKEN", "s3cret")
    body = {"agent_id": agent_public_id, "outcome": "x", "budget_usdc": 1}
    assert client.post("/api/engagements", json=body,
                       headers={"Sec-Fetch-Site": "cross-site"}).status_code == 401
    assert client.post("/api/engagements", json=body,
                       headers={"Origin": "https://evil.example"}).status_code == 401
    # a same-site subdomain is not our origin either
    assert client.post("/api/engagements", json=body,
                       headers={"Sec-Fetch-Site": "same-site"}).status_code == 401


# ── pages ─────────────────────────────────────────────────────────────────
def test_agent_page_hire_links_into_jobs(client, agent):
    html = client.get(f"/agent/{agent}").get_data(as_text=True)
    assert f'href="/jobs/new?agent={agent}"' in html


def test_job_form_carries_the_uploaded_document(client, screener, agent, agent_public_id):
    resp = client.post(f"/jobs/new?agent={agent}",
                       data={"outcome": "Write docs", "budget_usdc": "12",
                             "source_filename": "docs-sow.docx", "source_sha256": "ef" * 32})
    assert resp.status_code == 302
    eid = resp.headers["Location"].rsplit("/", 1)[-1]
    sow = client.get(f"/api/engagements/{eid}").get_json()["sow"]
    assert sow["source_document"] == {"filename": "docs-sow.docx", "sha256": "ef" * 32}
    page = client.get(f"/estimate/{eid}").get_data(as_text=True)
    assert "Drafted from" in page and "docs-sow.docx" in page


def test_jobs_pages(client, approve, screener, agent, agent_public_id):
    resp = client.post(f"/jobs/new?agent={agent}",
                       data={"outcome": "Write docs", "budget_usdc": "12", "deadline": "",
                             "milestones": "Draft | Reviewed | 5\nFinal | Merged | 7"})
    assert resp.status_code == 302
    eid = resp.headers["Location"].rsplit("/", 1)[-1]
    detail = client.get(f"/jobs/{eid}").get_data(as_text=True)
    assert "Approve &amp; fund 12 USDC" in detail and "Approve &amp; release" not in detail

    hire = client.post(f"/jobs/{eid}/hire")
    assert hire.status_code == 302 and hire.headers["Location"].startswith("/approvals/APR-")
    assert approve(hire.headers["Location"].rsplit("/", 1)[-1]).state == "consumed"
    detail = client.get(f"/jobs/{eid}").get_data(as_text=True)
    assert detail.count("Approve &amp; release") == 2
    assert "simulated, not on chain" in detail and "5 USDC" in detail

    rel = client.post(f"/jobs/{eid}/milestones/0/release")
    assert rel.status_code == 302 and rel.headers["Location"].startswith("/approvals/APR-")
    listing = client.get("/jobs").get_data(as_text=True)
    assert eid in listing and "12 USDC" in listing

    bad = client.post(f"/jobs/new?agent={agent}", data={"outcome": "x", "budget_usdc": "12",
                                                        "milestones": "only | two"})
    assert bad.status_code == 400 and "title | acceptance | amount" in bad.get_data(as_text=True)
    assert client.get("/jobs/ENG-NOPE").status_code == 404


def test_jobs_page_shows_refusal(client, screener, agent, agent_public_id):
    resp = client.post(f"/jobs/new?agent={agent}", data={"outcome": "x", "budget_usdc": "3"})
    eid = resp.headers["Location"].rsplit("/", 1)[-1]
    screener.set(PAYEE, "REFUSE")
    page = client.post(f"/jobs/{eid}/hire")
    assert page.status_code == 403 and "refused by risk screening" in page.get_data(as_text=True)


# ── fund approval ends without executing → job back to scoped ─────────────
def _status(client, eid):
    return client.get(f"/api/engagements/{eid}").get_json()["status"]


def test_denied_fund_approval_returns_job_to_scoped(client, screener, agent_public_id,
                                                    world_idp, approve):
    from app.models import Approval
    eid = _engagement(client, agent_public_id)["engagement_id"]
    apr = _hire(client, eid).get_json()
    assert _status(client, eid) == "awaiting_approval"
    row = _db.session.get(Approval, apr["approval_id"])
    world_idp.deny_device(row.device_code)
    assert approvals.poll(row).state == "denied"
    assert _status(client, eid) == "scoped"
    # … and it can be hired again
    again = _hire(client, eid).get_json()
    assert approve(again["approval_id"]).state == "consumed"
    assert _status(client, eid) == "funded"


def test_cancelled_fund_approval_returns_job_to_scoped(client, world_idp, screener,
                                                       agent_public_id):
    eid = _engagement(client, agent_public_id)["engagement_id"]
    apr = _hire(client, eid).get_json()
    approvals.cancel(apr["approval_id"], "changed my mind")
    assert _status(client, eid) == "scoped"


def test_expired_fund_approval_returns_job_to_scoped_on_view(client, world_idp, screener,
                                                             agent_public_id, monkeypatch):
    import time
    eid = _engagement(client, agent_public_id)["engagement_id"]
    apr = _hire(client, eid).get_json()
    later = time.time() + 3600
    monkeypatch.setattr(approvals, "clock", lambda: later)
    assert _status(client, eid) == "scoped"            # nobody polled the approval
    assert approvals.get(apr["approval_id"]).state == "expired"


def test_blocked_fund_approval_returns_job_to_scoped(client, approve, screener, agent_public_id):
    eid = _engagement(client, agent_public_id)["engagement_id"]
    apr = _hire(client, eid).get_json()
    screener.set(PAYEE, "REFUSE")
    assert approve(apr["approval_id"]).state == "blocked"
    assert _status(client, eid) == "scoped"


def test_ending_one_fund_approval_keeps_another_open_one(client, world_idp, screener,
                                                       agent_public_id):
    eid = _engagement(client, agent_public_id)["engagement_id"]
    first = _hire(client, eid).get_json()
    _hire(client, eid)                                 # a second, still pending
    approvals.cancel(first["approval_id"], "superseded")
    assert _status(client, eid) == "awaiting_approval"


# ── root mandate retry ────────────────────────────────────────────────────
def test_missing_root_mandate_is_retried_on_view(client, approve, screener, agent_public_id,
                                                 monkeypatch):
    from app.mandates import service as mandates
    from app.models import Engagement, Mandate
    real = mandates.issue_root

    def down(*a, **kw):
        raise RuntimeError("signing key unavailable")
    monkeypatch.setattr(mandates, "issue_root", down)
    eid = _funded(client, approve, agent_public_id)
    assert _db.session.get(Engagement, eid).mandate_id is None
    assert client.get(f"/api/engagements/{eid}").get_json()["mandate"] is None   # still failing

    monkeypatch.setattr(mandates, "issue_root", real)
    body = client.get(f"/api/engagements/{eid}").get_json()
    assert body["status"] == "funded" and body["mandate"]["mandate_id"].startswith("MND-")
    client.get(f"/api/engagements/{eid}")
    assert client.get(f"/jobs/{eid}").status_code == 200
    assert Mandate.query.filter_by(engagement_id=eid).count() == 1        # minted once


def test_job_page_retries_the_root_mandate(client, approve, screener, agent_public_id,
                                           monkeypatch):
    from app.mandates import service as mandates
    from app.models import Engagement
    real = mandates.issue_root
    monkeypatch.setattr(mandates, "issue_root",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("down")))
    eid = _funded(client, approve, agent_public_id)
    monkeypatch.setattr(mandates, "issue_root", real)
    assert client.get(f"/jobs/{eid}").status_code == 200
    _db.session.expire_all()
    assert _db.session.get(Engagement, eid).mandate_id


def test_no_mandate_retry_unless_funded(client, screener, agent_public_id, monkeypatch):
    from app.engagements import service as svc
    calls = []
    monkeypatch.setattr(svc, "ensure_root_mandate", lambda eng: calls.append(eng.id))
    eid = _engagement(client, agent_public_id)["engagement_id"]
    client.get(f"/api/engagements/{eid}")
    assert calls == []
