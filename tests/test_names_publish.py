"""`flask names publish-agents` and agent-name updates, with the sidecar faked."""
from __future__ import annotations

import pytest

from app.names import service
from app.names.client import SidecarError
from tests.conftest import WALLET
from tests.test_names import ROOT, FakeSidecar

PAYOUT = WALLET.lower()


class LiveishSidecar(FakeSidecar):
    """FakeSidecar plus /health and root setup."""

    def __init__(self, *, mode="dry_run", root=ROOT, root_status="active"):
        super().__init__()
        self.state = {"ok": True, "mode": mode, "root": root, "root_status": root_status}

    def health(self):
        self.calls.append(("health", {}))
        return dict(self.state)

    def setup_root(self, *, register=False):
        if not register and self.state["root_status"] == "unregistered":
            raise SidecarError("not registered yet", "ROOT_NOT_REGISTERED", 409)
        return self._call("root", register=register)


@pytest.fixture()
def sidecar(app):
    fake = LiveishSidecar()
    app.extensions["names_client"] = fake
    yield fake
    app.extensions.pop("names_client", None)


@pytest.fixture()
def stamped(db, agent):
    from app.models import Agent
    from app.seller import stamp
    row = db.session.get(Agent, agent)
    stamp.dev_stamp(row)
    db.session.commit()
    return row


@pytest.fixture()
def unstamped(db):
    from app.models import Agent
    row = Agent(name="Unstamped Agent", description="x", category="Development", billing="per_minute",
                min_price=0.05, max_price=0.2, current_price=0.1, seller=WALLET, deployer_wallet=WALLET)
    db.session.add(row)
    db.session.commit()
    return row


def _run(app, *args):
    return app.test_cli_runner().invoke(args=["names", "publish-agents", *args])


def test_names_every_hireable_agent_and_skips_the_rest(app, sidecar, stamped, unstamped):
    result = _run(app)
    assert result.exit_code == 0, result.output
    assert f"skip  {unstamped.public_id}  Unstamped Agent: NOT_STAMPED" in result.output
    assert f"ok    {stamped.public_id}  test-agent.{ROOT} (active, 1 tx)" in result.output
    assert sidecar.methods() == ["health", "agent"]
    body = sidecar.calls[1][1]
    assert body["label"] == "test-agent" and body["grantee"] == PAYOUT
    assert body["records"] == {"context": "Does tests", "payout": PAYOUT,
                               "manifest_hash": stamped.manifest_hash}
    assert stamped.ens_name == f"test-agent.{ROOT}"


def test_resends_active_names_and_keeps_them_active_on_failure(app, sidecar, stamped):
    assert _run(app).exit_code == 0
    sidecar.fail["agent"] = SidecarError("rpc down", "CHAIN_ERROR", 502)
    result = _run(app)
    assert result.exit_code == 1
    assert "FAIL" in result.output and "CHAIN_ERROR: rpc down" in result.output
    assert sidecar.methods().count("agent") == 2
    from app.models import EnsName
    assert EnsName.query.filter_by(agent_id=stamped.id).one().status == "active"


def test_live_sidecar_needs_confirmation(app, sidecar, stamped):
    sidecar.state["mode"] = "live"
    result = _run(app)
    assert result.exit_code != 0 and "--confirm-live" in result.output
    assert sidecar.methods() == ["health"]
    assert _run(app, "--confirm-live").exit_code == 0
    assert "agent" in sidecar.methods()


def test_refuses_a_sidecar_with_another_root(app, sidecar, stamped):
    sidecar.state["root"] = "someone-else.eth"
    result = _run(app)
    assert result.exit_code != 0 and "someone-else.eth" in result.output
    assert "agent" not in sidecar.methods()


def test_adopts_the_root_first(app, sidecar, stamped):
    sidecar.state["root_status"] = "missing"
    assert _run(app).exit_code == 0
    assert sidecar.methods() == ["health", "root", "agent"]
    assert sidecar.calls[1] == ("root", {"register": False})


def test_registers_a_new_root_only_when_asked(app, sidecar, stamped):
    sidecar.state["root_status"] = "unregistered"
    result = _run(app)
    assert result.exit_code != 0 and "--setup-root" in result.output
    assert "agent" not in sidecar.methods()
    assert _run(app, "--setup-root").exit_code == 0
    assert ("root", {"register": True}) in sidecar.calls


def test_plan_sends_and_stores_nothing(app, sidecar, stamped):
    app.config["PUBLIC_BASE_URL"] = "https://agents.example/"
    result = _run(app, "--plan")
    assert result.exit_code == 0
    assert f"plan  {stamped.public_id}  test-agent.{ROOT} (pending)" in result.output
    assert f"web: https://agents.example/agent/{stamped.id}" in result.output
    assert sidecar.calls == []
    from app.models import EnsName
    assert EnsName.query.count() == 0


def test_web_endpoint_needs_an_https_origin(app, stamped):
    app.config["PUBLIC_BASE_URL"] = "http://127.0.0.1:8090"
    assert "web" not in service._agent_records(stamped)
    app.config["PUBLIC_BASE_URL"] = "https://agents.example"
    assert service._agent_records(stamped)["web"] == f"https://agents.example/agent/{stamped.id}"


def test_a_failed_record_update_keeps_the_name_active(db, sidecar, stamped):
    row = service.on_agent_published(stamped)
    assert row.status == "active"
    stamped.description = "Now also writes docs"
    sidecar.fail["agent"] = SidecarError("down", "UNREACHABLE")
    row = service.on_agent_published(stamped)
    assert row.status == "active"          # the name still resolves; payments keep working
    assert row.records["context"] == "Does tests"   # what is on-chain, so the next publish retries
    row = service.on_agent_published(stamped)
    assert row.status == "active" and row.records["context"] == "Now also writes docs"
    assert sidecar.methods() == ["agent", "agent", "agent"]


def test_manifest_hash_is_published_only_while_the_stamp_is_valid(db, stamped):
    assert service._agent_records(stamped)["manifest_hash"] == stamped.manifest_hash
    stamped.payout_address = "0x" + "e" * 40      # changed payout: re-stamp required
    records = service._agent_records(stamped)
    assert "manifest_hash" not in records and records["payout"] == "0x" + "e" * 40


def test_named_agents_that_stop_being_hireable_stay_in_sync(app, db, sidecar, stamped):
    assert _run(app).exit_code == 0                      # named while hireable
    stamped.verification_tier = "suspended"
    db.session.commit()
    result = _run(app)
    assert result.exit_code == 0
    assert f"ok    {stamped.public_id}  test-agent.{ROOT} (active" in result.output
    assert "not hireable: UNLISTED)" in result.output
    body = sidecar.calls[-1][1]
    # Unlisting keeps the payee: work already under way can still be paid.
    assert body["records"]["payout"] == PAYOUT and body["grantee"] == PAYOUT


def test_retry_resends_an_active_agent_name(client, db, sidecar, stamped):
    from app.models import EnsName
    row = service.on_agent_published(stamped)
    stamped.description = "Now also writes docs"
    sidecar.fail["agent"] = SidecarError("down", "UNREACHABLE")
    service.on_agent_published(stamped)
    resp = client.post(f"/api/names/{row.name}/retry")
    assert resp.status_code == 200 and resp.get_json()["status"] == "active"
    assert resp.get_json()["error"] is None
    assert sidecar.methods().count("agent") == 3
    db.session.expire_all()
    assert db.session.get(EnsName, row.name).records["context"] == "Now also writes docs"


def test_retry_recomputes_the_records_it_sends(client, db, sidecar, stamped):
    from app.models import Screening
    sidecar.fail["agent"] = SidecarError("down", "UNREACHABLE")
    row = service.on_agent_published(stamped)
    assert row.status == "failed" and row.records["payout"] == PAYOUT
    db.session.add(Screening(hop="payee.onboard", agent_id=stamped.id, chain_address=PAYOUT,
                             verdict="REFUSE"))
    db.session.commit()
    assert client.post(f"/api/names/{row.name}/retry").get_json()["status"] == "active"
    body = sidecar.calls[-1][1]
    assert "payout" not in body["records"] and body["grantee"] is None


def test_retry_reports_a_failed_update(client, db, sidecar, stamped):
    row = service.on_agent_published(stamped)
    sidecar.fail["agent"] = SidecarError("rpc down", "CHAIN_ERROR", 502)
    body = client.post(f"/api/names/{row.name}/retry").get_json()
    assert body["status"] == "active" and body["error"] == "CHAIN_ERROR: rpc down"


def test_an_unstamped_agent_advertises_its_payee_but_no_manifest_hash(db, sidecar, unstamped):
    service.on_agent_published(unstamped)
    body = sidecar.calls[-1][1]
    assert body["records"]["payout"] == PAYOUT and "manifest_hash" not in body["records"]
    assert body["grantee"] == PAYOUT


def test_a_refused_payee_is_not_advertised(db, stamped):
    from app.models import Screening
    db.session.add(Screening(hop="payee.onboard", agent_id=stamped.id, chain_address=PAYOUT,
                             verdict="REFUSE"))
    db.session.commit()
    records = service._agent_records(stamped)
    assert "payout" not in records and "manifest_hash" not in records


def test_a_refused_payee_loses_its_endpoint_grants(db, sidecar, stamped):
    from app.models import Screening
    db.session.add(Screening(hop="payee.onboard", agent_id=stamped.id, chain_address=PAYOUT,
                             verdict="REFUSE"))
    db.session.commit()
    service.on_agent_published(stamped)
    body = sidecar.calls[-1][1]
    assert "grantee" in body and body["grantee"] is None   # null revokes on the sidecar
