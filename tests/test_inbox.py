"""Inbox: a funded job for the research demo agent writes a Delivery that
shows up in the buyer's Inbox once its available_at has passed."""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.extensions import db as _db
from app.inbox import service as inbox
from tests.test_engagements import _mandate_key, approve, screener  # noqa: F401  (fixtures)


@pytest.fixture()
def ledgerline(db):
    from app.models import Agent
    from app.seller.stamp import dev_stamp
    from tests.conftest import WALLET
    row = Agent(name=inbox.DEMO_AGENT_NAME, description="Equity research", category="Finance",
                billing="per_token", seller=WALLET, deployer_wallet=WALLET)
    db.session.add(row)
    db.session.commit()
    dev_stamp(row)
    db.session.commit()
    return row


def _login(client, human):
    with client.session_transaction() as s:
        s["human_id"] = human.id
        s["human_sub"] = human.world_sub


def _job(db, agent, buyer, *, status="funded"):
    from app.models import Engagement, Milestone
    eng = Engagement(agent_id=agent.id, buyer_human_id=buyer.id if buyer else None,
                     outcome="Equity research report on Northwind Robotics", status=status,
                     total_micro=25_000_000, sow_hash="0x" + "ab" * 32)
    db.session.add(eng)
    db.session.flush()
    for i, (title, acc) in enumerate([("Model and valuation", "- DCF built\n- Comps table"),
                                      ("Final report", "PDF delivered")]):
        db.session.add(Milestone(engagement_id=eng.id, idx=i, title=title, acceptance=acc,
                                 amount_micro=12_500_000, status="funded"))
    db.session.commit()
    return eng


@pytest.fixture()
def clock(monkeypatch):
    """Frozen inbox clock; advance with clock.shift(seconds)."""
    from datetime import datetime

    class Clock:
        t = datetime(2026, 9, 27, 12, 0, 0)

        def shift(self, seconds):
            self.t = self.t + timedelta(seconds=seconds)

    c = Clock()
    monkeypatch.setattr(inbox, "now", lambda: c.t)
    return c


def test_funding_the_demo_agent_schedules_a_delivery(client, approve, screener, ledgerline):
    from app.models import Delivery, Engagement
    resp = client.post("/api/engagements", json={
        "agent_id": ledgerline.public_id, "outcome": "Initiate coverage of Northwind Robotics",
        "budget_usdc": "25"})
    assert resp.status_code == 201, resp.get_json()
    eid = resp.get_json()["engagement_id"]
    apr = client.post(f"/api/engagements/{eid}/hire",
                      json={"flow": "device", "confirm_amount_usdc": "25"}).get_json()
    assert approve(apr["approval_id"]).state == "consumed"
    eng = _db.session.get(Engagement, eid)
    assert eng.status == "funded"
    [d] = Delivery.query.filter_by(engagement_id=eid).all()
    assert d.buyer_human_id == eng.buyer_human_id
    assert d.logs[0]["msg"].startswith("Agent's List orchestrator on Akash dispatched SOW " + eid)
    assert d.attestation["sow_hash"] == eng.sow_hash and d.attestation["all_milestones_met"]
    # The job page says the agent is working until the delay has passed.
    assert "is working on this" in client.get(f"/jobs/{eid}").get_data(as_text=True)


def test_other_agents_get_no_delivery(db, agent, human):
    from app.models import Agent
    eng = _job(db, db.session.get(Agent, agent), human)
    assert inbox.schedule(eng) is None


def test_hidden_until_available_then_shown(client, db, ledgerline, human, clock):
    eng = _job(db, ledgerline, human)
    d = inbox.schedule(eng)
    assert inbox.schedule(eng).id == d.id           # idempotent
    _login(client, human)
    html = client.get("/inbox").get_data(as_text=True)
    assert d.title not in html and "Nothing delivered yet" in html
    assert client.get(f"/inbox/{d.id}").status_code == 404
    assert "is working on this" in client.get(f"/jobs/{eng.id}").get_data(as_text=True)

    clock.shift(inbox.delay_seconds() + 1)
    html = client.get("/inbox").get_data(as_text=True)
    assert d.title in html and "Inbox (1)" in html
    page = client.get(f"/inbox/{d.id}").get_data(as_text=True)
    for text in ("SOW completion attestation", inbox.attestation_hash(d.attestation),
                 "DCF built", "Comps table", "Attestation signed", "Download PDF"):
        assert text in page
    assert "Inbox (1)" not in client.get("/inbox").get_data(as_text=True)   # now read
    assert "Delivered to your Inbox" in client.get(f"/jobs/{eng.id}").get_data(as_text=True)


def test_only_the_buyer_sees_their_delivery(client, db, ledgerline, human, clock):
    from app.models import Human
    other = Human(world_sub="0x" + "6" * 64)
    db.session.add(other)
    db.session.commit()
    d = inbox.schedule(_job(db, ledgerline, human))
    clock.shift(60)
    assert "Sign in to see your Inbox" in client.get("/inbox").get_data(as_text=True)
    assert client.get(f"/inbox/{d.id}").status_code == 404
    _login(client, other)
    assert d.title not in client.get("/inbox").get_data(as_text=True)
    assert client.get(f"/inbox/{d.id}").status_code == 404
    assert client.get(f"/inbox/{d.id}/report.pdf").status_code == 404


def test_pdf_download(client, db, ledgerline, human, clock):
    d = inbox.schedule(_job(db, ledgerline, human))
    clock.shift(60)
    _login(client, human)
    resp = client.get(f"/inbox/{d.id}/report.pdf")
    assert resp.status_code == 200 and resp.mimetype == "application/pdf"
    assert resp.data[:5] == b"%PDF-"
    assert d.attestation["deliverable"]["sha256"]


def test_attestation_hash_is_stable():
    a = {"b": 1, "a": [1, 2], "c": {"y": "z", "x": None}}
    b = {"c": {"x": None, "y": "z"}, "a": [1, 2], "b": 1}
    assert inbox.attestation_hash(a) == inbox.attestation_hash(b)
    assert inbox.attestation_hash(a) == \
        "1483d0dbf7e78ce111ff6351c82621491a8d07678a3f3c6f1e086f127126fab0"
    assert inbox.attestation_hash({**a, "b": 2}) != inbox.attestation_hash(a)


def test_dispatch_never_blocks_on_llm_errors(db, ledgerline, human, monkeypatch):
    from app import llm
    monkeypatch.setattr(llm, "LLM_URL", "http://127.0.0.1:9")
    monkeypatch.setattr(llm, "FINANCE_LLM_URL", "http://127.0.0.1:9")
    d = inbox.schedule(_job(db, ledgerline, human))
    assert "dispatched SOW" in d.logs[0]["msg"]
