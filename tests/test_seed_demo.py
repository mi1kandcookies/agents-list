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
    assert 8 <= len(ids_before) <= 10


def test_agents_have_valid_agt_ids_and_full_profiles(db):
    from app.models import Agent
    seed_demo_agents(db, Agent)
    rows = _demo_rows()
    assert len({a.public_id for a in rows}) == len(rows)
    for a in rows:
        assert agent_ids.is_valid(a.public_id), a.public_id
        assert a.category and a.description and a.capabilities
        assert 0 < a.min_price <= a.current_price <= a.max_price
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


def test_no_fake_track_record(db):
    from app.models import Agent, Review
    seed_demo_agents(db, Agent)
    for a in _demo_rows():
        assert (a.rating, a.reviews, a.tasks_completed, a.seller_rating) == (0.0, 0, 0, 0.0)
        assert a.verified is False and a.featured is False
    assert Review.query.count() == 0


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
    listed = client.get("/api/agents?per_page=50").get_json()["agents"]
    assert {a.public_id for a in rows} <= {x["agent_id"] for x in listed}
