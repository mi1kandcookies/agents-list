"""Engagements: SOW, hire → fund approval → escrow, submit, release with a
fresh approval each time, screening outcomes, and the /jobs pages.

The approval service (§3) and screener (§4) are swapped for in-process fakes
through sys.modules, exactly where app.engagements.service imports them."""
from __future__ import annotations

import sys
import types
from datetime import datetime, timezone

import pytest

from app.approvals.actions import action_hash, action_nonce, canonical
from app.approvals.executors import get_executor
from app.engagements.sow import build_sow, sow_hash
from app.extensions import db as _db
from tests.conftest import WALLET

WORLD_SUB = "0x" + "5" * 64     # the `human` fixture's sub
PAYEE = WALLET.lower()


class FakeApprovalError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class FakeApprovals:
    """Just enough of app.approvals.service (§3) to drive the executors."""

    def create_approval(self, kind, action, *, flow, engagement_id=None, milestone_id=None,
                        agent_id=None, screening_id=None):
        from app.models import Approval
        row = Approval(id=action["approval_id"], kind=kind, action_json=canonical(action).decode(),
                       action_hash=action_hash(action), nonce=action_nonce(action), flow=flow,
                       expires_at=datetime.fromtimestamp(action["exp"], tz=timezone.utc),
                       engagement_id=engagement_id, milestone_id=milestone_id, agent_id=agent_id,
                       screening_id=screening_id)
        _db.session.add(row)
        _db.session.flush()
        return row

    def start_device(self, approval):
        approval.state, approval.user_code = "pending", "WXYZ-1234"
        approval.verification_uri = "https://idp.test/activate"
        approval.verification_uri_complete = "https://idp.test/activate?user_code=WXYZ-1234"
        return approval

    def approve(self, approval_id, sub=WORLD_SUB):
        from app.models import Approval
        row = _db.session.get(Approval, approval_id)
        row.state, row.human_sub = "approved", sub
        _db.session.commit()

    def consume(self, approval_id, *, kind):
        from app.models import Approval
        row = _db.session.get(Approval, approval_id)
        if row.state == "consumed":
            raise FakeApprovalError("APPROVAL_CONSUMED")
        if row.state != "approved" or row.kind != kind:
            raise FakeApprovalError("NOT_APPROVED")
        if action_hash(row.action) != row.action_hash:
            raise FakeApprovalError("HASH_MISMATCH")
        row.consumed_at = datetime.now(timezone.utc)
        result = get_executor(kind)(row, row.action)
        row.state = "consumed" if result.ok else "failed"
        _db.session.commit()
        return result

    def approve_and_consume(self, approval_id, kind, sub=WORLD_SUB):
        self.approve(approval_id, sub)
        return self.consume(approval_id, kind=kind)


def _install(monkeypatch, dotted: str, module) -> None:
    pkg_name, _, attr = dotted.rpartition(".")
    monkeypatch.setitem(sys.modules, dotted, module)
    monkeypatch.setattr(sys.modules[pkg_name], attr, module, raising=False)


@pytest.fixture()
def approvals(monkeypatch):
    fake = FakeApprovals()
    _install(monkeypatch, "app.approvals.service", fake)
    return fake


@pytest.fixture()
def screener(monkeypatch, fake_screener):
    _install(monkeypatch, "app.screening.service", types.SimpleNamespace(screen=fake_screener.screen))
    return fake_screener


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


def _funded(client, approvals, agent_public_id) -> str:
    eng = _engagement(client, agent_public_id)
    apr = client.post(f"/api/engagements/{eng['engagement_id']}/hire",
                      json={"flow": "device", "confirm_amount_usdc": "25"}).get_json()
    assert approvals.approve_and_consume(apr["approval_id"], "engagement.fund").ok
    return eng["engagement_id"]


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
def test_hire_approve_fund(client, approvals, screener, agent_public_id):
    eng = _engagement(client, agent_public_id)
    eid = eng["engagement_id"]
    resp = client.post(f"/api/engagements/{eid}/hire",
                       json={"flow": "device", "confirm_amount_usdc": "25.00"})
    assert resp.status_code == 202
    apr = resp.get_json()
    assert apr["kind"] == "engagement.fund" and apr["state"] == "pending"
    assert apr["user_code"] == "WXYZ-1234" and apr["action_hash"].startswith("0x")
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

    result = approvals.approve_and_consume(apr["approval_id"], "engagement.fund")
    assert result.ok and result.redirect == f"/jobs/{eid}"
    body = client.get(f"/api/engagements/{eid}").get_json()
    assert body["status"] == "funded" and body["escrow_mode"] == "simulated"
    assert {m["status"] for m in body["milestones"]} == {"funded"}
    [fund] = body["ledger"]
    assert fund["kind"] == "fund" and fund["amount_micro"] == 25_000_000
    assert fund["status"] == "simulated" and fund["simulated"] and fund["explorer"] is None
    assert fund["ledger_id"] in result.ledger_ids
    assert body["approvals"][0]["state"] == "consumed"


def test_hire_confirm_amount_must_match(client, approvals, screener, agent_public_id):
    eid = _engagement(client, agent_public_id)["engagement_id"]
    resp = client.post(f"/api/engagements/{eid}/hire", json={"confirm_amount_usdc": "24.99"})
    assert resp.status_code == 400 and resp.get_json()["code"] == "AMOUNT_MISMATCH"


def test_refuse_blocks_hire_with_no_approval_or_ledger(client, approvals, screener, agent_public_id):
    from app.models import Approval
    eid = _engagement(client, agent_public_id)["engagement_id"]
    screener.set(PAYEE, "REFUSE")
    resp = client.post(f"/api/engagements/{eid}/hire", json={"confirm_amount_usdc": "25"})
    assert resp.status_code == 403
    body = resp.get_json()
    assert body["code"] == "SCREENING_REFUSED" and body["screening"]["verdict"] == "REFUSE"
    assert Approval.query.count() == 0 and not _ledger(eid)
    assert client.get(f"/api/engagements/{eid}").get_json()["status"] == "refused"


def test_client_supplied_screening_is_ignored(client, approvals, screener, agent_public_id):
    from app.models import Approval
    eid = _engagement(client, agent_public_id)["engagement_id"]
    screener.set(PAYEE, "REFUSE")
    forged = {"id": "SCR-000000000000", "verdict": "PAY", "fail_closed": False}
    resp = client.post(f"/api/engagements/{eid}/hire",
                       json={"confirm_amount_usdc": "25", "screening": forged,
                             "screening_id": forged["id"], "verdict": "PAY"})
    assert resp.status_code == 403 and resp.get_json()["code"] == "SCREENING_REFUSED"
    assert Approval.query.count() == 0


def test_client_supplied_screening_is_ignored_on_release(client, approvals, screener,
                                                        agent_public_id):
    eid = _funded(client, approvals, agent_public_id)
    screener.cap_micro = 1_000_000
    screener.set(PAYEE, "CAP")
    apr = client.post(f"/api/engagements/{eid}/milestones/0/release",
                      json={"screening": {"id": "SCR-000000000000", "verdict": "PAY"},
                            "amount_micro": 10_000_000}).get_json()
    assert ["Amount", "1.00 USDC"] in apr["summary"] and apr["screening"]["verdict"] == "CAP"


def test_ask_human_sets_screening_ack(client, approvals, screener, agent_public_id):
    from app.models import Approval
    eid = _engagement(client, agent_public_id)["engagement_id"]
    screener.set(PAYEE, "ASK_HUMAN")
    apr = client.post(f"/api/engagements/{eid}/hire", json={"confirm_amount_usdc": "25"}).get_json()
    assert _db.session.get(Approval, apr["approval_id"]).action["screening_ack"] is True
    assert any("warning acknowledged" in value for _, value in apr["summary"])


def test_screening_unavailable_fails_closed(client, approvals, agent_public_id):
    eid = _engagement(client, agent_public_id)["engagement_id"]
    resp = client.post(f"/api/engagements/{eid}/hire", json={"confirm_amount_usdc": "25"})
    assert resp.status_code == 403 and resp.get_json()["screening"]["fail_closed"] is True
    # a fail-closed refusal is not a judgement on the payee: the job stays hireable
    assert client.get(f"/api/engagements/{eid}").get_json()["status"] == "scoped"


def test_approval_service_unavailable_is_503(client, screener, agent_public_id):
    eid = _engagement(client, agent_public_id)["engagement_id"]
    resp = client.post(f"/api/engagements/{eid}/hire", json={"confirm_amount_usdc": "25"})
    assert resp.status_code == 503 and resp.get_json()["code"] == "APPROVALS_UNAVAILABLE"


def test_double_fund_is_rejected(client, approvals, screener, agent_public_id):
    eid = _engagement(client, agent_public_id)["engagement_id"]
    first = client.post(f"/api/engagements/{eid}/hire", json={"confirm_amount_usdc": "25"}).get_json()
    second = client.post(f"/api/engagements/{eid}/hire", json={"confirm_amount_usdc": "25"}).get_json()
    assert first["approval_id"] != second["approval_id"]
    assert approvals.approve_and_consume(first["approval_id"], "engagement.fund").ok
    result = approvals.approve_and_consume(second["approval_id"], "engagement.fund")
    assert not result.ok and "already funded" in result.summary
    with pytest.raises(FakeApprovalError):
        approvals.consume(first["approval_id"], kind="engagement.fund")
    assert [e.kind for e in _ledger(eid)] == ["fund"]
    resp = client.post(f"/api/engagements/{eid}/hire", json={"confirm_amount_usdc": "25"})
    assert resp.status_code == 409 and resp.get_json()["code"] == "NOT_HIREABLE"


def test_fund_executor_rejects_changed_terms(client, approvals, screener, agent_public_id):
    from app.models import Agent, Approval
    eid = _engagement(client, agent_public_id)["engagement_id"]
    apr = client.post(f"/api/engagements/{eid}/hire", json={"confirm_amount_usdc": "25"}).get_json()
    agent = Agent.query.filter_by(public_id=agent_public_id).one()
    agent.payout_address = "0x" + "d" * 40          # payee swapped after approval
    _db.session.commit()
    result = approvals.approve_and_consume(apr["approval_id"], "engagement.fund")
    assert not result.ok and "payee" in result.summary and not _ledger(eid)
    assert _db.session.get(Approval, apr["approval_id"]).state == "failed"


# ── submit / release ──────────────────────────────────────────────────────
def test_release_needs_a_fresh_approval_each_time(client, approvals, screener, agent_public_id):
    eid = _funded(client, approvals, agent_public_id)
    sub = client.post(f"/api/engagements/{eid}/milestones/0/submit", json={"evidence": "PR #42"})
    assert sub.status_code == 200 and sub.get_json()["milestone"]["status"] == "submitted"
    assert sub.get_json()["evidence_hash"].startswith("0x")

    a = client.post(f"/api/engagements/{eid}/milestones/0/release", json={"flow": "device"})
    assert a.status_code == 202
    a = a.get_json()
    assert a["kind"] == "milestone.release" and a["milestone_idx"] == 0
    assert screener.calls[-1]["hop"] == "milestone.release"
    assert client.get(f"/api/engagements/{eid}").get_json()["milestones"][0]["status"] == "submitted"
    assert approvals.approve_and_consume(a["approval_id"], "milestone.release").ok

    body = client.get(f"/api/engagements/{eid}").get_json()
    assert body["milestones"][0]["status"] == "released" and body["status"] == "in_progress"
    release = [e for e in body["ledger"] if e["kind"] == "release"]
    assert [(e["amount_micro"], e["to"]) for e in release] == [(10_000_000, PAYEE)]

    # the same approval can't be used again, nor replayed through the executor
    with pytest.raises(FakeApprovalError) as exc:
        approvals.consume(a["approval_id"], kind="milestone.release")
    assert exc.value.code == "APPROVAL_CONSUMED"
    from app.models import Approval
    row = _db.session.get(Approval, a["approval_id"])
    assert not get_executor("milestone.release")(row, row.action).ok
    again = client.post(f"/api/engagements/{eid}/milestones/0/release", json={})
    assert again.status_code == 409

    # milestone #2 needs its own approval
    b = client.post(f"/api/engagements/{eid}/milestones/1/release", json={"flow": "web"}).get_json()
    assert b["approval_id"] != a["approval_id"] and b["milestone_idx"] == 1
    assert approvals.approve_and_consume(b["approval_id"], "milestone.release").ok
    body = client.get(f"/api/engagements/{eid}").get_json()
    assert body["status"] == "completed"
    assert sum(e["amount_micro"] for e in body["ledger"] if e["kind"] == "release") == 25_000_000


def test_release_refused_by_screening(client, approvals, screener, agent_public_id):
    from app.models import Approval
    eid = _funded(client, approvals, agent_public_id)
    screener.set(PAYEE, "REFUSE")
    resp = client.post(f"/api/engagements/{eid}/milestones/0/release", json={})
    assert resp.status_code == 403 and resp.get_json()["code"] == "SCREENING_REFUSED"
    assert Approval.query.filter_by(kind="milestone.release").count() == 0
    assert [e.kind for e in _ledger(eid)] == ["fund"]
    assert client.get(f"/api/engagements/{eid}").get_json()["status"] == "funded"


def test_cap_releases_partially_and_holds_the_rest(client, approvals, screener, agent_public_id):
    eid = _funded(client, approvals, agent_public_id)
    screener.cap_micro = 4_000_000
    screener.set(PAYEE, "CAP")
    a = client.post(f"/api/engagements/{eid}/milestones/1/release", json={}).get_json()
    assert ["Amount", "4.00 USDC"] in a["summary"]
    result = approvals.approve_and_consume(a["approval_id"], "milestone.release")
    assert result.ok and "11.00 USDC held" in result.summary and len(result.ledger_ids) == 2
    body = client.get(f"/api/engagements/{eid}").get_json()
    m = body["milestones"][1]
    assert (m["status"], m["released_micro"]) == ("held", 4_000_000)
    kinds = [(e["kind"], e["amount_micro"]) for e in body["ledger"]]
    assert kinds == [("fund", 25_000_000), ("release", 4_000_000), ("hold", 11_000_000)]

    screener.set(PAYEE, "PAY")      # the rest needs another approval
    b = client.post(f"/api/engagements/{eid}/milestones/1/release", json={}).get_json()
    assert ["Amount", "11.00 USDC"] in b["summary"]
    assert approvals.approve_and_consume(b["approval_id"], "milestone.release").ok
    m = client.get(f"/api/engagements/{eid}").get_json()["milestones"][1]
    assert (m["status"], m["released_micro"]) == ("released", 15_000_000)


def test_submit_requires_funding_and_evidence(client, approvals, screener, agent_public_id):
    eid = _engagement(client, agent_public_id)["engagement_id"]
    resp = client.post(f"/api/engagements/{eid}/milestones/0/submit", json={"evidence": "done"})
    assert resp.status_code == 409
    eid = _funded(client, approvals, agent_public_id)
    assert client.post(f"/api/engagements/{eid}/milestones/0/submit", json={}).status_code == 400
    assert client.post(f"/api/engagements/{eid}/milestones/9/submit",
                       json={"evidence": "x"}).status_code == 404


# ── receipts (on-chain escrow faked) ──────────────────────────────────────
def test_receipt_poll_pending_then_confirmed(app, client, approvals, screener, agent_public_id):
    from tests.fakes.escrow import FakeEscrow
    escrow = FakeEscrow(mode="onchain")
    app.extensions["agents_list.escrow"] = escrow
    eid = _funded(client, approvals, agent_public_id)
    [fund] = client.get(f"/api/engagements/{eid}").get_json()["ledger"]
    assert fund["status"] == "confirmed"          # FakeEscrow confirms by default
    assert fund["explorer"].endswith(fund["tx_hash"])
    assert escrow.calls[0][1]["ref"] == f"{eid}:fund#0"

    eid2 = _funded(client, approvals, agent_public_id)
    tx_hash = _ledger(eid2)[0].tx_hash
    escrow.statuses[tx_hash] = "pending"
    assert client.get(f"/api/engagements/{eid2}").get_json()["ledger"][0]["status"] == "pending"
    escrow.statuses[tx_hash] = "confirmed"
    assert client.get(f"/api/engagements/{eid2}").get_json()["ledger"][0]["status"] == "confirmed"


def test_failed_fund_receipt_rolls_back(app, client, approvals, screener, agent_public_id):
    from tests.fakes.escrow import FakeEscrow
    escrow = FakeEscrow(mode="onchain")
    app.extensions["agents_list.escrow"] = escrow
    eid = _funded(client, approvals, agent_public_id)
    escrow.statuses[_ledger(eid)[0].tx_hash] = "failed"
    body = client.get(f"/api/engagements/{eid}").get_json()
    assert body["status"] == "scoped" and body["ledger"][0]["status"] == "failed"
    assert {m["status"] for m in body["milestones"]} == {"pending"}


def test_failed_release_receipt_rolls_back_release_and_hold(app, client, approvals, screener,
                                                            agent_public_id):
    from tests.fakes.escrow import FakeEscrow
    escrow = FakeEscrow(mode="onchain")
    app.extensions["agents_list.escrow"] = escrow
    eid = _funded(client, approvals, agent_public_id)
    screener.cap_micro = 4_000_000
    screener.set(PAYEE, "CAP")
    client.post(f"/api/engagements/{eid}/milestones/0/submit", json={"evidence": "done"})
    a = client.post(f"/api/engagements/{eid}/milestones/0/release", json={}).get_json()
    assert approvals.approve_and_consume(a["approval_id"], "milestone.release").ok
    release = next(e for e in _ledger(eid) if e.kind == "release")
    escrow.statuses[release.tx_hash] = "failed"
    body = client.get(f"/api/engagements/{eid}").get_json()
    assert {e["kind"]: e["status"] for e in body["ledger"]} == {
        "fund": "confirmed", "release": "failed", "hold": "failed"}
    m = body["milestones"][0]
    assert (m["status"], m["released_micro"], m["released_ledger_id"]) == ("submitted", 0, None)
    # it can be released again with a new approval, under a new escrow ref
    b = client.post(f"/api/engagements/{eid}/milestones/0/release", json={}).get_json()
    assert approvals.approve_and_consume(b["approval_id"], "milestone.release").ok
    refs = [kw["ref"] for name, kw in escrow.calls if name == "release"]
    assert refs == [f"{eid}:m0:release@0#0", f"{eid}:m0:release@0#1"]


# ── mandate hook ──────────────────────────────────────────────────────────
def test_root_mandate_issued_after_funding(app, client, approvals, screener, human, agent_public_id):
    from app.mandates import tokens
    from app.models import Mandate
    app.config["MANDATE_SIGNING_KEY"] = tokens.generate_pem()
    eid = _funded(client, approvals, agent_public_id)
    body = client.get(f"/api/engagements/{eid}").get_json()
    row = _db.session.get(Mandate, body["mandate"]["mandate_id"])
    assert row.budget_micro == 25_000_000 and row.grantee_agent_public_id == agent_public_id
    assert row.categories == ["Development"] and row.human_id == human.id
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


def test_jobs_pages(client, approvals, screener, agent):
    resp = client.post(f"/jobs/new?agent={agent}",
                       data={"outcome": "Write docs", "budget_usdc": "12", "deadline": "",
                             "milestones": "Draft | Reviewed | 5\nFinal | Merged | 7"})
    assert resp.status_code == 302
    eid = resp.headers["Location"].rsplit("/", 1)[-1]
    detail = client.get(f"/jobs/{eid}").get_data(as_text=True)
    assert "Approve &amp; fund 12.00 USDC" in detail and "Approve &amp; release" not in detail

    hire = client.post(f"/jobs/{eid}/hire")
    assert hire.status_code == 302 and hire.headers["Location"].startswith("/approvals/APR-")
    assert approvals.approve_and_consume(hire.headers["Location"].rsplit("/", 1)[-1],
                                         "engagement.fund").ok
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


def test_jobs_page_shows_refusal(client, approvals, screener, agent):
    resp = client.post(f"/jobs/new?agent={agent}", data={"outcome": "x", "budget_usdc": "3"})
    eid = resp.headers["Location"].rsplit("/", 1)[-1]
    screener.set(PAYEE, "REFUSE")
    page = client.post(f"/jobs/{eid}/hire")
    assert page.status_code == 403 and "refused by risk screening" in page.get_data(as_text=True)
