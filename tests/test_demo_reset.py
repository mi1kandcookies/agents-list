"""`flask reset-demo`: wipe jobs and non-demo agents, reseed the demo agents
and three example jobs with a consistent simulated ledger."""
from datetime import datetime, timedelta, timezone

import pytest

from app.demo_reset import DEMO_JOBS
from app.demo_seed import DEMO_AGENTS, DEMO_OPERATOR, seed_demo_agents

DEAD = "0x000000000000000000000000000000000000dEaD"


@pytest.fixture()
def messy(db, human):
    """A database like a long-lived dev copy: demo agents, legacy sample and
    ad-hoc agents, and a job with approvals, ledger, mandate, names and a
    review hanging off it."""
    from app.models import (Agent, Approval, ApprovalEvent, Engagement, EnsName, LedgerEntry,
                            Mandate, Milestone, Order, Review, Screening)
    seed_demo_agents(db, Agent, address_map="")
    sample = Agent(name="Sample: Docs Maintainer", category="Content", billing="per_minute",
                   seller=DEAD, deployer_wallet=DEAD.lower())
    adhoc = Agent(name="ff", category="Development", billing="per_token",
                  seller="0x" + "6" * 40)
    db.session.add_all([sample, adhoc])
    db.session.flush()
    demo = Agent.query.filter_by(seller=DEMO_OPERATOR).first()
    now = datetime.now(timezone.utc)
    eng = Engagement(agent_id=adhoc.id, buyer_human_id=human.id, outcome="gh",
                     total_micro=5_000_000, status="funded")
    eng.milestones.append(Milestone(idx=0, title="All", amount_micro=5_000_000, status="funded"))
    db.session.add(eng)
    db.session.flush()
    sub = Engagement(agent_id=demo.id, parent_engagement_id=eng.id, depth=1, outcome="sub",
                     total_micro=1_000_000, status="funded")
    db.session.add(sub)
    scr = Screening(hop="engagement.fund", engagement_id=eng.id, agent_id=adhoc.id,
                    chain_address="0x" + "6" * 40, verdict="PAY", reasons=[], traits=[],
                    provider="fake", fail_closed=False)
    db.session.add(scr)
    db.session.flush()
    appr = Approval(kind="engagement.fund", action_json="{}", action_hash="0x", nonce="n",
                    flow="web", state="consumed", expires_at=now, engagement_id=eng.id,
                    milestone_id=eng.milestones[0].id, agent_id=adhoc.id, screening_id=scr.id)
    login = Approval(kind="session.login", action_json="{}", action_hash="0x", nonce="m",
                     flow="web", state="pending", expires_at=now)
    db.session.add_all([appr, login])
    db.session.flush()
    db.session.add(ApprovalEvent(approval_id=appr.id, event="consumed"))
    db.session.add(LedgerEntry(engagement_id=eng.id, kind="fund", amount_micro=5_000_000,
                               approval_id=appr.id, screening_id=scr.id))
    root = Mandate(root_id="MND-ROOT", engagement_id=eng.id, human_id=human.id,
                   grantee_agent_public_id=adhoc.public_id, budget_micro=5_000_000,
                   categories=["Development"], max_depth=2, expires_at=now + timedelta(days=1),
                   token="t", approval_id=appr.id)
    db.session.add(root)
    db.session.flush()
    db.session.add(Mandate(root_id=root.id, parent_id=root.id, engagement_id=sub.id,
                           human_id=human.id, grantee_agent_public_id=demo.public_id,
                           budget_micro=1_000_000, categories=[], max_depth=2, depth=1,
                           expires_at=now + timedelta(days=1), token="u"))
    db.session.add(EnsName(name="ff.agents.eth", kind="agent", agent_id=adhoc.id))
    db.session.add(EnsName(name="eng-1.ff.agents.eth", kind="job", parent_name="ff.agents.eth",
                           engagement_id=eng.id, agent_id=adhoc.id))
    db.session.add(Review(agent_id=adhoc.id, user="me", rating=5, comment="ok"))
    db.session.add(Order(id="ORD-1", agent_id=sample.id, amount=1.0))
    db.session.commit()
    return {"human": human, "sample": sample.id, "adhoc": adhoc.id}


def _reset(app, *args):
    return app.test_cli_runner().invoke(args=["reset-demo", "--yes", *args])


def test_reset_leaves_only_demo_agents_and_three_example_jobs(app, db, messy):
    from app.models import (Agent, Approval, ApprovalEvent, Engagement, EnsName, Human,
                            LedgerEntry, Mandate, Order, Review)
    from app.seller.stamp import stamp_status
    out = _reset(app, "--dev-stamp")
    assert out.exit_code == 0, out.output

    agents = Agent.query.all()
    assert sorted(a.name for a in agents) == sorted(s["name"] for s in DEMO_AGENTS)
    assert all(a.seller == DEMO_OPERATOR and stamp_status(a).ok for a in agents)
    assert f"Hireable now: {len(DEMO_AGENTS)} of {len(DEMO_AGENTS)} agents." in out.output
    assert Approval.query.count() == ApprovalEvent.query.count() == Mandate.query.count() == 0
    assert Order.query.count() == 0
    assert EnsName.query.count() == 0
    assert Review.query.count() == 3 * len(DEMO_AGENTS)          # the demo reviews only
    assert Human.query.count() == 1                               # people are kept

    jobs = Engagement.query.order_by(Engagement.created_at).all()
    assert len(jobs) == len(DEMO_JOBS) == 3
    assert all(e.buyer_human_id == messy["human"].id for e in jobs)
    assert all(e.agent.seller == DEMO_OPERATOR for e in jobs)
    released = {e.status: sum(m.status == "released" for m in e.milestones) for e in jobs}
    assert released == {"completed": 3, "in_progress": 1, "funded": 0}
    for e in jobs:
        rows = LedgerEntry.query.filter_by(engagement_id=e.id).all()
        [fund] = [r for r in rows if r.kind == "fund"]
        assert fund.amount_micro == e.total_micro == sum(m.amount_micro for m in e.milestones)
        rel = {r.milestone_id: r.amount_micro for r in rows if r.kind == "release"}
        assert rel == {m.id: m.amount_micro for m in e.milestones if m.status == "released"}
        assert all(r.status == "simulated" and (r.tx_hash or "").startswith("sim-") for r in rows)
        assert all(m.released_ledger_id for m in e.milestones if m.status == "released")
        assert all(m.status == "funded" for m in e.milestones if m.status != "released")
        assert len(e.outcome) <= 90                              # fits the /jobs title


def test_reset_is_repeatable(app, db, messy):
    from app.models import Agent, Engagement, LedgerEntry, Review
    assert _reset(app, "--dev-stamp").exit_code == 0
    ids = sorted(a.public_id for a in Agent.query.all())
    out = _reset(app)
    assert out.exit_code == 0, out.output
    assert sorted(a.public_id for a in Agent.query.all()) == ids   # demo agents keep their ids
    assert Engagement.query.count() == 3 and Review.query.count() == 3 * len(DEMO_AGENTS)
    assert LedgerEntry.query.count() == 3 + 0 + 1 + 3               # funds + releases


def test_jobs_render_after_reset(app, client, db, messy):
    from app.models import Engagement
    assert _reset(app, "--dev-stamp").exit_code == 0
    html = client.get("/jobs").get_data(as_text=True)
    for job in DEMO_JOBS:
        assert job["outcome"] in html
    for e in Engagement.query.all():
        assert client.get(f"/jobs/{e.id}").status_code == 200


def test_reset_without_humans_creates_a_simulated_buyer(app, db):
    from app.demo_reset import DEMO_BUYER_SUB
    from app.models import Engagement, Human
    assert _reset(app).exit_code == 0
    [buyer] = Human.query.all()
    assert buyer.world_sub == DEMO_BUYER_SUB
    assert {e.buyer_human_id for e in Engagement.query.all()} == {buyer.id}


def test_reset_asks_before_deleting(app, db, messy):
    from app.models import Agent
    out = app.test_cli_runner().invoke(args=["reset-demo"], input="n\n")
    assert out.exit_code != 0
    assert Agent.query.filter_by(name="ff").count() == 1


def test_reset_refuses_in_production(app, db, messy, monkeypatch):
    from app.models import Agent
    monkeypatch.setitem(app.config, "ENV_NAME", "production")
    out = _reset(app)
    assert out.exit_code != 0 and "never runs in production" in out.output
    assert Agent.query.filter_by(name="ff").count() == 1


def test_reset_refuses_with_onchain_escrow(app, db, messy, monkeypatch):
    from app.engagements import service as svc
    from app.models import Agent
    escrow = svc.get_escrow()
    monkeypatch.setattr(escrow, "mode", "onchain")
    out = _reset(app)
    assert out.exit_code != 0 and "simulated escrow" in out.output
    assert Agent.query.filter_by(name="ff").count() == 1


def test_seed_loads_demo_agents_not_legacy_samples(app, db):
    from app.models import Agent
    from app.seller.stamp import stamp_status
    out = app.test_cli_runner().invoke(args=["seed"])
    assert out.exit_code == 0, out.output
    rows = Agent.query.all()
    assert sorted(a.name for a in rows) == sorted(s["name"] for s in DEMO_AGENTS)
    assert not any(a.name.startswith("Sample:") for a in rows)
    assert all(stamp_status(a).ok for a in rows)


def test_reset_deletes_in_foreign_key_order(app, db, messy):
    """With SQLite foreign keys enforced (as Postgres always does), the
    delete order must not leave a dangling reference at any step."""
    from sqlalchemy import text
    db.session.commit()
    db.session.execute(text("PRAGMA foreign_keys=ON"))
    assert db.session.execute(text("PRAGMA foreign_keys")).scalar() == 1
    out = _reset(app, "--dev-stamp")
    assert out.exit_code == 0, out.output
    assert db.session.execute(text("PRAGMA foreign_key_check")).fetchall() == []
