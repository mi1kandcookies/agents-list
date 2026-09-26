"""resolve_payee: ENS payout record vs profile, fail-closed on mismatch, and
the source carried into hire / release approvals and the verdict card."""
from __future__ import annotations

import pytest

from app.names import service as names
from app.names.service import PayeeError
from chain.ens_v2 import ENSResolutionError
from tests.conftest import WALLET
from tests.test_engagements import (  # noqa: F401  (fixtures)
    _engagement, _hire, _mandate_key, agent_public_id, approve, screener,
)

PROFILE = WALLET.lower()
OTHER = "0x" + "d" * 40
NAME = "test-agent.agentslist-app.eth"


class FakeResolver:
    def __init__(self, record=None):
        self.record = record
        self.calls: list[str] = []

    def payout_address(self, name):
        self.calls.append(name)
        if isinstance(self.record, Exception):
            raise self.record
        return self.record


@pytest.fixture()
def resolver(app):
    fake = FakeResolver()
    app.extensions["ens_resolver"] = fake
    yield fake
    app.extensions.pop("ens_resolver", None)


@pytest.fixture()
def agent_row(db, agent):
    from app.models import Agent
    return db.session.get(Agent, agent)


@pytest.fixture()
def named(db, agent_row):
    from app.models import EnsName
    db.session.add(EnsName(name=NAME, kind="agent", agent_id=agent_row.id, status="active",
                           records={}, tx_hashes=[]))
    db.session.commit()
    return NAME


# ── resolve_payee ────────────────────────────────────────────────────────────
def test_profile_when_no_resolver(agent_row, named):
    p = names.resolve_payee(agent_row)
    assert (p.address, p.source, p.ens_name) == (PROFILE, "profile", None)


def test_profile_when_no_active_name(db, agent_row, resolver):
    resolver.record = PROFILE
    assert names.resolve_payee(agent_row).source == "profile"
    assert resolver.calls == []


def test_profile_when_name_is_not_active(db, agent_row, named, resolver):
    from app.models import EnsName
    db.session.get(EnsName, named).status = "revoked"
    db.session.commit()
    resolver.record = OTHER
    assert names.resolve_payee(agent_row).source == "profile"


def test_ens_when_record_matches_profile(agent_row, named, resolver):
    resolver.record = PROFILE.upper().replace("0X", "0x")
    p = names.resolve_payee(agent_row)
    assert (p.address, p.source, p.ens_name) == (PROFILE, "ens", NAME)
    assert resolver.calls == [NAME]


def test_profile_when_record_unset(agent_row, named, resolver):
    p = names.resolve_payee(agent_row)
    assert (p.address, p.source, p.ens_name) == (PROFILE, "profile", NAME)


def test_strict_ens_requires_an_active_name(app, agent_row, resolver):
    app.config["ENS_RESOLVE_PAYEES"] = True
    resolver.record = PROFILE
    with pytest.raises(PayeeError) as e:
        names.resolve_payee(agent_row)
    assert (e.value.code, e.value.status) == ("PAYEE_UNRESOLVED", 503)


def test_strict_ens_requires_a_payout_record(app, agent_row, named, resolver):
    app.config["ENS_RESOLVE_PAYEES"] = True
    with pytest.raises(PayeeError) as e:
        names.resolve_payee(agent_row)
    assert (e.value.code, e.value.status) == ("PAYEE_UNRESOLVED", 503)


def test_mismatch_fails_closed(agent_row, named, resolver):
    resolver.record = OTHER
    with pytest.raises(PayeeError) as e:
        names.resolve_payee(agent_row)
    assert (e.value.code, e.value.status) == ("PAYEE_MISMATCH", 403)


def test_record_without_profile_address_is_a_mismatch(db, agent_row, named, resolver):
    agent_row.deployer_wallet = None
    agent_row.payout_address = None
    db.session.commit()
    resolver.record = OTHER
    with pytest.raises(PayeeError) as e:
        names.resolve_payee(agent_row)
    assert e.value.code == "PAYEE_MISMATCH"


def test_lookup_failure_fails_closed(agent_row, named, resolver):
    resolver.record = ENSResolutionError("rpc down")
    with pytest.raises(PayeeError) as e:
        names.resolve_payee(agent_row)
    assert (e.value.code, e.value.status) == ("PAYEE_UNRESOLVED", 503)


def test_resolver_from_config(app):
    assert names.get_resolver() is None
    app.config["ENS_RESOLVE_PAYEES"] = True
    from chain.ens_v2 import UniversalResolver
    assert isinstance(names.get_resolver(), UniversalResolver)


# ── engagements use it ───────────────────────────────────────────────────────
def test_hire_carries_ens_source(client, world_idp, screener, agent_public_id, named, resolver):
    resolver.record = PROFILE
    eid = _engagement(client, agent_public_id)["engagement_id"]
    resp = _hire(client, eid)
    assert resp.status_code == 202, resp.get_json()
    body = resp.get_json()
    assert ["Payee address from", "ENS record"] in body["summary"]
    assert body["screening"]["payee_source"] == "ens"
    from app.approvals import service as approvals
    assert approvals.get(body["approval_id"]).action["payee_source"] == "ens"


def test_hire_without_resolver_is_profile(client, world_idp, screener, agent_public_id):
    eid = _engagement(client, agent_public_id)["engagement_id"]
    body = _hire(client, eid).get_json()
    assert ["Payee address from", "Agent profile"] in body["summary"]


def test_hire_refused_on_mismatch(client, db, world_idp, screener, agent_public_id, named, resolver):
    from app.models import Approval
    resolver.record = OTHER
    eid = _engagement(client, agent_public_id)["engagement_id"]
    resp = _hire(client, eid)
    assert resp.status_code == 403
    assert resp.get_json()["code"] == "PAYEE_MISMATCH"
    assert Approval.query.filter_by(engagement_id=eid).count() == 0


def test_fund_approval_re_resolves_ens_before_consuming(client, approve, screener,
                                                        agent_public_id, named, resolver):
    resolver.record = PROFILE
    eid = _engagement(client, agent_public_id)["engagement_id"]
    approval = _hire(client, eid).get_json()
    resolver.record = OTHER
    row = approve(approval["approval_id"])
    assert (row.state, row.failure_code) == ("blocked", "PAYEE_MISMATCH")
    assert client.get(f"/api/engagements/{eid}").get_json()["status"] == "scoped"
    from app.models import LedgerEntry
    assert LedgerEntry.query.filter_by(engagement_id=eid).count() == 0


def test_release_refused_on_mismatch(client, screener, approve, agent_public_id, named, resolver):
    resolver.record = PROFILE
    eid = _engagement(client, agent_public_id)["engagement_id"]
    apr = _hire(client, eid).get_json()
    assert approve(apr["approval_id"]).state == "consumed"
    resolver.record = OTHER
    resp = client.post(f"/api/engagements/{eid}/milestones/0/release", json={"flow": "device"})
    assert resp.status_code == 403 and resp.get_json()["code"] == "PAYEE_MISMATCH"
    resolver.record = PROFILE
    resp = client.post(f"/api/engagements/{eid}/milestones/0/release", json={"flow": "device"})
    assert resp.status_code == 202 and resp.get_json()["screening"]["payee_source"] == "ens"


def test_verdict_card_shows_payee_source(app):
    tmpl = app.jinja_env.from_string(
        '{% from "components/verdict_card.html" import verdict_card %}{{ verdict_card(v) }}')
    base = {"id": "SCR-1", "hop": "engagement.fund", "verdict": "PAY", "reasons": [],
            "subject": {"chain_address": PROFILE}, "latency_ms": 1, "provider": "fake"}
    assert 'data-payee-source="ens"' in tmpl.render(v={**base, "payee_source": "ens"})
    assert "Payee from ENS record" in tmpl.render(v={**base, "payee_source": "ens"})
    assert "Payee from agent profile" in tmpl.render(v={**base, "payee_source": "profile"})
    assert "Payee from" not in tmpl.render(v=base)
