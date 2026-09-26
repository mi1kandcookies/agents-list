"""The matchmaker (POST /api/intake/match, app/intake/matchmaker.py) and the
step of the guided flow that shows its answer. The model is always faked."""
from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from app.intake import matchmaker as mm
from app.intake.token_model import token_cost

WALLET = "0x" + "c" * 40


def _agent(db, name, category, description, does, *, pin=3_000_000, pout=15_000_000, stamped=True,
           jobs=10, rating=4.5):
    from app.models import Agent
    from app.seller.stamp import dev_stamp
    row = Agent(name=name, description=description, category=category, billing="per_token",
                seller=WALLET, deployer_wallet=WALLET, input_price_per_1m=pin, output_price_per_1m=pout,
                tasks_completed=jobs, rating=rating)
    row.capabilities = does
    row.tags = []
    db.session.add(row)
    db.session.commit()
    if stamped:
        dev_stamp(row)
        db.session.commit()
    return row


@pytest.fixture()
def market(db):
    research = _agent(db, "Scout Research", "Research",
                      "Competitor and market research with sourced findings.",
                      ["Competitor matrix with pricing and positioning", "Market sizing with stated assumptions"])
    builder = _agent(db, "Forge Integrations", "Development",
                     "Typed API clients and webhook endpoints.",
                     ["Typed API client with retries", "Webhook endpoint with signature verification"])
    hidden = _agent(db, "Unstamped Research", "Research",
                    "Competitor research, market sizing, pricing and positioning.",
                    ["Competitor matrix with pricing and positioning"], stamped=False)
    return {"research": research, "builder": builder, "hidden": hidden}


def _job(**over):
    body = {
        "outcome": "Build a competitor matrix for our payroll product with pricing and positioning "
                   "for the top 20 competitors, and size the market.",
        "category": "research",
        "milestones": [{"title": "Scope and sources", "amount_usdc": 150, "criteria": ["Sources agreed"]},
                       {"title": "Final report", "amount_usdc": 450, "criteria": ["Every claim sourced"]}],
        "budget_usdc": 600,
        "deadline": {"mode": "month", "date": (date.today() + timedelta(days=30)).isoformat()},
    }
    body.update(over)
    return body


def _post(client, body, **headers):
    headers.setdefault("Sec-Fetch-Site", "same-origin")
    return client.post("/api/intake/match", json=body, headers=headers)


def _fake_llm(monkeypatch, reply):
    from app import llm
    calls = []

    def fake_chat(system, user, **kw):
        calls.append({"system": system, "user": json.loads(user), **kw})
        if isinstance(reply, Exception):
            raise reply
        return reply(calls[-1]) if callable(reply) else reply

    monkeypatch.setattr(llm, "LLM_URL", "http://matchmaker.test")
    monkeypatch.setattr(llm, "chat", fake_chat)
    return calls


def _reply(agent_id, **over):
    body = {"ask": "A priced competitor matrix and a market size.", "agent_id": agent_id, "can_do": True,
            "reasons": ["Its capabilities list a competitor matrix with pricing and positioning."],
            "no_agent_reason": None, "confidence": "high", "tokens": None}
    body.update(over)
    return json.dumps(body)


# ── endpoint ────────────────────────────────────────────────────────────────

def test_endpoint_needs_same_origin_or_bearer(client, market, monkeypatch):
    monkeypatch.setenv("MCP_API_TOKEN", "s3cret")
    assert client.post("/api/intake/match", json=_job(),
                       headers={"Sec-Fetch-Site": "cross-site"}).status_code == 401
    assert client.post("/api/intake/match", json=_job()).status_code == 401
    assert _post(client, _job()).status_code == 200
    ok = client.post("/api/intake/match", json=_job(), headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200 and ok.get_json()["ok"] is True


@pytest.mark.parametrize("body, field", [
    ({"outcome": "short"}, "outcome"),
    (_job(milestones=[]), "milestones"),
    (_job(milestones=[{"title": "", "amount_usdc": 1}]), "milestones[0].title"),
    (_job(budget_usdc="lots"), "budget_usdc"),
    (_job(deadline="next week"), "deadline"),
])
def test_bad_requests_are_400(client, market, body, field):
    res = _post(client, body)
    assert res.status_code == 400 and res.get_json()["field"] == field


def test_only_stamped_agents_are_candidates_and_client_agent_data_is_ignored(client, market, monkeypatch):
    calls = _fake_llm(monkeypatch, _reply(market["research"].public_id))
    body = _job(agent={"id": market["hidden"].public_id, "input_price_per_1m": 0},
                candidates=[{"id": "AGT-FAKE"}])
    res = _post(client, body).get_json()
    ids = {c["id"] for c in calls[0]["user"]["candidates"]}
    assert ids == {market["research"].public_id, market["builder"].public_id}
    assert res["considered"] == 2 and res["agent"]["agent_id"] == market["research"].public_id
    # Preferring an unstamped agent doesn't make it a candidate either.
    calls.clear()
    _post(client, _job(preferred_agent_id=market["hidden"].public_id))
    assert market["hidden"].public_id not in {c["id"] for c in calls[0]["user"]["candidates"]}
    assert calls[0]["user"]["preferred_agent_id"] is None


def test_llm_answer_is_used_with_server_numbers(client, market, monkeypatch):
    calls = _fake_llm(monkeypatch, _reply(market["research"].public_id.lower(), cost_usdc=0.01))
    res = _post(client, _job()).get_json()
    assert calls[0]["temperature"] == 0.0 and calls[0]["system"] == mm.prompt.SYSTEM
    assert res["basis"] == "llm" and res["fallback_reason"] is None
    assert res["agent"]["agent_id"] == market["research"].public_id
    assert res["confidence"] == "high" and res["ask"].startswith("A priced competitor")
    cost = token_cost(res["tokens"], 3_000_000, 15_000_000)
    assert (res["cost_low_micro"], res["cost_high_micro"]) == (cost["low_micro"], cost["high_micro"])
    assert res["token_basis"] == "heuristic" and res["verdict"] == "fits" and res["within_budget"] is True
    assert "within your budget and timeframe" in res["headline"]


def test_llm_tokens_are_clamped_and_cost_recomputed(client, market, monkeypatch):
    huge = {"input": {"low": 10**12, "high": 10**12}, "output": {"low": 0, "high": 1}}
    _fake_llm(monkeypatch, _reply(market["research"].public_id, tokens=huge))
    res = _post(client, _job()).get_json()
    base = mm._estimate(mm.parse_job(_job()), None)["tokens"]
    assert res["token_basis"] == "llm"
    assert res["tokens"]["input"]["high"] <= base["input"]["high"] * mm.CLAMP_HIGH + 1000
    assert res["tokens"]["output"]["low"] >= base["output"]["low"] * mm.CLAMP_LOW - 1000
    cost = token_cost(res["tokens"], 3_000_000, 15_000_000)
    assert res["cost_high_micro"] == cost["high_micro"]


@pytest.mark.parametrize("reply", [
    "not json at all",
    json.dumps({"agent_id": None}),                                            # missing fields
    json.dumps({"ask": "x", "agent_id": "AGT-0000-0000-0", "can_do": True,     # not a candidate
                "reasons": ["x"], "confidence": "high"}),
    json.dumps({"ask": "x", "agent_id": None, "can_do": True, "reasons": ["x"], "confidence": "high"}),
    json.dumps({"ask": "x", "agent_id": None, "can_do": False, "reasons": ["x"], "confidence": "sure"}),
    json.dumps({"ask": "x", "agent_id": None, "can_do": "no", "reasons": ["x"], "confidence": "low"}),
])
def test_invalid_llm_answers_fall_back_to_rules(client, market, monkeypatch, reply):
    _fake_llm(monkeypatch, reply)
    res = _post(client, _job()).get_json()
    assert res["basis"] == "heuristic" and res["fallback_reason"] == "unavailable"
    assert res["agent"]["agent_id"] == market["research"].public_id


def test_unstamped_agent_named_by_the_model_is_rejected(client, market, monkeypatch):
    _fake_llm(monkeypatch, _reply(market["hidden"].public_id))
    res = _post(client, _job()).get_json()
    assert res["basis"] == "heuristic" and res["agent"]["agent_id"] != market["hidden"].public_id


def test_model_timeout_falls_back_to_rules(client, market, monkeypatch):
    _fake_llm(monkeypatch, RuntimeError("LLM unreachable: timed out"))
    res = _post(client, _job()).get_json()
    assert res["basis"] == "heuristic" and res["fallback_reason"] == "unavailable"


def test_rules_without_a_model(client, market):
    res = _post(client, _job()).get_json()
    assert res["basis"] == "heuristic" and res["fallback_reason"] == "not_configured"
    assert res["agent"]["agent_id"] == market["research"].public_id
    assert res["confidence"] in ("low", "medium")
    assert any("competitor" in r for r in res["reasons"])
    assert res["browse_url"] == "/marketplace?category=Research"


def test_over_budget_verdict(client, market):
    res = _post(client, _job(budget_usdc=0.5, milestones=[
        {"title": "Report", "amount_usdc": 0.5, "criteria": ["Sourced"]}])).get_json()
    assert res["verdict"] == "over_budget" and res["within_budget"] is False
    assert res["cost_high_micro"] > res["budget_micro"]
    assert "above your budget" in res["headline"]


def test_over_deadline_verdict(client, market):
    res = _post(client, _job(deadline={"mode": "date", "date": date.today().isoformat()})).get_json()
    assert res["verdict"] == "over_deadline" and res["within_timeframe"] is False
    assert res["deadline_fit"] == "short" and "deadline" in res["headline"]
    both = _post(client, _job(budget_usdc=0.5, milestones=[{"title": "R", "amount_usdc": 0.5, "criteria": []}],
                              deadline=date.today().isoformat())).get_json()
    assert both["verdict"] == "over_budget_and_deadline"
    flexible = _post(client, _job(deadline={"mode": "flexible", "date": ""})).get_json()
    assert flexible["within_timeframe"] is None and flexible["verdict"] == "fits"


def test_no_agent_can_do_it(client, market):
    body = _job(outcome="Cater a wedding dinner for 120 guests with a vegan menu and live music.",
                category="ops", milestones=[{"title": "Menu", "amount_usdc": 300, "criteria": ["Tasting"]}],
                budget_usdc=300)
    res = _post(client, body).get_json()
    assert res["agent"] is None and res["can_do"] is False and res["verdict"] == "no_agent"
    assert res["no_agent_reason"] and res["cost_low_micro"] is None
    assert res["browse_url"] == "/marketplace?category=Automation"


def test_model_can_say_no_agent(client, market, monkeypatch):
    _fake_llm(monkeypatch, _reply(None, can_do=False, reasons=[],
                                  no_agent_reason="None of these agents does visual design \u2014 sorry."))
    res = _post(client, _job()).get_json()
    assert res["basis"] == "llm" and res["agent"] is None and res["verdict"] == "no_agent"
    assert "\u2014" not in res["no_agent_reason"] and res["reasons"] == [res["no_agent_reason"]]


def test_doesnt_list_rules_an_agent_out():
    cand = {"id": "AGT-X", "name": "Copy", "category": "Content", "use_case": "", "description": "Landing copy",
            "about": "", "does": ["Landing page copy", "Headline variants"], "doesnt": ["Visual design or building the page"],
            "tags": [], "tools": [], "mcp_servers": [], "skills": [], "input_price_per_1m": 1, "output_price_per_1m": 1,
            "track_record": {"jobs": 1, "rating": 4.0, "reviews": 0, "on_time_rate": None}}
    job = mm.parse_job(_job(outcome="Visual design for our landing page, plus the headline copy.",
                            category="content"))
    assert mm._rule_score(job, cand)["excluded"] == "Visual design or building the page"
    decision = mm.heuristic_decision(job, [cand], {"AGT-X": mm._estimate(job, cand)})
    assert decision["agent_id"] is None and "does not do" in decision["no_agent_reason"]


# ── flow UI ─────────────────────────────────────────────────────────────────

def test_step_7_offers_accept_or_back_to_browsing_only(client):
    from pathlib import Path
    html = client.get("/new").get_data(as_text=True)
    step = html[html.index('data-step="7"'):html.index('data-step="8"')]
    assert "Checking the marketplace for an agent that can do this." in step
    assert 'name="agent"' not in step and "Matched agents" not in step
    assert 'id="match-browse"' in html and ">Back to browsing<" in html
    assert 'id="match-edit"' in html and ">Edit my job<" in html
    assert "\u2014" not in step
    flow = (Path(__file__).resolve().parents[1] / "app/static/js/flow.js").read_text()
    assert '"Accept"' in flow and "state.agent = agentChoice(r.agent)" in flow


def test_unstamped_agent_cannot_be_hired_from_its_profile(client, market):
    hidden = market["hidden"]
    profile = client.get(f"/agent/{hidden.id}").get_data(as_text=True)
    assert f"/jobs/new?agent={hidden.id}" not in profile
    assert "This agent can't be hired right now." in profile
    form = client.get(f"/jobs/new?agent={hidden.id}")
    assert form.status_code == 200
    text = form.get_data(as_text=True)
    assert "can't be hired right now" in text and 'id="job-form"' not in text
    post = client.post(f"/jobs/new?agent={hidden.id}", data={"outcome": "x", "budget_usdc": "5"})
    assert post.status_code == 409
    flow = client.get(f"/new?agent={hidden.id}").get_data(as_text=True)
    assert 'id="agent-notice"' in flow and "Unstamped Research can&#39;t be hired right now." in flow
    stamped = client.get(f"/agent/{market['research'].id}").get_data(as_text=True)
    assert f"/jobs/new?agent={market['research'].id}" in stamped


# ── MCP ─────────────────────────────────────────────────────────────────────

def test_mcp_match_agent_returns_the_matchmaker_answer(client, market):
    from agentslist_mcp import tools

    class Client:
        def match_job(self, payload):
            self.payload = payload
            return _post(client, payload).get_json()

    c = Client()
    out = tools.match_agent(c, _job()["outcome"], 600, milestones=[
        {"title": "Report", "acceptance": "- Sourced\n- One page summary", "amount_usdc": 600}], category="research")
    assert c.payload["milestones"][0]["criteria"] == ["Sourced", "One page summary"]
    assert out["ok"] and out["agent"]["agent_id"] == market["research"].public_id
    assert out["money_moved"] is False and "request_scope" in out["next_step"]
    assert tools.match_agent(c, "", 5)["code"] == "INVALID_REQUEST"
