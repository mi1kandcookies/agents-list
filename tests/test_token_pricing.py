"""Token pricing, token-based estimates and agent icons on listings."""
import json
from datetime import datetime, timezone

import pytest

from app.common.agent_icons import CATEGORY_ICONS, FALLBACK, ICON_DIR, ICONS, icon_for, icon_svg
from app.common.money import format_token_price
from app.intake import token_model
from app.intake.estimate import estimate, estimate_text
from app.intake.token_model import estimate_tokens, format_tokens, model_config, token_cost


def _card(app, **agent):
    base = {"id": 1, "name": "Card Agent", "description": "Does work", "category": "Security",
            "seller": "Demo operator", "rating": 0, "reviews": 0, "tasks_completed": 3,
            "input_price_per_1m": 3_000_000, "output_price_per_1m": 15_000_000,
            "demo_listing": True, "icon": None}
    base.update(agent)
    with app.test_request_context():
        tpl = app.jinja_env.from_string(
            '{% from "components/agent_card.html" import agent_card %}{{ agent_card(a) }}')
        return tpl.render(a=base)


def _price_agent(db, agent_id, **fields):
    from app.models import Agent
    row = db.session.get(Agent, agent_id)
    for k, v in fields.items():
        setattr(row, k, v)
    db.session.commit()
    return row


# ── token prices ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("micro, text", [
    (3_000_000, "$3"), (15_000_000, "$15"), (2_500_000, "$2.50"),
    (600_000, "$0.60"), (75_000, "$0.075"), (0, ""), (None, ""),
])
def test_format_token_price(micro, text):
    assert format_token_price(micro) == text


def test_card_shows_token_prices_and_no_demo_tag(app):
    html = _card(app)
    assert '<span class="amount">$3</span> <span class="meta">input,</span></span>' in html
    assert '<span class="amount">$15</span> <span class="meta">output</span>' in html
    assert "per 1M tokens" in html
    assert "per min" not in html and ".00" not in html
    assert "Demo listing" not in html and "demo-tag" not in html


def test_card_without_token_prices_says_on_request(app):
    html = _card(app, input_price_per_1m=0, output_price_per_1m=0)
    assert "Token prices on request" in html and "$0" not in html


def test_profile_shows_token_prices_no_turnaround_no_demo_tag(client, db, agent):
    _price_agent(db, agent, input_price_per_1m=3_500_000, output_price_per_1m=17_500_000,
                 demo_listing=True, avg_completion_time="2 days")
    html = client.get(f"/agent/{agent}").get_data(as_text=True)
    assert "$3.50</span> per 1M tokens" in html and "$17.50</span> per 1M tokens" in html
    assert "per 1M input tokens" in html
    assert "per min" not in html and "Typical range" not in html and "Current rate" not in html
    assert "Avg turnaround" not in html and "2 days" not in html
    assert "Demo listing" not in html
    for kept in ("Jobs delivered", "On-time rate", "Repeat hires", "Rating"):
        assert kept in html


def test_api_agent_exposes_unrounded_token_prices(client, db, agent):
    _price_agent(db, agent, input_price_per_1m=75_000, output_price_per_1m=300_000)
    data = client.get(f"/api/agents/{agent}").get_json()
    data = data.get("agent", data)
    assert data["input_price_per_1m"] == 75_000 and data["input_price_display"] == 0.075
    assert data["output_price_display"] == 0.3


def test_search_api_returns_token_prices_not_per_minute(client, db, agent):
    _price_agent(db, agent, input_price_per_1m=3_000_000, output_price_per_1m=15_000_000)
    hit = client.get("/api/search?q=Test").get_json()["results"][0]
    assert (hit["input_price_per_1m"], hit["output_price_per_1m"]) == (3_000_000, 15_000_000)
    assert "current_price" not in hit


# ── icons ───────────────────────────────────────────────────────────────────

def test_every_icon_ships_and_category_map_is_complete():
    for key in ICONS:
        svg = (ICON_DIR / f"{key}.svg").read_text()
        assert svg.startswith("<svg") and 'viewBox="0 0 24 24"' in svg and 'stroke="currentColor"' in svg
    assert set(CATEGORY_ICONS.values()) <= set(ICONS)
    assert (ICON_DIR / "CREDITS.md").exists()


def test_icon_for_override_then_category_then_fallback():
    assert icon_for({"category": "Security"}) == "shield-check"
    assert icon_for({"category": "Security", "icon": "clipboard-check"}) == "clipboard-check"
    assert icon_for({"category": "Security", "icon": "no-such-icon"}) == "shield-check"
    assert icon_for({"category": "Underwater basket weaving"}) == FALLBACK
    assert icon_for({}) == FALLBACK

    class Row:
        category, icon = "Development", "plug"
    assert icon_for(Row()) == "plug"
    assert 'aria-hidden="true"' in icon_svg("plug")
    assert icon_svg("nope") == icon_svg(FALLBACK)


def test_card_renders_icon_not_letters(app):
    html = _card(app, category="Finance")
    tile = html[html.index('class="agent-avatar"'):html.index("</span>", html.index('class="agent-avatar"'))]
    assert "<svg" in tile and "SEC" not in tile and "FIN" not in tile
    assert icon_svg("circle-dollar-sign") in html


def test_footer_has_no_icon_set_link(client):
    html = client.get("/").get_data(as_text=True)
    assert "lucide.dev" not in html and "Icons by" not in html


# ── estimator ───────────────────────────────────────────────────────────────

SCOPE = {"category": "research", "outcome": "x" * 120,
         "milestones": [{"title": "Scope", "criteria": ["a b c"]}] * 3}


def test_estimate_tokens_heuristic_math():
    cfg = model_config(path="")
    tok = estimate_tokens(SCOPE, cfg)
    prof = token_model.TOKEN_PROFILE["research"]
    factor = 1 + token_model.CRITERION_WEIGHT * 1
    context = -(-(120 + 5 + 5) // token_model.CHARS_PER_TOKEN)        # ceil
    mid_in = 3 * (prof["input_per_milestone"] * factor + context * token_model.TURNS_PER_MILESTONE)
    mid_out = 3 * prof["output_per_milestone"] * factor
    assert tok["basis"] == "heuristic" and tok["runs"] == 0
    assert tok["input"]["low"] == int(mid_in * token_model.LOW // 1000 * 1000)
    assert tok["input"]["high"] == -(-int(round(mid_in * token_model.HIGH)) // 1000) * 1000
    assert tok["output"]["low"] <= mid_out <= tok["output"]["high"]
    assert tok == estimate_tokens(SCOPE, cfg)                           # deterministic


def test_more_criteria_and_milestones_mean_more_tokens():
    cfg = model_config(path="")
    base = estimate_tokens(SCOPE, cfg)
    more_crit = estimate_tokens({**SCOPE, "milestones": [{"title": "Scope", "criteria": ["a", "b", "c"]}] * 3}, cfg)
    more_ms = estimate_tokens({**SCOPE, "milestones": SCOPE["milestones"] * 2}, cfg)
    assert more_crit["output"]["high"] > base["output"]["high"]
    assert more_ms["input"]["low"] > base["input"]["low"]
    eng = estimate_tokens({**SCOPE, "category": "engineering"}, cfg)
    assert eng["input"]["high"] > base["input"]["high"]
    unknown = estimate_tokens({**SCOPE, "category": None}, cfg)
    assert unknown["basis"] == "heuristic"


def test_token_cost_is_tokens_times_prices():
    tok = {"input": {"low": 1_000_000, "high": 2_000_000}, "output": {"low": 100_000, "high": 200_000},
           "basis": "heuristic", "runs": 0}
    cost = token_cost(tok, 3_000_000, 15_000_000)
    # low: 1M x $3 + 100k x $15 = $4.50; high: 2M x $3 + 200k x $15 = $9
    assert cost == {"low_micro": 4_500_000, "high_micro": 9_000_000}
    # rounding: low down, high up, to the cent
    odd = token_cost({"input": {"low": 1_234, "high": 1_234}, "output": {"low": 0, "high": 0}},
                     3_000_000, 0)
    assert odd == {"low_micro": 0, "high_micro": 10_000}
    assert token_cost(tok, 0, 0) is None


def test_estimate_combines_tokens_prices_and_budget_cap():
    ms = [{"title": "Scope", "amount_cents": 20_000, "criteria": ["a b c"]}] * 3
    est = estimate(outcome="x" * 120, milestones=ms, category="Research",
                   input_price_per_1m=3_000_000, output_price_per_1m=15_000_000,
                   token_config=model_config(path=""))
    expect = token_cost(estimate_tokens(SCOPE, model_config(path="")), 3_000_000, 15_000_000)
    assert (est["cost_low_micro"], est["cost_high_micro"]) == (expect["low_micro"], expect["high_micro"])
    assert est["total_cents"] == 60_000 and est["over_budget"] is False
    tight = estimate(outcome="x" * 120, milestones=[dict(m, amount_cents=100) for m in ms],
                     category="Research", input_price_per_1m=3_000_000,
                     output_price_per_1m=15_000_000, token_config=model_config(path=""))
    assert tight["over_budget"] is True
    text = estimate_text(tight)
    assert "above your budget" in text["cost_note"] and text["cap"] == "3"


@pytest.mark.parametrize("n, text", [(950, "950"), (81_234, "81k"), (999_600, "1M"),
                                     (1_240_000, "1.2M"), (12_345_678, "12M")])
def test_format_tokens(n, text):
    assert format_tokens(n) == text


def test_calibration_file_switches_basis_and_reports_runs(tmp_path, monkeypatch):
    path = tmp_path / "cal.json"
    path.write_text(json.dumps({"runs": 7, "categories": {
        "research": {"input_per_milestone": 100_000, "output_per_milestone": 10_000,
                     "low": 0.9, "high": 1.1, "runs": 24},
        "content": {"input_per_milestone": 50_000, "output_per_milestone": 5_000},
        "growth": {"input_per_milestone": "bad"}}}))
    cfg = model_config(path=str(path))
    assert cfg["profiles"]["research"]["basis"] == "calibrated"
    assert cfg["profiles"]["content"]["runs"] == 7              # file-level default
    assert cfg["profiles"]["growth"]["basis"] == "heuristic"    # invalid entry ignored
    assert cfg["profiles"]["engineering"]["basis"] == "heuristic"

    tok = estimate_tokens(SCOPE, cfg)
    assert tok["basis"] == "calibrated" and tok["runs"] == 24
    assert tok["input"]["high"] < estimate_tokens(SCOPE, model_config(path=""))["input"]["high"]
    est = estimate(outcome=SCOPE["outcome"], category="research", token_config=cfg,
                   milestones=[{"title": "Scope", "amount_cents": 100, "criteria": ["a b c"]}] * 3)
    assert estimate_text(est)["method"].startswith("Estimated from 24 measured runs")

    # The env var is the default source; a missing file falls back quietly.
    monkeypatch.setenv(token_model.CALIBRATION_ENV, str(path))
    assert model_config()["profiles"]["research"]["basis"] == "calibrated"
    monkeypatch.setenv(token_model.CALIBRATION_ENV, str(tmp_path / "missing.json"))
    assert model_config()["profiles"]["research"]["basis"] == "heuristic"


def test_heuristic_copy_never_claims_runs():
    est = estimate(outcome="x", category="research", token_config=model_config(path=""),
                   milestones=[{"title": "A", "amount_cents": 100, "criteria": ["c"]}])
    method = estimate_text(est)["method"]
    assert "milestones and this agent's token prices" in method
    assert "run" not in method.replace("rule", "")


def test_flow_config_ships_the_resolved_token_model(client):
    import re
    html = client.get("/new").get_data(as_text=True)
    m = re.search(r'<script type="application/json" id="flow-config">(.*?)</script>', html, re.S)
    cfg = json.loads(m.group(1))
    assert cfg["token_model"]["profiles"]["research"]["basis"] == "heuristic"
    assert "_default" in cfg["token_model"]["profiles"]


def test_estimate_page_shows_tokens_and_token_cost(client, db, agent, human):
    from app.models import Engagement, Milestone
    _price_agent(db, agent, input_price_per_1m=3_000_000, output_price_per_1m=15_000_000)
    eng = Engagement(agent_id=agent, buyer_human_id=human.id, outcome="Competitor teardown",
                     category="Research", total_micro=500_000_000, status="scoped",
                     deadline_at=datetime(2030, 1, 15, tzinfo=timezone.utc))
    eng.milestones.append(Milestone(idx=0, title="Scope and sources", amount_micro=500_000_000,
                                    acceptance="- Agreed list of sources"))
    db.session.add(eng)
    db.session.commit()
    html = client.get(f"/estimate/{eng.id}").get_data(as_text=True)
    est = estimate(outcome="Competitor teardown", category="Research",
                   milestones=[{"title": "Scope and sources", "amount_cents": 50_000,
                                "criteria": ["Agreed list of sources"]}],
                   input_price_per_1m=3_000_000, output_price_per_1m=15_000_000)
    text = estimate_text(est)
    assert f'data-bind="est-cost">{text["cost"]}<' in html
    assert f'data-bind="est-tok-in">{text["tokens_in"]}<' in html
    assert f'data-bind="est-tok-out">{text["tokens_out"]}<' in html
    assert "Estimated from your milestones and this agent&#39;s token prices" in html
    assert 'data-bind="est-cap">500<' in html
    assert "Estimated usage" in html and "measured run" not in html
