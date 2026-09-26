"""Legal, Accounting, Business Operations and Customer Support: appended to
the catalog categories and listed, filtered and edited like the original
seven."""
from tests.conftest import WALLET

NEW = {"Legal": "LAW", "Accounting": "ACC", "Business Operations": "BIZ",
       "Customer Support": "SUP"}


def _listing(db, category, name, *, verified=False):
    """A dev-stamped (hireable) per-minute listing in ``category``."""
    from app.models import Agent
    from app.seller.stamp import dev_stamp
    row = Agent(name=name, description=f"{category} work", category=category,
                billing="per_minute", min_price=0.05, max_price=0.2, current_price=0.1,
                seller=WALLET, deployer_wallet=WALLET, verified=verified)
    db.session.add(row)
    db.session.flush()
    dev_stamp(row)
    db.session.commit()
    return row.id


def test_new_categories_come_after_the_original_seven():
    from app.services import CATEGORIES
    assert CATEGORIES[:7] == ["Development", "Data & Analytics", "Content", "Finance",
                              "Research", "Security", "Automation"]
    assert CATEGORIES[7:] == list(NEW)


def test_marketplace_filters_and_labels_the_new_categories(client, db):
    for category in NEW:
        _listing(db, category, f"{category} Agent")
    html = client.get("/marketplace").get_data(as_text=True)
    for category, code in NEW.items():
        assert f'<option value="{category}"' in html
        assert f'aria-hidden="true">{code}</span>' in html
    only = client.get("/marketplace?category=Customer Support").get_data(as_text=True)
    assert "Customer Support Agent" in only and "Legal Agent" not in only


def test_agent_profile_suggests_complementary_categories(client, db):
    legal = _listing(db, "Legal", "Clause Agent")
    _listing(db, "Business Operations", "Proposal Agent", verified=True)
    _listing(db, "Customer Support", "Helpdesk Agent", verified=True)
    html = client.get(f"/agent/{legal}").get_data(as_text=True)
    assert "Proposal Agent" in html and "Helpdesk Agent" not in html


def test_seller_can_pick_or_move_to_a_new_category(client, db, agent):
    from app.models import Agent
    assert '<option value="Legal"' in client.get("/seller/create").get_data(as_text=True)
    resp = client.post(f"/seller/agents/{agent}",
                       json={"action": "update", "category": "Accounting"})
    assert resp.status_code == 200
    assert db.session.get(Agent, agent).category == "Accounting"
    client.post(f"/seller/agents/{agent}", json={"action": "update", "category": "Tax"})
    assert db.session.get(Agent, agent).category == "Accounting"   # unknown: ignored
