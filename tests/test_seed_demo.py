"""`flask seed-demo`: idempotent demo listings with AGT ids, honest track
records and fail-closed screening addresses unless a map is configured."""
import json

from app.common import agent_ids
from app.demo_seed import (DEMO_AGENTS, DEMO_OPERATOR, demo_profile, placeholder_payout_address,
                           seed_demo_agents)

SEPOLIA_A, MAINNET_A = "0x" + "1" * 40, "0x" + "a" * 40
SEPOLIA_B, MAINNET_B = "0x" + "2" * 40, "0x" + "b" * 40


def _demo_rows():
    from app.models import Agent
    return Agent.query.filter_by(seller=DEMO_OPERATOR).order_by(Agent.id).all()


def test_cli_seeds_and_is_idempotent(app, db):
    runner = app.test_cli_runner()
    first = runner.invoke(args=["seed-demo"])
    assert first.exit_code == 0, first.output
    assert f"{len(DEMO_AGENTS)} added" in first.output
    ids_before = [(a.id, a.public_id) for a in _demo_rows()]

    second = runner.invoke(args=["seed-demo"])
    assert second.exit_code == 0, second.output
    assert "0 added" in second.output
    assert [(a.id, a.public_id) for a in _demo_rows()] == ids_before
    assert len(ids_before) == len(DEMO_AGENTS)


def test_agents_have_valid_agt_ids_and_full_profiles(db):
    from app.models import Agent
    seed_demo_agents(db, Agent)
    rows = _demo_rows()
    assert len({a.public_id for a in rows}) == len(rows)
    for a in rows:
        assert agent_ids.is_valid(a.public_id), a.public_id
        assert a.category and a.description and a.capabilities
        assert 0 < a.min_price <= a.current_price <= a.max_price
        assert a.billing == "per_token"
        assert 0 < a.input_price_per_1m < a.output_price_per_1m
        assert a.input_price_per_1m % 10_000 == 0 and a.output_price_per_1m % 10_000 == 0
        profile = demo_profile(a.name)
        for key in ("specialty", "does", "doesnt", "tools", "models", "sample_outputs", "faq"):
            assert profile[key], (a.name, key)
        assert all(s["label"] == "Example work" for s in profile["sample_outputs"])
        manifest = json.loads(a.manifest_json)
        assert manifest["agent_id"] == a.public_id
        assert manifest["payout_address"] == a.payout_address
        assert manifest["tools"] and manifest["skills"] and manifest["model"]
        assert manifest["price_min_micro"] == round(a.min_price * 1_000_000)


def test_manifest_hash_is_stable_across_reruns(db):
    from app.models import Agent
    from app.seller.stamp import current_manifest, manifest_hash
    seed_demo_agents(db, Agent, address_map="")
    before = {a.name: manifest_hash(current_manifest(a)) for a in _demo_rows()}
    seed_demo_agents(db, Agent, address_map="")
    assert {a.name: manifest_hash(current_manifest(a)) for a in _demo_rows()} == before


def test_not_hireable_until_stamped(db):
    from app.models import Agent
    from app.seller.stamp import DEV_STAMP_SUB, stamp_status
    seed_demo_agents(db, Agent, address_map="")
    assert {stamp_status(a).code for a in _demo_rows()} == {"NOT_STAMPED"}
    result = seed_demo_agents(db, Agent, address_map="", dev_stamp=True)
    assert result["dev_stamped"] == len(DEMO_AGENTS)
    for a in _demo_rows():
        assert stamp_status(a).ok and a.manifest_stamp_sub == DEV_STAMP_SUB
    seed_demo_agents(db, Agent, address_map="")  # a plain re-run keeps the stamps valid
    assert all(stamp_status(a).ok for a in _demo_rows())


def test_real_operator_stamp_is_never_overwritten(db):
    from app.models import Agent
    seed_demo_agents(db, Agent, address_map="")
    row = _demo_rows()[0]
    row.manifest_stamp_sub = "0x" + "9" * 64
    db.session.commit()
    result = seed_demo_agents(db, Agent, address_map="", dev_stamp=True)
    assert result["dev_stamped"] == len(DEMO_AGENTS) - 1
    assert _demo_rows()[0].manifest_stamp_sub == "0x" + "9" * 64


def test_cli_dev_stamp_refused_in_production(app, monkeypatch):
    monkeypatch.setitem(app.config, "ENV_NAME", "production")
    out = app.test_cli_runner().invoke(args=["seed-demo", "--dev-stamp"])
    assert out.exit_code != 0 and "Refusing --dev-stamp" in out.output


def test_demo_track_record_is_flagged_bounded_and_stable(db):
    """Demo listings carry an illustrative track record (flagged in the data,
    not rendered), within realistic bounds and identical on every run. No
    turnaround is seeded: it depends on the job, so it is not shown."""
    from app.models import Agent
    seed_demo_agents(db, Agent)
    first = {a.name: (a.tasks_completed, a.avg_completion_time, a.on_time_rate, a.repeat_hire_rate)
             for a in _demo_rows()}
    for a in _demo_rows():
        assert a.demo_listing is True
        assert 26 <= a.tasks_completed <= 164
        assert 0.88 <= a.on_time_rate <= 0.99 and 0.22 <= a.repeat_hire_rate <= 0.61
        assert a.avg_completion_time.strip() == "-"
        assert a.verified is False and a.featured is False
    seed_demo_agents(db, Agent)
    assert first == {a.name: (a.tasks_completed, a.avg_completion_time, a.on_time_rate,
                              a.repeat_hire_rate) for a in _demo_rows()}


def test_demo_ratings_are_spread_bounded_and_stable(db):
    """Each listing gets a rating from 3.7 to 4.9 (one decimal) derived from
    its slug, spread evenly across all 13 possible values (there are more
    listings than one-decimal values in the range, so it can't require every
    listing to be unique), and a review count of 12-140 that never exceeds
    its jobs delivered. A reseed restores the same values."""
    from app.demo_seed import demo_rating
    from app.models import Agent
    seed_demo_agents(db, Agent)
    rows = _demo_rows()
    for a in rows:
        assert 3.7 <= a.rating <= 4.9 and round(a.rating, 1) == a.rating, (a.name, a.rating)
        assert 12 <= a.reviews <= 140 and a.reviews <= a.tasks_completed, a.name
    assert len({a.rating for a in rows}) == min(len(rows), 13)
    assert min(a.rating for a in rows) == 3.7 and max(a.rating for a in rows) == 4.9
    assert {a.name: a.rating for a in rows} == {s["name"]: demo_rating(s["slug"]) for s in DEMO_AGENTS}
    before = {a.name: (a.rating, a.reviews) for a in rows}
    rows[0].rating, rows[0].reviews = 1.0, 999
    db.session.commit()
    seed_demo_agents(db, Agent)
    assert {a.name: (a.rating, a.reviews) for a in _demo_rows()} == before


def test_demo_reviews_are_seeded_once_and_render(client, db):
    """Three written reviews per listing, no duplicates on reseed, shown on
    the profile; cards show the rating and count instead of "No ratings yet"."""
    from app.demo_seed import DEMO_REVIEWS
    from app.models import Agent, Review
    seed_demo_agents(db, Agent, address_map="", dev_stamp=True)
    seed_demo_agents(db, Agent, address_map="", dev_stamp=True)
    assert Review.query.count() == 3 * len(DEMO_AGENTS)
    for spec in DEMO_AGENTS:
        assert len(DEMO_REVIEWS[spec["slug"]]) == 3
        for user, stars, date, text in DEMO_REVIEWS[spec["slug"]]:
            assert 1 <= stars <= 5 and "\u2014" not in text and len(text) < 300
    row = next(a for a in _demo_rows() if a.name == "Keelhaul Audit")
    html = client.get(f"/agent/{row.id}").get_data(as_text=True)
    assert "No reviews yet" not in html and "Marta V., protocol lead" in html
    assert f"Rating from {row.reviews} reviews" in html
    home = client.get("/marketplace").get_data(as_text=True)
    assert "No ratings yet" not in home
    assert f"{row.rating:.1f} out of 5 from {row.reviews} reviews" in home


def test_without_map_payouts_are_unmapped_placeholders(db):
    from app.models import Agent
    seed_demo_agents(db, Agent, address_map="")
    by_name = {a.name: a for a in _demo_rows()}
    for spec in DEMO_AGENTS:
        row = by_name[spec["name"]]
        assert row.payout_address == placeholder_payout_address(spec["slug"])
        assert row.screening_address is None


def test_unmapped_payee_is_refused_by_screening(app, db):
    from app.models import Agent
    from app.screening.service import screen
    seed_demo_agents(db, Agent, address_map="")
    row = _demo_rows()[0]
    v = screen("engagement.fund", chain_address=row.payout_address, amount_micro=1_000_000,
               agent_id=row.public_id)
    assert v["verdict"] == "REFUSE" and v["fail_closed"] is True
    assert any(r["code"] == "UNMAPPED_ADDRESS" for r in v["reasons"])


def test_map_entries_are_assigned_in_order_and_refreshed(db):
    from app.models import Agent
    seed_demo_agents(db, Agent, address_map="")
    raw = json.dumps({SEPOLIA_A: MAINNET_A, SEPOLIA_B: MAINNET_B})
    result = seed_demo_agents(db, Agent, address_map=raw)
    assert result["added"] == 0 and result["mapped"] and result["map_entries"] == 2
    rows = _demo_rows()
    assert [(r.payout_address, r.screening_address) for r in rows[:3]] == [
        (SEPOLIA_A, MAINNET_A), (SEPOLIA_B, MAINNET_B), (SEPOLIA_A, MAINNET_A)]


def test_leaves_other_listings_alone(db):
    from app.models import Agent
    spec = DEMO_AGENTS[0]
    other = Agent(name=spec["name"], description="someone else's", category="Security",
                  billing="per_minute", seller="0x" + "c" * 40)
    db.session.add(other)
    db.session.commit()
    result = seed_demo_agents(db, Agent)
    assert result["skipped"] == 1
    db.session.refresh(other)
    assert other.description == "someone else's"


def test_demo_agents_render_and_list_with_agt_ids(client, db):
    from app.models import Agent
    seed_demo_agents(db, Agent)
    rows = _demo_rows()
    for a in rows:
        assert client.get(f"/agent/{a.id}").status_code == 200
    listed = []
    page = 1
    while True:
        body = client.get(f"/api/agents?per_page=50&page={page}").get_json()
        listed.extend(body["agents"])
        if len(body["agents"]) < 50:
            break
        page += 1
    assert {a.public_id for a in rows} <= {x["agent_id"] for x in listed}


def test_demo_token_prices_follow_model_list_price_and_margin(db):
    """Token prices = the model's list price x the operator margin, in USDC
    micro-units per 1M tokens, the same on every run."""
    from app.demo_seed import DEMO_AGENTS, MODEL_LIST_PRICES, token_prices
    from app.models import Agent
    seed_demo_agents(db, Agent)
    rows = {a.name: a for a in _demo_rows()}
    for spec in DEMO_AGENTS:
        list_in, list_out = MODEL_LIST_PRICES[spec["model"][1]]
        assert 1.0 < spec["margin"] < 2.0
        row = rows[spec["name"]]
        assert (row.input_price_per_1m, row.output_price_per_1m) == token_prices(spec)
        assert abs(row.input_price_per_1m - list_in * spec["margin"] * 1e6) <= 5_000
        assert abs(row.output_price_per_1m - list_out * spec["margin"] * 1e6) <= 5_000
    audit = rows["Keelhaul Audit"]      # claude-opus at $5 / $25, margin 1.4
    assert (audit.input_price_per_1m, audit.output_price_per_1m) == (7_000_000, 35_000_000)
    seed_demo_agents(db, Agent)
    assert {a.name: (a.input_price_per_1m, a.output_price_per_1m) for a in _demo_rows()} == \
        {n: (r.input_price_per_1m, r.output_price_per_1m) for n, r in rows.items()}


def test_demo_icon_overrides_are_known_icons(db):
    from app.common.agent_icons import ICONS, icon_for
    from app.models import Agent
    seed_demo_agents(db, Agent)
    rows = {a.name: a for a in _demo_rows()}
    assert rows["Attest SOC 2 Readiness"].icon == "clipboard-check"
    assert rows["Keelhaul Audit"].icon is None
    assert icon_for(rows["Keelhaul Audit"]) == "shield-check"     # from its category
    assert all(icon_for(a) in ICONS for a in rows.values())
