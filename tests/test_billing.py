"""per_milestone listings: cards and profiles show what one milestone
costs; per-minute and per-token listings render as before."""
from tests.conftest import WALLET


def _listing(db, *, billing="per_milestone", low=400.0, high=2500.0):
    """A dev-stamped (hireable) listing, returned as its id."""
    from app.models import Agent
    from app.seller.stamp import dev_stamp
    row = Agent(name="Clause Review", description="Reviews contracts", category="Legal",
                billing=billing, min_price=low, max_price=high, current_price=low,
                seller=WALLET, deployer_wallet=WALLET)
    db.session.add(row)
    db.session.flush()
    dev_stamp(row)
    db.session.commit()
    return row.id


def test_card_shows_the_milestone_range(client, db):
    _listing(db)
    html = client.get("/marketplace").get_data(as_text=True)
    assert '<span class="amount">$400–$2,500</span> <span class="meta">per milestone</span>' in html


def test_card_shows_one_amount_when_the_range_is_flat(client, db):
    _listing(db, low=12.5, high=12.5)
    html = client.get("/marketplace").get_data(as_text=True)
    assert '<span class="amount">$12.50</span> <span class="meta">per milestone</span>' in html


def test_profile_prices_per_milestone(client, db):
    html = client.get(f"/agent/{_listing(db)}").get_data(as_text=True)
    assert "<dt>Starting at</dt>" in html and "$400 per milestone" in html
    assert "$400 to $2,500 per milestone" in html
    assert "<small>from</small> $400 <small>per milestone</small>" in html
    assert "Per milestone, paid from escrow when you approve it" in html
    assert "Per token processed" not in html and "Current rate" not in html


def test_per_minute_and_per_token_listings_render_as_before(client, db, agent):
    html = client.get(f"/agent/{agent}").get_data(as_text=True)
    assert "<dt>Current rate</dt>" in html and "$0.10 per min" in html
    assert "Per minute of work" in html and "per milestone" not in html
    token = _listing(db, billing="per_token", low=0.00002, high=0.00005)
    html = client.get(f"/agent/{token}").get_data(as_text=True)
    assert "$0.000020 per token" in html and "Per token processed" in html
    card = client.get("/marketplace").get_data(as_text=True)
    assert '<span class="amount">$0.000020</span> <span class="meta">per token</span>' in card
    assert "per milestone" not in card
