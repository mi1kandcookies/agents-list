"""`flask seed-specialists`: first-party specialists become per-milestone demo
listings with a stable sample track record, whose operator manifest carries
the package's spec_hash; re-runs are idempotent and never touch another
seller's listing. Uses the kit's example specialist (tests/agentkit/fixtures)
and manifest-only specialists written to tmp_path; one test also lists
whatever specialist packages this checkout has under specialists/."""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml
from markupsafe import escape
from sqlalchemy import inspect

from agentkit.manifest import load_manifest, operator_fields, spec_hash
from app.common import agent_ids
from app.specialist_seed import (FIRST_PARTY_OPERATOR, placeholder_payout_address,
                                 sample_track_record, seed_specialists)
from tests.conftest import WALLET

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "tests" / "agentkit" / "fixtures"
EXAMPLE = FIXTURES / "example_specialist"
NAME = "Example Specialist"
IN_TREE = sorted(p.parent.name for p in (REPO / "specialists").glob("*/agent.yaml"))
TEN = [("appsec-pack", "Security"), ("bid-pack", "Business Operations"),
       ("data-pack", "Data & Analytics"), ("finance-pack", "Finance"),
       ("helpdesk-pack", "Customer Support"), ("law-pack", "Legal"),
       ("ledger-pack", "Accounting"), ("market-pack", "Research"),
       ("tests-pack", "Development"), ("upgrade-pack", "Development")]
SEPOLIA_A, MAINNET_A = "0x" + "1" * 40, "0x" + "a" * 40
SEPOLIA_B, MAINNET_B = "0x" + "2" * 40, "0x" + "b" * 40
OPERATOR_PAYOUT, OPERATOR_SUB = "0x" + "7" * 40, "0x" + "9" * 64


def _seed(db, root=FIXTURES, **kw):
    from app.models import Agent
    kw.setdefault("address_map", "")
    return seed_specialists(db, Agent, root=root, **kw)


def _cli(app, *args):
    return app.test_cli_runner().invoke(args=["seed-specialists", *args])


def _rows():
    from app.models import Agent
    return Agent.query.filter_by(seller=FIRST_PARTY_OPERATOR).order_by(Agent.id).all()


def _title(slug):
    return slug.replace("-", " ").title()


def _write_specialist(root: Path, slug: str, name: str, category: str, *, pricing=None) -> Path:
    """A manifest-only specialist package under ``root``."""
    pkg = root / slug.replace("-", "_")
    (pkg / "prompts").mkdir(parents=True)
    (pkg / "prompts" / "system.md").write_text("You draft carefully.", encoding="utf-8")
    draft = "deliverables/m1/draft.md"
    manifest = {
        "schema_version": 1, "slug": slug, "name": name, "version": "0.1.0",
        "summary": f"{name} drafts the work.",
        "listing": {"category": category, "description": f"{name}, described.",
                    "tags": ["draft"], "capabilities": [f"{name} drafts"],
                    "pricing": {"model": "per_milestone", "typical_low": 300,
                                "typical_high": 1200, **(pricing or {})}},
        "profile": "docs",
        "tools": ["read_file", "write_file", "submit_milestone"],
        "milestones": [{"id": "m1", "title": "Draft", "deliverables": [draft], "hours": [1, 2],
                        "acceptance": [{"check": "file_exists", "params": {"path": draft}}]}],
    }
    (pkg / "agent.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    return pkg


def _ten(tmp_path) -> Path:
    root = tmp_path / "specs"
    for slug, category in TEN:
        _write_specialist(root, slug, _title(slug), category)
    return root


def _columns(row) -> dict:
    return {a.key: getattr(row, a.key) for a in inspect(type(row)).column_attrs}


# --- listing ----------------------------------------------------------------------

def test_cli_seeds_and_is_idempotent(app, db, tmp_path):
    root = _ten(tmp_path)
    first = _cli(app, "--root", str(root))
    assert first.exit_code == 0, first.output
    assert "10 added, 0 refreshed" in first.output and "Not stamped" in first.output
    rows = _rows()
    assert f"{rows[0].public_id}  Appsec Pack  (Security, rated {rows[0].rating:.1f} " \
           f"from {rows[0].reviews} reviews)" in first.output
    before = [_columns(r) for r in rows]

    second = _cli(app, "--root", str(root))
    assert second.exit_code == 0, second.output
    assert "0 added, 10 refreshed" in second.output
    assert [_columns(r) for r in _rows()] == before


def test_cli_with_no_specialists_says_so(app, db, tmp_path):
    out = _cli(app, "--root", str(tmp_path))
    assert out.exit_code == 0 and "No specialists found." in out.output
    assert _rows() == []


def test_listing_and_manifest_come_from_agent_yaml(db):
    _seed(db)
    [row] = _rows()
    public = load_manifest(EXAMPLE).public_listing()
    assert agent_ids.is_valid(row.public_id)
    assert (row.name, row.description, row.long_description, row.category) == (
        NAME, public["summary"], public["description"], "Research")
    assert (row.billing, row.min_price, row.max_price, row.current_price) == (
        "per_milestone", 50.0, 150.0, 50.0)
    assert (row.model_provider, row.model_name) == ("Anthropic", "claude-opus-5")
    assert row.tags == public["tags"] and row.capabilities == public["capabilities"]
    assert (row.verified, row.featured, row.verification_tier) == (False, False, "none")
    assert row.payout_address == placeholder_payout_address("example-specialist")
    assert row.screening_address is None

    manifest = json.loads(row.manifest_json)
    fields = operator_fields(load_manifest(EXAMPLE))
    assert manifest["agent_id"] == row.public_id
    assert manifest["spec_hash"] == spec_hash(EXAMPLE) == fields["spec_hash"]
    assert (manifest["model"], manifest["tools"], manifest["skills"], manifest["mcp_servers"]) \
        == ("anthropic:claude-opus-5", fields["tools"], ["cited briefs"], [])
    # The x402 task price (1 USDC by default), not the per-milestone range.
    assert manifest["price_min_micro"] == manifest["price_max_micro"] == 1_000_000
    assert manifest["payout_address"] == row.payout_address


def test_manifest_hash_is_stable_across_reruns(db):
    from app.seller.stamp import current_manifest, manifest_hash
    _seed(db)
    before = manifest_hash(current_manifest(_rows()[0]))
    _seed(db)
    assert manifest_hash(current_manifest(_rows()[0])) == before


def test_task_price_comes_from_the_spec_and_respects_the_x402_cap(db, tmp_path):
    root = tmp_path / "specs"
    _write_specialist(root, "quote-pack", "Quote Pack", "Legal", pricing={"task_price_usdc": 4.1})
    _write_specialist(root, "pricey-pack", "Pricey Pack", "Legal", pricing={"task_price_usdc": 9})
    result = _seed(db, root=root, max_task_micro=5_000_000)
    assert [name for _, name, *_ in result["listed"]] == ["Quote Pack"]
    assert json.loads(_rows()[0].manifest_json)["price_min_micro"] == 4_100_000
    [(slug, message)] = result["problems"]
    assert slug == "pricey-pack" and "above X402_MAX_PAYMENT_USDC (5 USDC)" in message
    _seed(db, max_task_micro=250_000)   # the 1 USDC default is capped too
    example = next(r for r in _rows() if r.name == NAME)
    assert json.loads(example.manifest_json)["price_max_micro"] == 250_000


def test_new_categories_are_listed_and_unknown_ones_refused(app, db, tmp_path):
    root = tmp_path / "specs"
    for slug, category in (("law-pack", "Legal"), ("ledger-pack", "Accounting"),
                           ("bid-pack", "Business Operations"),
                           ("helpdesk-pack", "Customer Support"), ("tax-pack", "Tax")):
        _write_specialist(root, slug, _title(slug), category)
    _write_specialist(root, "fixed-pack", "Fixed Pack", "Legal", pricing={"model": "fixed"})
    (root / "broken").mkdir()
    (root / "broken" / "agent.yaml").write_text("slug: [", encoding="utf-8")
    out = _cli(app, "--root", str(root))
    assert out.exit_code != 0 and "3 specialist(s) could not be listed" in out.output
    assert {r.name: r.category for r in _rows()} == {
        "Bid Pack": "Business Operations", "Helpdesk Pack": "Customer Support",
        "Law Pack": "Legal", "Ledger Pack": "Accounting"}
    assert all(r.billing == "per_milestone" for r in _rows())
    assert "tax-pack: category 'Tax' is not a catalog category" in out.output
    assert "fixed-pack: pricing must be per_milestone in USDC, not fixed" in out.output
    assert "  broken: " in out.output


# --- sample track record ---------------------------------------------------------------

def test_sample_track_record_is_stable_and_covers_the_range():
    records = [sample_track_record(f"pack-{i}") for i in range(400)]
    assert {r["rating"] for r in records} == {x / 10 for x in range(37, 50)}
    assert {r["reviews"] for r in records} <= set(range(12, 181))
    assert all(r["reviews"] <= r["jobs"] <= 3 * r["reviews"] for r in records)
    assert sample_track_record("contract-review") == sample_track_record("contract-review")


def test_ten_specialists_get_a_stable_flagged_track_record(db, tmp_path):
    root = _ten(tmp_path)
    _seed(db, root=root)
    rows = _rows()
    assert len(rows) == 10
    for row in rows:
        slug = row.name.lower().replace(" ", "-")
        track = sample_track_record(slug)
        assert row.demo_listing is True
        assert (row.rating, row.reviews, row.tasks_completed) == (
            track["rating"], track["reviews"], track["jobs"])
        assert 3.7 <= row.rating <= 4.9 and round(row.rating, 1) == row.rating
        assert 12 <= row.reviews <= 180 and row.tasks_completed >= row.reviews
    assert len({r.rating for r in rows}) >= 4     # spread over the range, not one value
    before = [(r.rating, r.reviews, r.tasks_completed) for r in rows]
    _seed(db, root=root)
    assert [(r.rating, r.reviews, r.tasks_completed) for r in _rows()] == before


def test_rerun_keeps_recorded_reviews_and_admin_decisions(client, db):
    _seed(db)
    row = _rows()[0]
    seeded = (row.rating, row.reviews)
    resp = client.post(f"/api/agents/{row.id}/rate", json={"rating": 1, "user": "buyer"})
    assert resp.status_code == 200 and resp.get_json()["reviews"] == seeded[1] + 1
    row = _rows()[0]
    recorded = (row.rating, row.reviews)
    row.verified, row.featured, row.verification_tier = True, True, "suspended"
    db.session.commit()
    result = _seed(db)
    assert (result["added"], result["updated"]) == (0, 1)
    row = _rows()[0]
    assert (row.rating, row.reviews) == recorded != seeded
    assert row.tasks_completed >= row.reviews
    assert (row.verified, row.featured, row.verification_tier) == (True, True, "suspended")
    row.reviews, row.tasks_completed = 400, 5
    db.session.commit()
    _seed(db)
    assert (_rows()[0].reviews, _rows()[0].tasks_completed) == (400, 400)


# --- other sellers -------------------------------------------------------------------

def test_other_sellers_listings_are_never_touched(app, client, db, tmp_path):
    """A third party's same-named listing (whatever seller name it typed into
    /seller/create) is never adopted; it is reported and the command exits
    non-zero. Unrelated listings stay exactly as they were."""
    from app.models import Agent
    root = tmp_path / "specs"
    for slug in ("law-pack", "ledger-pack", "bid-pack"):
        _write_specialist(root, slug, _title(slug), "Legal")
    base = {"description": "someone else's", "category": "Legal", "billing": "per_minute",
            "min_price": "0.05", "max_price": "0.2", "wallet": WALLET}
    for name, seller in (("Law Pack", "0x" + "c" * 40), ("Ledger Pack", FIRST_PARTY_OPERATOR),
                         ("Unrelated Agent", "Law Co")):
        resp = client.post("/seller/create", json={**base, "name": name, "seller": seller})
        assert resp.status_code == 201, resp.get_json()
    others = Agent.query.filter(Agent.seller != FIRST_PARTY_OPERATOR).all() + [
        Agent.query.filter_by(name="Ledger Pack").one()]
    before = {r.id: _columns(r) for r in others}

    out = _cli(app, "--root", str(root), "--dev-stamp")
    assert out.exit_code != 0
    assert "law-pack: another seller's listing is already named 'Law Pack'" in out.output
    assert "ledger-pack: another seller's listing is already named 'Ledger Pack'" in out.output
    assert "1 added" in out.output
    assert [r.name for r in _rows() if r.demo_listing] == ["Bid Pack"]
    assert {r.id: _columns(db.session.get(Agent, r.id)) for r in others} == before


def test_demo_and_third_party_pages_render_the_same_after_seeding(app, client, db, tmp_path):
    from app.demo_seed import seed_demo_agents
    from app.models import Agent
    seed_demo_agents(db, Agent, address_map="", dev_stamp=True)
    demo = Agent.query.filter_by(name="Keelhaul Audit").one()
    resp = client.post("/seller/create", json={
        "name": "Ledger Helper", "description": "d", "category": "Finance",
        "billing": "per_token", "wallet": WALLET})
    third = resp.get_json()["agentId"]
    pages = {url: client.get(url).get_data(as_text=True)
             for url in (f"/agent/{demo.id}", f"/agent/{third}", f"/seller/agents/{third}")}
    _seed(db, root=_ten(tmp_path), dev_stamp=True)
    assert {url: client.get(url).get_data(as_text=True) for url in pages} == pages


# --- stamps and addresses ----------------------------------------------------------------

def test_hireable_only_after_a_dev_stamp(db):
    from app.seller.stamp import DEV_STAMP_SUB, NotHireable, assert_hireable, stamp_status
    _seed(db)
    [row] = _rows()
    assert stamp_status(row).code == "NOT_STAMPED"
    with pytest.raises(NotHireable):
        assert_hireable(row)
    assert _seed(db, dev_stamp=True)["dev_stamped"] == 1
    [row] = _rows()
    assert_hireable(row)
    assert row.manifest_stamp_sub == DEV_STAMP_SUB
    _seed(db)   # a plain re-run keeps the stamp valid
    assert stamp_status(_rows()[0]).ok


def test_cli_dev_stamp_refused_in_production(app, monkeypatch):
    monkeypatch.setitem(app.config, "ENV_NAME", "production")
    out = _cli(app, "--root", str(FIXTURES), "--dev-stamp")
    assert out.exit_code != 0 and "Refusing --dev-stamp" in out.output
    assert _rows() == []


def _operator_stamp(db, row, payout=OPERATOR_PAYOUT):
    """What a real operator does: set their own payout in the manifest editor
    and stamp it with World ID (payee.onboard screened PAY)."""
    from app.models import Screening
    from app.seller import stamp
    fields = {k: v for k, v in stamp.current_manifest(row).items() if k not in ("v", "agent_id")}
    fields["payout_address"] = payout
    row.manifest_hash = stamp.save_manifest(row, stamp.build_manifest(row, **fields))
    row.manifest_stamped_at, row.manifest_stamp_sub = datetime.now(timezone.utc), OPERATOR_SUB
    row.payout_address = payout
    db.session.add(Screening(hop="payee.onboard", agent_id=row.id, chain_address=payout,
                             verdict="PAY"))
    db.session.commit()
    assert stamp.stamp_status(row).ok


def test_real_operator_stamp_and_payout_survive_reruns(db):
    from app.screening.service import screen
    from app.seller.stamp import stamp_status
    raw = json.dumps({SEPOLIA_A: MAINNET_A})
    _seed(db, address_map=raw)
    assert (_rows()[0].payout_address, _rows()[0].screening_address) == (SEPOLIA_A, MAINNET_A)
    _operator_stamp(db, _rows()[0])
    for dev in (False, True):
        result = _seed(db, address_map=raw, dev_stamp=dev)
        assert (result["dev_stamped"], result["restamp"]) == (0, [])
        row = _rows()[0]
        assert row.manifest_stamp_sub == OPERATOR_SUB and stamp_status(row).ok
        assert row.payout_address == json.loads(row.manifest_json)["payout_address"] \
            == OPERATOR_PAYOUT
        # The map's address screened the seed's payout; it never vouches for the operator's.
        assert row.screening_address is None
    v = screen("engagement.fund", chain_address=OPERATOR_PAYOUT, amount_micro=1_000_000,
               agent_id=_rows()[0].public_id)
    assert v["verdict"] == "REFUSE" and "UNMAPPED_ADDRESS" in [r["code"] for r in v["reasons"]]


def test_a_changed_spec_is_reported_for_restamp(app, db, tmp_path):
    from app.seller.stamp import stamp_status
    root = tmp_path / "specs"
    pkg = _write_specialist(root, "law-pack", "Law Pack", "Legal")
    _seed(db, root=root)
    _operator_stamp(db, _rows()[0])
    assert _seed(db, root=root)["restamp"] == []
    (pkg / "prompts" / "system.md").write_text("You draft differently.", encoding="utf-8")
    out = _cli(app, "--root", str(root))
    assert out.exit_code == 0, out.output
    assert "Re-stamp required (the manifest changed since it was stamped): Law Pack" in out.output
    row = _rows()[0]
    assert stamp_status(row).code == "RESTAMP_REQUIRED"
    assert json.loads(row.manifest_json)["payout_address"] == OPERATOR_PAYOUT


def test_map_entries_go_in_slug_order_and_assigned_payouts_stay(db, tmp_path):
    from app.seller.stamp import current_manifest, manifest_hash
    root = tmp_path / "specs"
    for slug in ("beta-pack", "gamma-pack", "delta-pack"):
        _write_specialist(root, slug, _title(slug), "Legal")
    _seed(db, root=root)   # no map yet: placeholders, assigned once a map is set
    raw = json.dumps({SEPOLIA_A: MAINNET_A, SEPOLIA_B: MAINNET_B})
    result = _seed(db, root=root, address_map=raw)
    assert result["added"] == 0 and result["mapped"] and result["map_entries"] == 2
    before = {r.name: (r.payout_address, r.screening_address, manifest_hash(current_manifest(r)))
              for r in _rows()}
    assert {k: v[:2] for k, v in before.items()} == {
        "Beta Pack": (SEPOLIA_A, MAINNET_A), "Delta Pack": (SEPOLIA_B, MAINNET_B),
        "Gamma Pack": (SEPOLIA_A, MAINNET_A)}
    _write_specialist(root, "alpha-pack", "Alpha Pack", "Legal")   # sorts first
    assert _seed(db, root=root, address_map=raw)["added"] == 1
    after = {r.name: (r.payout_address, r.screening_address, manifest_hash(current_manifest(r)))
             for r in _rows()}
    assert {k: after[k] for k in before} == before
    assert after["Alpha Pack"][:2] == (SEPOLIA_A, MAINNET_A)


def test_unmapped_placeholder_payee_is_refused_by_screening(db):
    from app.screening.service import screen
    _seed(db)
    row = _rows()[0]
    v = screen("engagement.fund", chain_address=row.payout_address, amount_micro=1_000_000,
               agent_id=row.public_id)
    assert v["verdict"] == "REFUSE" and v["fail_closed"] is True


# --- pages -------------------------------------------------------------------------

def test_specialists_show_ratings_and_milestone_prices(client, db, tmp_path):
    _seed(db, root=_ten(tmp_path), dev_stamp=True)
    rows = _rows()
    market = client.get("/marketplace").get_data(as_text=True)
    for row in rows:
        assert str(escape(row.name)) in market
        assert f"{row.rating:.1f} out of 5 from {row.reviews} reviews" in market
    assert market.count('<span class="demo-tag"') == 10
    assert '<span class="amount">$300–$1,200</span> <span class="meta">per milestone</span>' \
        in market
    row = rows[0]
    profile = client.get(f"/agent/{row.id}").get_data(as_text=True)
    assert "Demo listing" in profile and f"Rating from {row.reviews} reviews" in profile
    assert f"{row.rating:.1f}" in profile and "$300 to $1,200 per milestone" in profile
    listed = {a["agent_id"]: a for a in
              client.get("/api/agents?per_page=50").get_json()["agents"]}
    assert {listed[r.public_id]["billing"] for r in rows} == {"per_milestone"}
    assert all(listed[r.public_id]["rating"] == r.rating and listed[r.public_id]["demo_listing"]
               for r in rows)


@pytest.mark.skipif(not IN_TREE, reason="no specialist packages under specialists/ here")
def test_every_specialist_in_the_tree_is_listed(client, db):
    result = _seed(db, root=None, dev_stamp=True)   # the default root: specialists/
    assert result["problems"] == []
    rows = _rows()
    assert len(rows) == len(IN_TREE) == result["dev_stamped"]
    market = client.get("/marketplace").get_data(as_text=True)
    for row in rows:
        assert row.billing == "per_milestone" and row.demo_listing
        assert 3.7 <= row.rating <= 4.9 and row.tasks_completed >= row.reviews >= 12
        assert str(escape(row.name)) in market
        assert client.get(f"/agent/{row.id}").status_code == 200


def test_the_web_app_boots_without_importing_agentkit():
    code = ("import sys; from app import create_app; app = create_app('testing'); "
            "print('seed-specialists' in app.cli.commands, "
            "sorted(m for m in sys.modules if m.split('.')[0] == 'agentkit'))")
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True,
                         text=True, timeout=120, env={**os.environ, "FLASK_ENV": "testing"})
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().splitlines()[-1] == "True []"
