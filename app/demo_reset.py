"""
demo_reset.py - `flask --app wsgi reset-demo`: put a database back into a
clean demo state.

Steps, in one command:
    1. Delete every job and what hangs off it: engagements, milestones,
       ledger entries, approvals (and their events), mandates, screenings
       tied to a job, job ENS names, hire intents, legacy orders, payouts
       and chain transactions.
    2. Delete every agent that is not one of the demo agents
       (app/demo_seed.py), with its reviews, screenings, names and reports.
    3. Reseed the demo agents (idempotent, same as `flask seed-demo`).
    4. Seed three example jobs for the demo buyer: one funded with nothing
       released, one with 1 of 3 milestones released, one completed.

Kept: humans, used World ID token ids (replay protection), the onboarding
screenings of kept agents and the approval behind a kept agent's current
operator stamp.

Money: the example jobs are funded and released through the escrow service
and the ledger, exactly as the approval executors do, but without World ID
approvals. The command refuses unless the escrow is simulated (no keys), so
it never moves funds, and refuses in production.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import click
from flask.cli import with_appcontext

DEMO_BUYER_SUB = "simulated:demo-buyer"

# (agent name, outcome, days since created, deadline in days or None,
#  milestones released, milestones [(title, acceptance, amount_usdc)])
DEMO_JOBS: list[dict] = [
    {
        "agent": "Keelhaul Audit",
        "outcome": "Security review of our USDC vault and escrow contracts",
        "age_days": 2, "deadline_days": 21, "released": 0,
        "milestones": [
            ("Scope and threat model",
             "- Contracts in scope pinned to a commit hash\n- Threat model agreed", 400),
            ("Findings report",
             "- Every finding has a Foundry proof of concept\n- Findings ranked by severity", 1000),
            ("Fix review",
             "- Each fix commit re-checked\n- Final report updated", 400),
        ],
    },
    {
        "agent": "Conduit Data Pipelines",
        "outcome": "Nightly Stripe and HubSpot sync into BigQuery with tested dbt models",
        "age_days": 9, "deadline_days": 12, "released": 1,
        "milestones": [
            ("Source profiling and data contract",
             "- Row counts reconciled with both sources\n- Data contract signed off", 300),
            ("Ingestion jobs and dbt models",
             "- Incremental loads run nightly\n- dbt tests pass in CI", 700),
            ("Freshness alerts and hand-off",
             "- Freshness alerts post to Slack\n- Runbook in the repository", 300),
        ],
    },
    {
        "agent": "Headline Landing Copy",
        "outcome": "Landing page copy for the launch of our invoicing product",
        "age_days": 24, "deadline_days": None, "released": 3,
        "milestones": [
            ("Outline and angle", "- Outline approved\n- Two headline directions", 90),
            ("Full page draft", "- All sections written\n- Matches the brand tone guide", 210),
            ("Final copy and variants", "- Two headline variants for testing\n- One revision round", 150),
        ],
    },
]


class ResetRefused(click.ClickException):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── delete ───────────────────────────────────────────────────────────────────

def _delete(model, *criteria) -> int:
    from app.extensions import db
    q = db.session.query(model)
    for c in criteria:
        q = q.filter(c)
    return q.delete(synchronize_session=False)


def clear_jobs_and_extra_agents(db) -> dict:
    """Steps 1 and 2. Children before parents, so it also works with foreign
    keys enforced. Flushed, not committed."""
    from app.demo_seed import DEMO_AGENTS, DEMO_OPERATOR
    from app.models import (Agent, Approval, ApprovalEvent, ChainTransaction, Engagement, EnsName,
                            HireIntent, LedgerEntry, Mandate, Milestone, ModerationReport, Order,
                            Payout, Review, Screening, VerificationEntry)
    demo_names = {s["name"] for s in DEMO_AGENTS}
    agents = Agent.query.all()
    kept = [a for a in agents if a.name in demo_names and a.seller == DEMO_OPERATOR]
    doomed = [a.id for a in agents if a not in kept]
    # The approval behind a kept agent's current stamp is its provenance.
    stamp_approvals = [a.manifest_stamp_approval_id for a in kept if a.manifest_stamp_approval_id]
    kept_screenings = [sid for (sid,) in db.session.query(Approval.screening_id).filter(
        Approval.id.in_(stamp_approvals), Approval.screening_id.isnot(None)).all()]

    counts = {}
    counts["hire_intents"] = _delete(HireIntent)
    # Job names hang under agent names; delete them first.
    counts["ens_names"] = _delete(EnsName, EnsName.engagement_id.isnot(None))
    counts["ens_names"] += _delete(EnsName, EnsName.agent_id.in_(doomed))
    db.session.query(Mandate).update({Mandate.parent_id: None}, synchronize_session=False)
    counts["mandates"] = _delete(Mandate)
    counts["ledger_entries"] = _delete(LedgerEntry)
    doomed_approvals = [aid for (aid,) in db.session.query(Approval.id).filter(
        ~Approval.id.in_(stamp_approvals)).all()]
    _delete(ApprovalEvent, ApprovalEvent.approval_id.in_(doomed_approvals))
    counts["approvals"] = _delete(Approval, Approval.id.in_(doomed_approvals))
    counts["screenings"] = _delete(
        Screening, ~Screening.id.in_(kept_screenings),
        (Screening.engagement_id.isnot(None)) | (Screening.agent_id.in_(doomed)))
    from app.models import Delivery
    counts["deliveries"] = _delete(Delivery)
    counts["milestones"] = _delete(Milestone)
    db.session.query(Engagement).update({Engagement.parent_engagement_id: None},
                                        synchronize_session=False)
    counts["engagements"] = _delete(Engagement)
    counts["orders"] = _delete(Order)
    counts["payouts"] = _delete(Payout)
    counts["chain_transactions"] = _delete(ChainTransaction)
    # Reviews are reseeded with the demo agents.
    counts["reviews"] = _delete(Review)
    _delete(VerificationEntry, VerificationEntry.agent_id.in_(doomed))
    _delete(ModerationReport, ModerationReport.agent_id.in_(doomed))
    counts["agents"] = _delete(Agent, Agent.id.in_(doomed))
    db.session.flush()
    db.session.expire_all()
    return counts


# ── example jobs ─────────────────────────────────────────────────────────────

def demo_buyer(db, sub: str | None = None):
    """The human the example jobs belong to: ``sub`` if given (created when
    missing), else the most recently seen human who is not banned, else a
    simulated demo buyer."""
    from app.models import Human
    if sub:
        human = Human.query.filter_by(world_sub=sub).first()
    else:
        human = (Human.query.filter(Human.banned_at.is_(None))
                 .order_by(Human.last_seen_at.desc().nullslast(), Human.id.desc()).first())
        sub = DEMO_BUYER_SUB
    if human is None:
        human = Human(world_sub=sub)
        db.session.add(human)
        db.session.flush()
    return human


def seed_demo_jobs(db, buyer) -> list:
    """Step 4. Uses the engagement service, the escrow service (simulated)
    and the ledger, so balances match what the approval executors record."""
    from app.engagements import ledger
    from app.engagements import service as svc
    from app.models import Agent
    from app.demo_seed import DEMO_OPERATOR
    escrow = svc.get_escrow()
    if escrow.mode != "simulated":
        raise ResetRefused("escrow keys are configured; example jobs are only seeded against "
                           "the simulated escrow")
    now = _now()
    created = []
    for job in DEMO_JOBS:
        agent = Agent.query.filter_by(name=job["agent"], seller=DEMO_OPERATOR).first()
        if agent is None:
            continue
        payee = svc.payee_address(agent)
        start = now - timedelta(days=job["age_days"])
        deadline = (now + timedelta(days=job["deadline_days"])).strftime("%Y-%m-%d") \
            if job["deadline_days"] else None
        milestones = [{"title": t, "acceptance": a, "amount_usdc": usdc}
                      for t, a, usdc in job["milestones"]]
        budget = sum(usdc for _, _, usdc in job["milestones"]) * 1_000_000
        eng, _ = svc.create_engagement(agent=agent, outcome=job["outcome"], budget_micro=budget,
                                       milestones=milestones, deadline=deadline,
                                       screen_payee=False)
        eng.created_at = start
        eng.buyer_human_id = buyer.id
        tx = escrow.fund_from_vault(amount_micro=eng.total_micro, ref=f"{eng.id}:fund#0")
        fund = ledger.record(eng, kind="fund", amount_micro=eng.total_micro, tx=tx,
                             from_addr=escrow.vault_address, to_addr=escrow.escrow_address)
        fund.created_at = start + timedelta(hours=1)
        for m in eng.milestones:
            m.status = "funded"
        eng.status = "funded"
        for m in eng.milestones[:job["released"]]:
            delivered = start + timedelta(days=2 + 3 * m.idx)
            m.status, m.submitted_at = "submitted", delivered
            tx = escrow.release(to=payee, amount_micro=m.amount_micro,
                                ref=f"{eng.id}:m{m.idx}:release@0#0")
            rel = ledger.record(eng, kind="release", amount_micro=m.amount_micro, milestone=m,
                                tx=tx, from_addr=escrow.escrow_address, to_addr=payee)
            rel.created_at = delivered + timedelta(hours=20)
            m.status, m.released_ledger_id = "released", rel.id
            eng.status = "in_progress"
        if eng.milestones and all(m.status == "released" for m in eng.milestones):
            eng.status = "completed"
        eng.updated_at = max([start] + [m.submitted_at for m in eng.milestones if m.submitted_at])
        created.append(eng)
    db.session.commit()
    return created


# ── command ──────────────────────────────────────────────────────────────────

def reset_refusal(app) -> str | None:
    import os
    env = str(app.config.get("ENV_NAME") or os.environ.get("FLASK_ENV") or "").lower()
    if env == "production":
        return "reset-demo never runs in production"
    return None


@click.command("reset-demo")
@click.option("--yes", is_flag=True, help="Do not ask for confirmation.")
@click.option("--dev-stamp", is_flag=True,
              help="DEVELOPMENT ONLY: also write simulated operator stamps for the demo agents "
                   "(same rules as `flask seed-stamps`).")
@click.option("--buyer-sub", default=None,
              help="World ID subject of the buyer who owns the example jobs. Default: the most "
                   "recently seen human, else a simulated demo buyer.")
@with_appcontext
def reset_demo(yes: bool, dev_stamp: bool, buyer_sub: str | None):
    """Delete all jobs and non-demo agents, reseed the demo agents and three
    example jobs."""
    from flask import current_app

    from app.demo_seed import seed_demo_agents
    from app.engagements import service as svc
    from app.extensions import db
    from app.models import Agent
    from app.seller.stamp import seed_stamps_refusal, stamp_status

    refusal = reset_refusal(current_app)
    if refusal:
        raise ResetRefused(refusal)
    if svc.get_escrow().mode != "simulated":
        raise ResetRefused("escrow keys are configured; reset-demo only runs against the "
                           "simulated escrow")
    if dev_stamp:
        refusal = seed_stamps_refusal(current_app)
        if refusal:
            raise ResetRefused(f"Refusing --dev-stamp: {refusal}")
    if not yes:
        click.confirm(f"Delete every job and every non-demo agent in "
                      f"{db.engine.url.render_as_string(hide_password=True)}?", abort=True)
    counts = clear_jobs_and_extra_agents(db)
    db.session.commit()
    try:
        seeded = seed_demo_agents(db, Agent, dev_stamp=dev_stamp)
    except (ValueError, OSError) as exc:
        raise ResetRefused(f"SCREENING_ADDRESS_MAP is unreadable: {exc}") from None
    buyer = demo_buyer(db, buyer_sub)
    jobs = seed_demo_jobs(db, buyer)

    click.echo("Deleted: " + ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in counts.items()) + ".")
    click.echo(f"Demo agents: {seeded['added']} added, {seeded['updated']} refreshed.")
    rows = Agent.query.order_by(Agent.id).all()
    hireable = sum(1 for a in rows if stamp_status(a).ok)
    click.echo(f"Hireable now: {hireable} of {len(rows)} agents.")
    click.echo(f"Example jobs for human {buyer.id}:")
    for eng in jobs:
        done = sum(1 for m in eng.milestones if m.status == "released")
        click.echo(f"  {eng.id}  {eng.status:<11} {done}/{len(eng.milestones)} released  {eng.outcome}")
