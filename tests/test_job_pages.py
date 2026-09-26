"""Job tracking and approval pages render for every job and approval state,
built from database rows (no provider, screener or escrow calls)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.approvals import actions
from app.engagements.sow import build_sow, sow_hash, sow_json, title_hash

PAYEE = "0x" + "4b" * 20
NOW = datetime.now(timezone.utc).replace(tzinfo=None)


def _ts(dt) -> int:
    return int(dt.replace(tzinfo=timezone.utc).timestamp())


@pytest.fixture()
def rows(db, agent, human):
    """Builders for engagements, approvals and ledger rows."""
    from app.models import (Agent, Approval, ApprovalEvent, Engagement, LedgerEntry, Milestone,
                            Screening)
    a = db.session.get(Agent, agent)
    a.payout_address = PAYEE
    db.session.commit()

    class B:
        agent = a

        @staticmethod
        def job(status, plan=((10_000_000, "- Outline approved\n- Sources listed"),
                              (15_000_000, "Tests pass")), buyer=human, deadline_days=5):
            ms = [{"idx": i, "title": f"Step {i + 1}", "acceptance": acc, "amount_micro": amt}
                  for i, (amt, acc) in enumerate(plan)]
            total = sum(m["amount_micro"] for m in ms)
            dl = NOW + timedelta(days=deadline_days) if deadline_days is not None else None
            sow = build_sow(agent_public_id=a.public_id, outcome=f"Job in state {status}",
                            budget_micro=total, milestones=ms, deadline=_ts(dl) if dl else None,
                            category=a.category)
            eng = Engagement(agent_id=a.id, outcome=f"Job in state {status}", category=a.category,
                             sow_json=sow_json(sow), sow_hash=sow_hash(sow), total_micro=total,
                             status=status, buyer_human_id=buyer.id if buyer else None,
                             deadline_at=dl)
            for m in ms:
                eng.milestones.append(Milestone(idx=m["idx"], title=m["title"],
                                                acceptance=m["acceptance"],
                                                amount_micro=m["amount_micro"]))
            db.session.add(eng)
            db.session.commit()
            return eng

        @staticmethod
        def screening(eng, verdict="PAY"):
            s = Screening(hop="engagement.fund", engagement_id=eng.id, agent_id=a.id,
                          chain_address=PAYEE, screened_address=PAYEE, verdict=verdict,
                          reasons=[], traits=[], provider="fake", latency_ms=5)
            db.session.add(s)
            db.session.commit()
            return s

        @staticmethod
        def approval(eng, state, *, kind="engagement.fund", milestone=None, flow="web",
                     code=None, screening=None, result=None, user_code=None):
            row = Approval(kind=kind, action_json="{}", action_hash="0x", nonce="x", flow=flow,
                           state=state, expires_at=NOW + timedelta(hours=1),
                           engagement_id=eng.id, agent_id=a.id, failure_code=code,
                           milestone_id=milestone.id if milestone else None,
                           screening_id=screening.id if screening else None)
            db.session.add(row)
            db.session.flush()
            fields = {"engagement_id": eng.id, "sow_hash": eng.sow_hash,
                      "payee_agent_id": a.public_id, "payee_address": PAYEE,
                      "amount_micro": milestone.amount_micro if milestone else eng.total_micro}
            if milestone is not None:
                fields["milestone_idx"] = milestone.idx
            else:
                fields["milestones"] = [{"idx": m.idx, "amount_micro": m.amount_micro,
                                         "title_hash": title_hash(m.title)} for m in eng.milestones]
            action = actions.build_action(kind, approval_id=row.id,
                                          exp=_ts(NOW + timedelta(hours=1)), **fields)
            row.action_json, row.action_hash = json.dumps(action), actions.action_hash(action)
            row.nonce = actions.action_nonce(action)
            if user_code:
                row.user_code, row.device_code = user_code, "dev"
                row.verification_uri_complete = f"https://id.example/activate?user_code={user_code}"
                row.next_poll_at = NOW + timedelta(days=1)   # never asks the provider
            if result:
                db.session.add(ApprovalEvent(approval_id=row.id, event="consumed",
                                             detail={"result": result}))
            db.session.commit()
            return row

        @staticmethod
        def ledger(eng, kind, amount, *, milestone=None, approval=None, status="simulated"):
            e = LedgerEntry(engagement_id=eng.id, kind=kind, amount_micro=amount,
                            milestone_id=milestone.id if milestone else None,
                            approval_id=approval.id if approval else None, status=status,
                            tx_hash=None if kind == "hold" else f"sim-{kind}")
            db.session.add(e)
            db.session.commit()
            return e

        @staticmethod
        def fund(eng):
            apr = B.approval(eng, "consumed")
            B.ledger(eng, "fund", eng.total_micro, approval=apr)
            for m in eng.milestones:
                m.status = "funded"
            db.session.commit()
            return apr
    return B


def _html(client, url):
    resp = client.get(url)
    assert resp.status_code == 200, url
    return resp.get_data(as_text=True)


# ── /jobs/<id> ──────────────────────────────────────────────────────────────
def test_scoped_job_offers_funding_and_tabs(client, rows):
    eng = rows.job("scoped")
    html = _html(client, f"/jobs/{eng.id}")
    assert "Approve &amp; fund 25 USDC" in html and "Approve &amp; release" not in html
    assert f'href="/jobs/{eng.id}/chain"' in html and "Delegation chain" in html
    assert "Not funded" in html and "Nothing has moved yet." in html
    assert "No approvals requested yet." in html
    assert f'data-copy="{eng.sow_hash}"' in html
    assert "<li><span class=\"box\"" in html and "Sources listed" in html   # criteria checklist
    assert "4 days left" in html or "5 days left" in html


def test_in_progress_job_shows_money_timeline_and_actions(client, db, rows):
    eng = rows.job("in_progress")
    rows.fund(eng)
    m0, m1 = eng.milestones
    rel = rows.approval(eng, "consumed", kind="milestone.release", milestone=m0)
    rows.ledger(eng, "release", m0.amount_micro, milestone=m0, approval=rel)
    m0.status = "released"
    m1.status, m1.submitted_at = "submitted", NOW - timedelta(days=1)
    db.session.commit()
    html = _html(client, f"/jobs/{eng.id}")
    assert "10 USDC" in html and "15 USDC" in html            # released, in escrow
    assert 'class="is-released" style="width:40.0%"' in html
    assert html.count("Approve &amp; release") == 1                 # only the submitted one
    assert 'data-tip="Coming soon"' in html and "Dispute" in html and "Request changes" in html
    auto = (m1.submitted_at + timedelta(days=7)).strftime("%Y-%m-%d")
    assert f"Auto-releases <span class=\"mono\">{auto}</span>" in html
    assert "Delivered for review" in html and "Released to agent" in html
    assert "simulated, not on chain" in html
    assert rel.id in html and "Done" in html


def test_open_release_approval_is_continued_not_duplicated(client, rows):
    eng = rows.job("funded")
    rows.fund(eng)
    pending = rows.approval(eng, "pending", kind="milestone.release", milestone=eng.milestones[0])
    html = _html(client, f"/jobs/{eng.id}")
    assert f'href="/approvals/{pending.id}">Continue release approval' in html
    assert html.count("Approve &amp; release") == 1                 # milestone 2 only


def test_held_completed_and_refused_jobs(client, db, rows):
    held = rows.job("in_progress", plan=((60_000_000, "Report"),))
    rows.fund(held)
    m = held.milestones[0]
    rel = rows.approval(held, "consumed", kind="milestone.release", milestone=m)
    rows.ledger(held, "release", 10_000_000, milestone=m, approval=rel)
    rows.ledger(held, "hold", 50_000_000, milestone=m, approval=rel)
    m.status = "held"
    db.session.commit()
    html = _html(client, f"/jobs/{held.id}")
    assert "50 USDC held" in html and "Partly held" in html and "Approve &amp; release" in html

    done = rows.job("completed", plan=((5_000_000, "Done"),), deadline_days=None)
    done.milestones[0].status = "released"
    db.session.commit()
    html = _html(client, f"/jobs/{done.id}")
    assert "status-pill is-completed" in html and "No deadline" in html
    assert "Approve &amp;" not in html

    refused = rows.job("refused")
    html = _html(client, f"/jobs/{refused.id}")
    assert "Risk screening refused" in html and "Approve &amp;" not in html


def test_sub_job_has_no_fund_button_and_links_its_chain(client, db, rows):
    parent = rows.job("funded")
    child = rows.job("scoped")
    child.parent_engagement_id, child.depth = parent.id, 1
    db.session.commit()
    html = _html(client, f"/jobs/{child.id}")
    assert "Approve &amp; fund" not in html and f"/jobs/{child.id}/hire" not in html
    assert "data-subhire" in html and f'href="/jobs/{parent.id}"' in html
    assert f'href="/jobs/{child.id}/chain"' in html
    assert client.get(f"/jobs/{child.id}/chain").status_code == 200


def test_names_tab_links_to_the_job_name(client, db, rows):
    eng = rows.job("scoped")
    assert 'class="is-off"' in _html(client, f"/jobs/{eng.id}")
    eng.ens_name = "job-1.agents.eth"
    db.session.commit()
    assert 'href="/names?root=job-1.agents.eth"' in _html(client, f"/jobs/{eng.id}")


def test_job_page_error_state(client, rows, fake_screener, app):
    app.extensions["screener"] = fake_screener
    eng = rows.job("completed")
    resp = client.post(f"/jobs/{eng.id}/hire")
    assert resp.status_code == 409 and "That didn't go through." in resp.get_data(as_text=True)


# ── /jobs ───────────────────────────────────────────────────────────────────
def test_jobs_list_filters_and_empty_states(client, rows):
    html = _html(client, "/jobs")
    assert "No jobs yet" in html and 'href="/new"' in html and "Describe your job" in html
    a, b = rows.job("scoped"), rows.job("completed")
    html = _html(client, "/jobs")
    assert a.id in html and b.id in html and "25 USDC" in html
    html = _html(client, "/jobs?status=completed")
    assert b.id in html and a.id not in html and 'aria-current="true">Completed' in html
    assert "No closed jobs" in _html(client, "/jobs?status=closed")


def test_jobs_list_shows_only_the_signed_in_humans_jobs(client, db, rows, human):
    from app.models import Human
    other = Human(world_sub="0x" + "9" * 64)
    db.session.add(other)
    db.session.commit()
    mine, theirs = rows.job("scoped"), rows.job("scoped", buyer=other)
    with client.session_transaction() as sess:
        sess["human_id"], sess["human_sub"] = human.id, human.world_sub
    html = _html(client, "/jobs")
    assert mine.id in html and theirs.id not in html


def test_jobs_list_asks_for_sign_in_outside_development(client, app, rows):
    rows.job("scoped")
    app.testing, app.debug = False, False
    app.config["ENV_NAME"] = "production"
    html = _html(client, "/jobs")
    assert "Sign in to see your jobs" in html and "ENG-" not in html


# ── /approvals/<id> ─────────────────────────────────────────────────────────
HOOKS = ('id="approval"', "data-api=", "data-cancel=", "data-state=", "data-terminal=",
         "data-expires-at=", 'id="approval-pill"')


def test_pending_approval_pages_keep_the_poll_hooks(client, rows):
    eng = rows.job("scoped")
    scr = rows.screening(eng)
    created = rows.approval(eng, "created", flow="device", screening=scr)
    pending = rows.approval(eng, "pending", flow="device", screening=scr, user_code="WDJB-MJHT")
    web = rows.approval(eng, "created", flow="web", screening=scr)

    html = _html(client, f"/approvals/{created.id}")
    assert all(h in html for h in HOOKS) and "Get approval code" in html
    assert "data-cancel-button" in html and "25 USDC" in html and rows.agent.name in html
    assert "verdict-card--pay" in html and "css/screening.css" in html

    html = _html(client, f"/approvals/{pending.id}")
    assert 'id="approval-user-code">WDJB-MJHT' in html and "Open on your phone" in html
    assert 'data-copy="WDJB-MJHT"' in html and "data-countdown" in html and "apr-spinner" in html
    assert 'id="approval-pending"' in html

    html = _html(client, f"/approvals/{web.id}")
    assert "Continue with World ID" in html and f"/approvals/{web.id}/start" in html
    assert "js/approval-poll.js" in html


@pytest.mark.parametrize("state, code, headline, extra", [
    ("denied", None, "Denied", "declined in World ID"),
    ("expired", None, "Expired", "Ask for a new approval"),
    ("cancelled", None, "Cancelled", "Nothing was done"),
    ("blocked", "SCREENING_REFUSED", "Blocked", "The payee failed risk screening."),
    ("rejected", "REPLAYED_TOKEN", "Replay blocked", "Replays are blocked."),
    ("rejected", "NONCE_MISMATCH", "Rejected", "different action"),
])
def test_terminal_approval_screens(client, rows, state, code, headline, extra):
    eng = rows.job("scoped")
    apr = rows.approval(eng, state, code=code)
    html = _html(client, f"/approvals/{apr.id}")
    assert f"<h2>{headline}</h2>" in html and extra in html
    assert 'id="approval-terminal"' in html and 'data-terminal="true"' in html
    assert f'href="/jobs/{eng.id}">Back to the job' in html
    if code and code != "REPLAYED_TOKEN":
        assert f'Reason code <span class="mono">{code}</span>' in html


def test_consumed_approval_shows_done_with_ledger(client, rows):
    eng = rows.job("funded")
    apr = rows.approval(eng, "consumed", result={
        "ok": True, "summary": "Funded 25.00 USDC into escrow", "ledger_ids": [],
        "redirect": f"/jobs/{eng.id}"})
    entry = rows.ledger(eng, "fund", eng.total_micro, approval=apr)
    html = _html(client, f"/approvals/{apr.id}")
    assert "Approved &amp; done" in html and "Funded 25.00 USDC into escrow" in html
    assert f"{entry.id} (simulated)" in html and f'href="/jobs/{eng.id}">Continue' in html


def test_consumed_approval_shows_pending_chain_payment(client, rows, monkeypatch):
    class PendingEscrow:
        def receipt_status(self, tx_hash):
            return "pending"

    monkeypatch.setattr("app.engagements.service.get_escrow", lambda: PendingEscrow())
    eng = rows.job("funded")
    apr = rows.approval(eng, "consumed", result={
        "ok": True, "summary": "Funded 25.00 USDC into escrow", "ledger_ids": []})
    rows.ledger(eng, "fund", eng.total_micro, approval=apr, status="pending")
    html = _html(client, f"/approvals/{apr.id}")
    assert "Payment pending" in html
    assert "no second payment will be signed" in html
    assert 'data-payment-pending="true"' in html
