"""scripts/demo_check.py against the app in-process.

The script never fakes approvals; here the *test* plays the human by
answering the device flow on the fake World ID provider whenever the script
asks the human to act. That checks the script's step logic and summary
without a phone or network."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from app.extensions import db as _db

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "demo_check.py"


def _load():
    spec = importlib.util.spec_from_file_location("demo_check", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["demo_check"] = mod  # dataclasses resolve annotations via sys.modules
    spec.loader.exec_module(mod)
    return mod


class _Resp:
    def __init__(self, r):
        self.status_code = r.status_code
        self._json = r.get_json(silent=True)
        self.text = r.get_data(as_text=True)

    def json(self):
        if self._json is None:
            raise ValueError("not json")
        return self._json


class FlaskSession:
    """Just enough of requests.Session for demo_check.Api."""

    def __init__(self, client):
        self.client, self.headers, self.verify = client, {}, True

    def request(self, method, url, json=None, timeout=None):
        path = url.split("://", 1)[-1].split("/", 1)[-1]
        return _Resp(self.client.open("/" + path, method=method, json=json,
                                      headers=dict(self.headers)))


@pytest.fixture()
def setup(app, db, client, world_idp, fake_screener, monkeypatch):
    from app.demo_seed import seed_demo_agents
    from app.mandates import tokens
    from app.models import Agent
    app.config["MANDATE_SIGNING_KEY"] = tokens.generate_pem()
    app.extensions["screener"] = fake_screener
    app.config["MCP_API_TOKEN"] = "demo-check-token"
    monkeypatch.setenv("MCP_API_TOKEN", "demo-check-token")
    seed_demo_agents(db, Agent, address_map="", dev_stamp=True)
    mod = _load()
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(mod, "ask", lambda prompt: "y")
    real_prompt = mod.show_approval_prompt

    def human(api, approval, instruction):
        """The test's stand-in for the person holding the phone."""
        from app.models import Approval
        real_prompt(api, approval, instruction)
        row = _db.session.get(Approval, approval["approval_id"])
        if "DENY" in instruction:
            world_idp.deny_device(row.device_code)
        else:
            world_idp.approve_device(row.device_code)
    monkeypatch.setattr(mod, "show_approval_prompt", human)
    return mod, mod.Api("http://testserver", session=FlaskSession(client))


def test_full_walkthrough_passes(setup, capsys):
    mod, api = setup
    args = type("Args", (), {"agent": None, "budget": "2.00", "skip_expire": True})()
    res = mod.run(api, args)
    outcomes = {step: result for step, result, _ in res.rows}
    assert not res.failed, res.rows
    assert outcomes["human approved, funding executed"] == "PASS"
    assert outcomes["second hire on a funded engagement is refused"] == "PASS"
    assert outcomes["release approved and executed"] == "PASS"
    assert outcomes["denied, no money moved"] == "PASS"
    assert outcomes["sub-hire without a mandate is refused"] == "PASS"
    assert outcomes["sub-hire over the mandate budget is refused"] == "PASS"
    assert outcomes["sub-hire within the mandate"] == "PASS"
    out = capsys.readouterr().out
    assert "User code" in out and "APPROVE" in out


def test_ask_human_subhire_waits_for_the_root_human(setup, fake_screener):
    """Screening asks for a human on the sub-hire hop only; the test's
    stand-in approves it on the phone."""
    mod, api = setup
    from app.models import Agent
    first = Agent.query.order_by(Agent.id).first()
    for row in Agent.query.filter(Agent.id != first.id).all():
        fake_screener.set(row.payout_address, "ASK_HUMAN")
    args = type("Args", (), {"agent": first.public_id, "budget": "2.00", "skip_expire": True})()
    res = mod.run(api, args)
    outcomes = {step: result for step, result, _ in res.rows}
    assert outcomes["sub-hire approved by the root human"] == "PASS", res.rows


def test_expiry_path_waits_for_the_provider(setup, world_idp, monkeypatch):
    """Nobody answers; the provider reports the device code expired."""
    mod, api = setup
    from app.models import Approval

    def tick(_seconds):
        for row in Approval.query.filter_by(state="pending", flow="device").all():
            world_idp.expire_device(row.device_code)
    monkeypatch.setattr(mod.time, "sleep", tick)
    args = type("Args", (), {"agent": None, "budget": "2.00", "skip_expire": False})()
    res = mod.run(api, args)
    outcomes = {step: result for step, result, _ in res.rows}
    assert outcomes["expired, no money moved"] == "PASS", res.rows


def test_screening_refusal_is_reported_not_hidden(setup, fake_screener):
    mod, api = setup
    fake_screener.default = "REFUSE"
    args = type("Args", (), {"agent": None, "budget": "2.00", "skip_expire": True})()
    res = mod.run(api, args)
    assert res.failed
    assert ("start hire approval", "FAIL") in [(s, r) for s, r, _ in res.rows]


def test_unstamped_agent_is_reported(setup, db, capsys):
    mod, api = setup
    from app.models import Agent
    for row in Agent.query.all():
        row.manifest_stamped_at = None
    db.session.commit()
    args = type("Args", (), {"agent": None, "budget": "2.00", "skip_expire": True})()
    res = mod.run(api, args)
    assert ("start hire approval", "FAIL") in [(s, r) for s, r, _ in res.rows]
    assert "--dev-stamp" in capsys.readouterr().out


def test_unreachable_server_exits_with_error(capsys):
    mod = _load()
    assert mod.main(["http://127.0.0.1:9", "--skip-expire"]) == 2
    assert "cannot reach" in capsys.readouterr().out
