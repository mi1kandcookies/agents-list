"""Guided job flow (/new) and engagement estimate page (/estimate/<id>)."""
import json
import re
from datetime import date, datetime, timezone

from app.intake.estimate import CATEGORIES, category_for, estimate


def _flow_config(html: str) -> dict:
    m = re.search(r'<script type="application/json" id="flow-config">(.*?)</script>', html, re.S)
    assert m, "flow config missing"
    return json.loads(m.group(1))


def test_new_page_renders_all_steps(client):
    res = client.get("/new")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    for step in range(1, 10):
        assert f'data-step="{step}"' in html
    assert "Approve with World ID" in html
    assert "css/flow.css" in html and "js/flow.js" in html
    config = _flow_config(html)
    assert [c["label"] for c in config["categories"]] == [
        "Research", "Growth", "Engineering", "Data", "Ops", "Content"]
    assert config["auto_release_days"] > 0


def test_new_page_prefills_outcome_from_query(client):
    res = client.get("/new?q=Map+our+top+20+competitors")
    html = res.get_data(as_text=True)
    assert re.search(r'<textarea id="f-outcome"[^>]*>Map our top 20 competitors</textarea>', html, re.S)
    assert 'data-prefill="Map our top 20 competitors"' in html


def test_new_page_preselects_agent_from_profile_link(client, agent):
    html = client.get(f"/new?agent={agent}").get_data(as_text=True)
    m = re.search(r'<script type="application/json" id="flow-agent">(.*?)</script>', html, re.S)
    picked = json.loads(m.group(1))
    assert picked["id"] == agent and picked["name"] == "Test Agent"
    assert picked["category_key"] == "engineering"   # "Development" listing category
    html = client.get("/new?agent=999999").get_data(as_text=True)
    assert '<script type="application/json" id="flow-agent">null</script>' in html


def test_new_is_served_by_the_guided_flow(app):
    adapter = app.url_map.bind("localhost")
    assert adapter.match("/new")[0] == "intake.new_job"


def test_new_page_escapes_prefill(client):
    html = client.get("/new?q=%3Cscript%3Ealert(1)%3C/script%3E").get_data(as_text=True)
    assert "<script>alert(1)" not in html
    assert "&lt;script&gt;alert(1)" in html


def test_estimate_unknown_engagement_is_404(client):
    res = client.get("/estimate/ENG-DOESNOTEXIST")
    assert res.status_code == 404
    assert "No engagement ENG-DOESNOTEXIST exists" in res.get_data(as_text=True)


def test_estimate_page_shows_contract_for_engagement(client, db, agent, human):
    from app.models import Engagement, Milestone
    eng = Engagement(agent_id=agent, buyer_human_id=human.id, outcome="Competitor teardown",
                     category="Research", total_micro=500_000_000, sow_hash="0x" + "ab" * 32,
                     status="scoped", deadline_at=datetime(2030, 1, 15, tzinfo=timezone.utc))
    eng.milestones.append(Milestone(idx=0, title="Scope and sources", amount_micro=200_000_000,
                                    acceptance="- Agreed list of sources\n- Questions signed off"))
    eng.milestones.append(Milestone(idx=1, title="Final report", amount_micro=300_000_000,
                                    acceptance="Executive summary on one page"))
    db.session.add(eng)
    db.session.commit()

    res = client.get(f"/estimate/{eng.id}")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert eng.id in html
    assert "Scope and sources" in html and "Questions signed off" in html
    assert 'data-bind="c-total">500<' in html and "200 USDC" in html and "500.00" not in html
    assert eng.sow_hash in html
    assert "Each milestone is paid only when you approve it." in html
    assert 'id="estimate-approve"' in html


def test_estimate_page_hides_approve_once_funded(client, db, agent):
    from app.models import Engagement
    eng = Engagement(agent_id=agent, outcome="x", total_micro=1, status="funded")
    db.session.add(eng)
    db.session.commit()
    html = client.get(f"/estimate/{eng.id}").get_data(as_text=True)
    assert 'id="estimate-approve"' not in html


def test_category_defaults_are_consistent():
    for cat in CATEGORIES:
        assert 2 <= len(cat["milestones"]) <= 3
        assert abs(sum(m["share"] for m in cat["milestones"]) - 1) < 1e-9
        assert all(m["criteria"] for m in cat["milestones"])
    assert category_for("data & analytics")["key"] == "data"
    assert category_for("Engineering")["key"] == "engineering"
    assert category_for("unknown") is None


def test_estimate_is_deterministic_and_tightens_with_detail():
    # Cost is now token based (see tests/test_token_model.py); the buffer only
    # widens the duration range.
    vague = estimate(outcome="short", milestones=[{"amount_cents": 50_000, "criteria": []}],
                     category="research")
    assert vague["confidence"] == "low"
    assert vague["total_cents"] == 50_000
    assert vague["cost_low_micro"] is None and vague["cost_high_micro"] is None  # no prices
    assert (vague["days_low"], vague["days_high"]) == (3, 5)

    detailed = estimate(outcome="x" * 100,
                        milestones=[{"title": "M", "amount_cents": 50_000, "criteria": ["a", "b"]}] * 2,
                        category="research", deadline=date(2026, 1, 8), today=date(2026, 1, 1),
                        input_price_per_1m=3_000_000, output_price_per_1m=15_000_000)
    assert detailed["confidence"] == "good" and detailed["tips"] == []
    assert (detailed["days_low"], detailed["days_high"]) == (6, 8)
    assert detailed["deadline_fit"] == "tight"
    assert 0 < detailed["cost_low_micro"] < detailed["cost_high_micro"]
    assert detailed["over_budget"] is False
    assert detailed == estimate(outcome="x" * 100,
                                milestones=[{"title": "M", "amount_cents": 50_000, "criteria": ["a", "b"]}] * 2,
                                category="research", deadline=date(2026, 1, 8), today=date(2026, 1, 1),
                                input_price_per_1m=3_000_000, output_price_per_1m=15_000_000)
