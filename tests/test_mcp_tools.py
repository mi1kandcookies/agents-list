"""MCP tool logic against a fake HTTP layer. Must not import the ``mcp`` library."""
import pathlib
import subprocess
import sys

import pytest
import requests

from agentslist_mcp import agent_ids, tools
from agentslist_mcp.client import AgentListClient

ROOT = pathlib.Path(__file__).resolve().parents[1]
AGENT = agent_ids.encode(123456789)
OTHER_AGENT = agent_ids.encode(987654321)
ENG = "ENG-7Q2K"
APR = "APR-91XZ"


class FakeResponse:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = "" if body is None else str(body)

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeSession:
    """Records calls; routes ``(METHOD, path)`` to a queue of (status, body)."""

    def __init__(self, routes=None):
        self.routes = {k: list(v) for k, v in (routes or {}).items()}
        self.calls = []

    def request(self, method, url, *, params=None, json=None, headers=None, timeout=None):
        path = url.split("://", 1)[1].split("/", 1)[1]
        self.calls.append({"method": method, "path": "/" + path, "params": params, "json": json,
                           "headers": headers or {}})
        queue = self.routes.get((method, "/" + path))
        if not queue:
            raise AssertionError(f"unexpected call {method} /{path}")
        status, body = queue.pop(0) if len(queue) > 1 else queue[0]
        return FakeResponse(status, body)


def make_client(routes=None, token="t0k"):
    session = FakeSession(routes)
    return AgentListClient("http://api.test/", token, session=session), session


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, s):
        self.sleeps.append(s)
        self.now += s


def approval(state="pending", **over):
    body = {
        "approval_id": APR, "kind": "engagement.fund", "state": state, "flow": "device",
        "user_code": "WDJB-MJHT", "verification_uri": "https://id.example/device",
        "verification_uri_complete": "https://id.example/device?code=WDJB-MJHT",
        "expires_at": 1790000180, "action_hash": "0x" + "ab" * 32,
        "summary": [["Pay", "25 USDC"], ["To", AGENT]],
        "screening": {"id": "SCR-1", "verdict": "PAY", "cap_micro": None, "reasons": [], "fail_closed": False},
        "failure_code": None, "result": None,
    }
    body.update(over)
    return body


def engagement(status="awaiting_approval", approvals=(), ledger=(), agent=AGENT):
    return {"engagement_id": ENG, "agent_id": agent, "status": status, "total_micro": 25_000_000,
            "milestones": [{"idx": 0, "status": "pending", "amount_micro": 25_000_000}],
            "approvals": list(approvals), "ledger": list(ledger), "chain_url": f"/engagements/{ENG}/chain"}


def test_tools_module_does_not_import_mcp_library():
    code = ("import sys, agentslist_mcp.tools, agentslist_mcp.client; "
            "assert not [m for m in sys.modules if m == 'mcp' or m.startswith('mcp.')]")
    subprocess.run([sys.executable, "-c", code], check=True, cwd=ROOT)


# --- agent id validation --------------------------------------------------

def _swap(agent_id, i):
    body = list(agent_id.replace("-", "")[3:])
    body[i], body[i + 1] = body[i + 1], body[i]
    s = "".join(body)
    return f"AGT-{s[:4]}-{s[4:8]}-{s[8]}"


def _typo_with_suggestion():
    for i in range(7):
        typo = _swap(AGENT, i)
        if typo == AGENT:
            continue
        try:
            agent_ids.decode(typo)
        except agent_ids.AgentIdError as exc:
            if exc.suggestion == AGENT:
                return typo
    raise AssertionError("no unambiguous transposition found for fixture id")


@pytest.mark.parametrize("call", [
    lambda c, bad: tools.request_scope(c, bad, "Build a landing page", 25),
    lambda c, bad: tools.hire(c, ENG, bad, 25),
    lambda c, bad: tools.subhire(c, ENG, bad, 5, "Development", "jwt.jwt.jwt"),
])
def test_typod_agent_id_rejected_before_any_http_call(call):
    client, session = make_client()
    typo = _typo_with_suggestion()
    out = call(client, typo)
    assert session.calls == []
    assert out["ok"] is False and out["code"] == "INVALID_AGENT_ID"
    assert out["suggestion"] == AGENT and AGENT in out["error"]
    assert out["field"] == "agent_id"


def test_garbage_agent_id_rejected_without_suggestion():
    client, session = make_client()
    out = tools.request_scope(client, "AGT-NOPE", "x", 1)
    assert session.calls == [] and out["code"] == "INVALID_AGENT_ID" and out["suggestion"] is None


# --- search / scope -------------------------------------------------------

def test_search_sends_query_and_filters_by_budget():
    agents = [{"agent_id": AGENT, "name": "Cheap", "price_hint_usdc": 10},
              {"agent_id": OTHER_AGENT, "name": "Pricey", "price_hint_usdc": 500},
              {"agent_id": agent_ids.encode(5), "name": "Unpriced", "price_hint_usdc": None}]
    client, session = make_client({("GET", "/api/agents"): [(200, agents)]})
    out = tools.search_agents(client, "landing page", category="Development", max_budget_usdc=50, limit=500)
    assert [a["name"] for a in out["agents"]] == ["Cheap", "Unpriced"]
    call = session.calls[0]
    assert call["params"] == {"q": "landing page", "category": "Development", "limit": 50}
    assert call["headers"]["Authorization"] == "Bearer t0k"


def test_request_scope_normalizes_id_and_returns_sow():
    created = {**engagement(status="scoped"), "sow": {"outcome": "Landing page"}, "sow_hash": "0x" + "cd" * 32}
    client, session = make_client({("POST", "/api/engagements"): [(201, created)]})
    out = tools.request_scope(client, AGENT.lower().replace("-", ""), "Landing page", 25,
                              milestones=[{"title": "Draft", "acceptance": "Deployed", "amount_usdc": 25}])
    assert out["ok"] and out["engagement_id"] == ENG and out["sow_hash"] == created["sow_hash"]
    assert out["money_moved"] is False
    sent = session.calls[0]["json"]
    assert sent["agent_id"] == AGENT and sent["budget_usdc"] == 25
    assert sent["milestones"] == [{"title": "Draft", "acceptance": "Deployed", "amount_usdc": 25}]


def test_request_scope_rejects_bad_amounts_locally():
    client, session = make_client()
    for bad in (0, -1, "abc", True, 1.0000001):
        assert tools.request_scope(client, AGENT, "x", bad)["code"] == "INVALID_AMOUNT"
    assert session.calls == []


# --- hire -----------------------------------------------------------------

def test_hire_returns_code_link_and_verdict_without_claiming_payment():
    client, session = make_client({
        ("GET", f"/api/engagements/{ENG}"): [(200, engagement(status="scoped"))],
        ("POST", f"/api/engagements/{ENG}/hire"): [(202, approval())],
    })
    out = tools.hire(client, ENG, AGENT, 25)
    assert out["ok"] is True
    assert out["user_code"] == "WDJB-MJHT"
    assert out["verification_uri_complete"] == "https://id.example/device?code=WDJB-MJHT"
    assert out["expires_at"] == 1790000180 and out["action_hash"] == "0x" + "ab" * 32
    assert out["screening_verdict"] == "PAY"
    assert out["money_moved"] is False
    assert "WDJB-MJHT" in out["instruction"] and "No money has moved" in out["instruction"]
    assert session.calls[-1]["json"] == {"flow": "device", "confirm_amount_usdc": 25}


def test_hire_rejects_agent_that_does_not_match_engagement():
    client, session = make_client({("GET", f"/api/engagements/{ENG}"): [(200, engagement(agent=OTHER_AGENT))]})
    out = tools.hire(client, ENG, AGENT, 25)
    assert out["code"] == "AGENT_MISMATCH"
    assert [c["method"] for c in session.calls] == ["GET"]


def test_hire_blocked_by_screening_says_no_money_moved():
    blocked = approval(state="blocked", user_code=None, verification_uri_complete=None,
                       failure_code="SCREENING_REFUSED",
                       screening={"id": "SCR-2", "verdict": "REFUSE", "reasons": [{"code": "TOXIC_SCORE_HIGH"}]})
    client, _ = make_client({
        ("GET", f"/api/engagements/{ENG}"): [(200, engagement())],
        ("POST", f"/api/engagements/{ENG}/hire"): [(202, blocked)],
    })
    out = tools.hire(client, ENG, AGENT, 25)
    assert out["state"] == "blocked" and out["screening_verdict"] == "REFUSE"
    assert "No money moved" in out["instruction"] and "do not retry" in out["instruction"]


@pytest.mark.parametrize("status,code,field", [
    (409, "APPROVAL_CONSUMED", None),
    (403, "SCREENING_REFUSED", None),
    (403, "CAP_EXCEEDED", "confirm_amount_usdc"),
    (404, "AGENT_NOT_FOUND", "agent_id"),
])
def test_api_error_codes_surface_verbatim(status, code, field):
    body = {"error": f"server says {code.lower()}", "code": code, "field": field}
    client, _ = make_client({
        ("GET", f"/api/engagements/{ENG}"): [(200, engagement())],
        ("POST", f"/api/engagements/{ENG}/hire"): [(status, body)],
    })
    out = tools.hire(client, ENG, AGENT, 25)
    assert out == {"ok": False, "code": code, "error": body["error"], "field": field, "http_status": status}


def test_non_json_error_and_unreachable_api():
    client, _ = make_client({("GET", "/api/agents"): [(502, None)]})
    assert tools.search_agents(client, "x")["code"] == "HTTP_502"

    class Down:
        def request(self, *a, **k):
            raise requests.ConnectionError("refused")

    out = tools.search_agents(AgentListClient("http://api.test", "", session=Down()), "x")
    assert out["code"] == "API_UNREACHABLE" and out["http_status"] is None


# --- status polling -------------------------------------------------------

def test_status_polls_until_terminal_and_reports_ledger():
    pending = approval(state="pending")
    done = approval(state="consumed", result={"ledger_ids": ["LED-1"], "tx": None})
    ledger = [{"id": "LED-1", "kind": "fund", "amount_micro": 25_000_000, "status": "simulated", "tx_hash": None}]
    client, session = make_client({
        ("GET", f"/api/engagements/{ENG}"): [
            (200, engagement(approvals=[pending])),
            (200, engagement(approvals=[pending])),
            (200, engagement(approvals=[pending])),
            (200, engagement(status="funded", approvals=[done], ledger=ledger)),
        ],
        ("GET", f"/api/approvals/{APR}"): [(200, pending), (200, pending), (200, done)],
    })
    clock = FakeClock()
    out = tools.get_engagement_status(client, ENG, wait_seconds=25, sleep=clock.sleep, clock=clock)
    assert out["terminal"] is True and out["timed_out"] is False
    assert out["status"] == "funded" and out["approval"]["state"] == "consumed"
    assert out["money_moved"] is True and "simulated" in out["message"]
    assert len(clock.sleeps) == 2
    assert sum(1 for c in session.calls if c["path"] == f"/api/approvals/{APR}") == 3


def test_status_times_out_without_claiming_payment_and_caps_wait():
    client, _ = make_client({
        ("GET", f"/api/engagements/{ENG}"): [(200, engagement(approvals=[approval()]))],
        ("GET", f"/api/approvals/{APR}"): [(200, approval())],
    })
    clock = FakeClock()
    out = tools.get_engagement_status(client, ENG, wait_seconds=600, sleep=clock.sleep, clock=clock)
    assert out["terminal"] is False and out["timed_out"] is True
    assert out["money_moved"] is False and "No money has moved" in out["message"]
    assert clock.now <= tools.MAX_WAIT_SECONDS


def test_status_approved_but_tx_pending_is_not_money_moved():
    done = approval(state="consumed")
    ledger = [{"id": "LED-1", "kind": "fund", "amount_micro": 25_000_000, "status": "pending", "tx_hash": "0x1"}]
    client, _ = make_client({("GET", f"/api/engagements/{ENG}"): [
        (200, engagement(status="awaiting_approval", approvals=[done], ledger=ledger))]})
    out = tools.get_engagement_status(client, ENG)
    assert out["terminal"] is True and out["money_moved"] is False
    assert "not yet confirmed" in out["message"]


def test_status_without_approvals_returns_immediately():
    client, session = make_client({("GET", f"/api/engagements/{ENG}"): [(200, engagement(status="scoped"))]})
    clock = FakeClock()
    out = tools.get_engagement_status(client, ENG, wait_seconds=25, sleep=clock.sleep, clock=clock)
    assert out["approval"] is None and out["status"] == "scoped" and clock.sleeps == []
    assert len(session.calls) == 1


# --- release --------------------------------------------------------------

def test_release_flow_start_then_status_confirms_release():
    rel_pending = approval(kind="milestone.release", state="pending")
    rel_done = approval(kind="milestone.release", state="consumed")
    ledger = [{"id": "LED-1", "kind": "fund", "amount_micro": 25_000_000, "status": "confirmed"},
              {"id": "LED-2", "kind": "release", "amount_micro": 25_000_000, "status": "confirmed",
               "tx_hash": "0xabc"}]
    client, session = make_client({
        ("POST", f"/api/engagements/{ENG}/milestones/0/release"): [(202, rel_pending)],
        ("GET", f"/api/engagements/{ENG}"): [
            (200, engagement(status="in_progress", approvals=[rel_pending])),
            (200, engagement(status="completed", approvals=[rel_done], ledger=ledger)),
        ],
        ("GET", f"/api/approvals/{APR}"): [(200, rel_done)],
    })
    started = tools.release_milestone(client, ENG, 0)
    assert started["ok"] and started["kind"] == "milestone.release" and started["milestone_index"] == 0
    assert started["user_code"] and started["verification_uri_complete"] and started["money_moved"] is False
    assert session.calls[0]["json"] == {"flow": "device"}

    clock = FakeClock()
    out = tools.get_engagement_status(client, ENG, wait_seconds=10, sleep=clock.sleep, clock=clock)
    assert out["terminal"] and out["engagement_terminal"] and out["status"] == "completed"
    assert out["money_moved"] and "release 25000000 micro-USDC (confirmed on-chain)" in out["message"]


def test_release_rejects_bad_index_locally():
    client, session = make_client()
    for bad in (-1, "0", True, 1.5):
        assert tools.release_milestone(client, ENG, bad)["code"] == "INVALID_REQUEST"
    assert session.calls == []


# --- subhire --------------------------------------------------------------

def test_subhire_uses_mandate_auth_and_handles_both_responses():
    child = {"engagement_id": "ENG-CHILD", "agent_id": OTHER_AGENT, "status": "funded", "sow_hash": "0x1"}
    client, session = make_client({("POST", f"/api/engagements/{ENG}/subhire"): [
        (201, child), (202, approval(kind="subhire.fund"))]})
    out = tools.subhire(client, ENG, OTHER_AGENT, 5, "Development", "hdr.payload.sig", outcome="Write tests")
    assert out["ok"] and out["engagement_id"] == "ENG-CHILD" and out["money_moved"] is False
    assert session.calls[0]["headers"]["Authorization"] == "Mandate hdr.payload.sig"
    assert session.calls[0]["json"]["agent_id"] == OTHER_AGENT

    out = tools.subhire(client, ENG, OTHER_AGENT, 5, "Development", "hdr.payload.sig")
    assert out["kind"] == "subhire.fund" and out["user_code"] == "WDJB-MJHT"


def test_client_reads_env_config(monkeypatch):
    monkeypatch.setenv("MCP_API_BASE", "http://example.test:9000/")
    monkeypatch.setenv("MCP_API_TOKEN", "abc")
    c = AgentListClient(session=FakeSession())
    assert c.base_url == "http://example.test:9000" and c.token == "abc"
    monkeypatch.delenv("MCP_API_BASE")
    assert AgentListClient(session=FakeSession()).base_url == "http://127.0.0.1:8090"
