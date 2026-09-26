"""`flask seed`: sample listings carry an illustrative track record, and a
re-run refreshes it on existing sample rows without touching other
listings."""
from app.sample_data import SAMPLE_AGENTS, SAMPLE_SELLER, seed_sample_agents
from tests.conftest import WALLET


def _samples():
    from app.models import Agent
    return Agent.query.filter_by(seller=SAMPLE_SELLER).order_by(Agent.id).all()


def test_samples_have_varied_ratings_and_plausible_counts(db):
    from app.models import Agent
    assert seed_sample_agents(db, Agent) == {"added": len(SAMPLE_AGENTS), "updated": 0}
    rows = _samples()
    assert len(rows) == len(SAMPLE_AGENTS)
    for a in rows:
        assert 3.9 <= a.rating <= 4.9 and round(a.rating, 1) == a.rating, (a.name, a.rating)
        assert 0 < a.reviews <= a.tasks_completed, a.name
    assert len({a.rating for a in rows}) > 1


def test_rerun_refreshes_sample_rows_and_leaves_other_listings_alone(app, db):
    from app.models import Agent
    stale_spec, taken_spec = SAMPLE_AGENTS[0], SAMPLE_AGENTS[1]
    # A sample row seeded before samples had a track record, and a real
    # operator's listing that happens to share a sample name.
    db.session.add_all([
        Agent(name=stale_spec["name"], category=stale_spec["category"], billing="per_minute",
              seller=SAMPLE_SELLER, deployer_wallet=SAMPLE_SELLER.lower()),
        Agent(name=taken_spec["name"], category=taken_spec["category"], billing="per_minute",
              seller=WALLET, deployer_wallet=WALLET, rating=3.0, reviews=2, tasks_completed=2),
    ])
    db.session.commit()

    runner = app.test_cli_runner()
    first = runner.invoke(args=["seed"])
    assert first.exit_code == 0, first.output
    assert f"{len(SAMPLE_AGENTS) - 2} added, 1 refreshed" in first.output

    stale = Agent.query.filter_by(name=stale_spec["name"]).one()
    assert (stale.rating, stale.reviews, stale.tasks_completed) == (
        stale_spec["rating"], stale_spec["reviews"], stale_spec["tasks_completed"])
    taken = Agent.query.filter_by(name=taken_spec["name"]).one()
    assert (taken.seller, taken.rating, taken.reviews, taken.tasks_completed) == (WALLET, 3.0, 2, 2)

    second = runner.invoke(args=["seed"])
    assert second.exit_code == 0, second.output
    assert f"0 added, {len(SAMPLE_AGENTS) - 1} refreshed" in second.output
    assert Agent.query.count() == len(SAMPLE_AGENTS)
