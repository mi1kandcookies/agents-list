"""App shell and visual identity: key pages render, the main nav carries the
marketplace items, headline figures come from the database, and the
stylesheet no longer ships the old red/dark theme."""
import re
from pathlib import Path

import pytest

from tests.conftest import WALLET

CSS = Path(__file__).resolve().parent.parent / "app" / "static" / "css" / "main.css"


@pytest.mark.parametrize("url", ["/", "/marketplace", "/how-it-works", "/new", "/new?q=write+tests"])
def test_key_pages_render(client, agent, url):
    resp = client.get(url)
    assert resp.status_code == 200
    assert b"Agent's List" in resp.data


def test_agent_profile_renders(client, agent):
    html = client.get(f"/agent/{agent}").get_data(as_text=True)
    assert "Test Agent" in html
    assert f"/jobs/new?agent={agent}" in html    # Hire
    assert f"/new?agent={agent}" in html         # Get estimate
    assert "On-time rate" in html
    assert "Start protected work" in html
    assert f"/api/agents/{agent}/generate" not in html


def test_nav_contains_marketplace_items(client):
    html = client.get("/").get_data(as_text=True)
    nav = html[html.index('class="site-header"'):html.index("</header>")]
    for label, href in [("Browse agents", "/marketplace"), ("How it works", "/how-it-works"),
                        ("For operators", "/seller/"), ("Describe your job", "/new"),
                        ("Sign in", "/login")]:
        assert label in nav, label
        assert f'href="{href}' in nav, href
    assert "role-btn" not in nav
    assert "/admin" not in nav


def test_shell_uses_supplied_mark_and_institutional_type(client):
    html = client.get("/").get_data(as_text=True)
    assert 'img/agents-list-mark-transparent.png' in html
    assert 'family=Inter' in html and 'IBM+Plex+Mono' not in html
    css = CSS.read_text()
    assert "--font-body: 'Inter'" in css
    # Numbers use Inter's tabular lining figures, not a monospace face.
    assert "Plex" not in css and "--font-num:  \"Num Hyphen\", var(--font-body)" in css
    assert 'font-variant-numeric: tabular-nums lining-nums' in css


def _nav(html: str) -> str:
    return html[html.index('class="site-header"'):html.index("</header>")]


def test_nav_signed_out_offers_sign_in_and_hides_jobs(client):
    nav = _nav(client.get("/marketplace").get_data(as_text=True))
    assert 'href="/login?next=/marketplace"' in nav or 'href="/login?next=%2Fmarketplace"' in nav
    assert "Signed in" not in nav and "Sign out" not in nav and 'href="/jobs"' not in nav


def test_nav_signed_in_shows_short_id_jobs_and_sign_out(client, human):
    import hashlib
    with client.session_transaction() as sess:
        sess["human_id"], sess["human_sub"] = human.id, human.world_sub
    nav = _nav(client.get("/").get_data(as_text=True))
    short = hashlib.sha256(human.world_sub.encode()).hexdigest()[:8]
    # Header stays uncluttered: no "Signed in" label or id, just the account menu.
    assert "Signed in" not in nav and short not in nav and human.world_sub not in nav
    assert 'class="account-dot"' in nav
    assert 'href="/jobs"' in nav and ">Jobs<" in nav
    assert 'action="/logout"' in nav and "Sign out" in nav and 'href="/login' not in nav


def test_home_box_starts_the_guided_flow(client):
    html = client.get("/").get_data(as_text=True)
    form = html[html.index('id="home-describe"') - 80:]
    form = form[:form.index("</form>")]
    assert 'action="/new"' in form and 'method="get"' in form and 'name="q"' in form
    hero = html[html.index('class="home-search"'):]
    assert 'href="/marketplace"' in hero[:hero.index("</section>")]   # Browse agents
    assert "protection-row" not in hero   # integration strip was removed on request


def test_catalog_placeholder_new_view_is_gone(app):
    assert "catalog.new_job" not in app.view_functions
    assert app.url_map.bind("localhost").match("/new")[0] == "intake.new_job"


def test_home_leads_with_banner_then_search(client, db):
    html = client.get("/").get_data(as_text=True)
    assert html.index('class="hero"') < html.index('id="home-describe"')
    assert "img/home-banner-2880.webp" in html and "img/home-banner-3548.jpg" in html
    assert "agents on Agent&rsquo;s&nbsp;List:</h1>" in html   # live text, not baked into the image
    # Both banner sets are present; only the active one is displayed (CSS + pre-paint script).
    assert 'data-variant="field"' in html and 'data-variant="surf"' in html
    assert "img/hero-surf-2880.webp" in html and "data-banner-toggle" in html
    assert "Featured Agents" in html
    # The home page no longer shows a stats strip or the escrow line under the search.
    assert "stats-strip" not in html and "Agents listed" not in html
    assert "Funds held in escrow" not in html


def test_new_serves_the_guided_flow(client, agent):
    # /new is the guided flow (app/intake); agent matching happens in its
    # estimate step. Detailed coverage lives in tests/test_intake.py.
    html = client.get("/new?q=I+need+someone+to+write+tests").get_data(as_text=True)
    assert 'id="flow-form"' in html and "I need someone to write tests" in html
    html = client.get(f"/new?agent={agent}").get_data(as_text=True)
    assert "Test Agent" in html


def _stamp(db, agent_id):
    from app.models import Agent
    from app.seller.stamp import dev_stamp
    dev_stamp(db.session.get(Agent, agent_id))
    db.session.commit()


def test_marketplace_lists_only_hireable_agents(client, db, agent):
    assert "Test Agent" not in client.get("/marketplace").get_data(as_text=True)   # unstamped
    _stamp(db, agent)
    assert "Test Agent" in client.get("/marketplace").get_data(as_text=True)


def test_agent_cards_carry_no_trust_badges(client, db, agent):
    from app.models import Agent
    db.session.get(Agent, agent).verified = True
    db.session.commit()
    _stamp(db, agent)
    html = client.get("/marketplace").get_data(as_text=True)
    assert "Test Agent" in html
    assert "trust-badge" not in html and "Operator-stamped config" not in html


def test_payment_screening_badge_on_card_evidence_on_profile(client, db, agent):
    from app.models import Agent, Screening
    db.session.get(Agent, agent).payout_address = WALLET
    db.session.add(Screening(
        hop="payee.onboard", agent_id=agent, chain_address=WALLET.lower(),
        screened_address=WALLET.lower(), network="eip155:1", verdict="PAY",
        reasons=[], traits=["high_reputation"], toxic_score=4, provider="intercepta",
        fail_closed=False, latency_ms=42,
    ))
    db.session.commit()
    _stamp(db, agent)
    # Cards stay compact: no badges and no raw screening evidence.
    html = client.get("/marketplace").get_data(as_text=True)
    assert "Test Agent" in html
    assert "Screened payout" not in html and "toxicScore" not in html
    # The full screening evidence lives on the agent's profile.
    detail = client.get(f"/agent/{agent}").get_data(as_text=True)
    assert "Payment screening" in detail and "toxicScore 4" in detail and "high_reputation" in detail


def test_components_render(app):
    tpl = app.jinja_env.from_string(
        '{% from "components/status_pill.html" import status_pill %}'
        '{% from "components/verdict_slot.html" import verdict_slot %}'
        '{% from "components/mono_stat.html" import mono_stat %}'
        '{% from "components/approval_state.html" import approval_state %}'
        '{% from "components/chain_node.html" import chain_node %}'
        '{% from "components/stepper.html" import stepper %}'
        '{% from "components/empty_state.html" import empty_state %}'
        "{% for s in states %}{{ status_pill(s) }}{% endfor %}"
        "{% for v in verdicts %}{{ verdict_slot(v) }}{% endfor %}"
        "{{ verdict_slot() }}{{ mono_stat(None, 'x') }}{{ approval_state('approved') }}"
        "{{ chain_node('Funded', state='done') }}{{ stepper([{'label': 'A'}]) }}{{ empty_state('None') }}")
    states = ["draft", "pending", "approved", "denied", "expired",
              "funded", "in_progress", "submitted", "released", "refused"]
    html = tpl.render(states=states, verdicts=["PAY", "CAP", "REFUSE", "ASK_HUMAN"])
    for s in states:
        assert f"status-pill is-{s}" in html
    for v in ["pay", "cap", "refuse", "ask_human"]:
        assert f"verdict is-{v}" in html
    assert "Not screened yet" in html
    assert 'mono-stat-value is-unknown">—' in html


def test_stylesheet_has_no_legacy_red_or_dark_theme():
    css = CSS.read_text().upper()
    for legacy in ("#E84142", "#A32D2D", "#501313", "#CE2E2E", "#0A0A0A", "#1E1E1E"):
        assert legacy not in css, legacy
    assert 'DATA-THEME="DARK"' not in css
    assert "--C-ACCENT:    #4F46E5" in css
