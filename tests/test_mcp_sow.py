"""MCP submit_sow against a fake HTTP layer. Must not import the ``mcp`` library."""
import pytest

from agentslist_mcp import agent_ids, tools
from agentslist_mcp.client import AgentListClient

AGENT = agent_ids.encode(123456789)
SHA = "ab" * 32
SCOPE = {
    "method": "heuristic", "title": "Payroll competitor map",
    "outcome": "A ranked map of the 20 closest competitors to our payroll product.",
    "brief": "A ranked map of the 20 closest competitors to our payroll product.",
    "category_key": "research", "category_label": "Research", "agent_category": "Research",
    "milestones": [
        {"title": "Scope and sources", "acceptance": ["Sources agreed"], "amount_usdc": 600},
        {"title": "Findings draft", "acceptance": ["Every claim sourced", "Pricing for all 20"], "amount_usdc": 1000},
        {"title": "Final report", "acceptance": ["One-page summary"], "amount_usdc": 900},
    ],
    "acceptance": [], "deadline": {"date": "2026-12-15", "mode": "date", "text": "due 15 December 2026"},
    "budget_usdc": 2500, "warnings": [], "notes": [],
    "source": {"filename": "sow.md", "sha256": SHA, "pages": None, "bytes": 780, "chars": 774,
               "truncated": False},
    "text": "# Statement of Work ...",
}


class FakeResponse:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def request(self, method, url, *, params=None, json=None, headers=None, timeout=None, files=None):
        path = "/" + url.split("://", 1)[1].split("/", 1)[1]
        self.calls.append({"method": method, "path": path, "json": json, "files": files,
                           "headers": headers or {}})
        if (method, path) not in self.routes:
            raise AssertionError(f"unexpected call {method} {path}")
        status, body = self.routes[(method, path)]
        return FakeResponse(status, body(json) if callable(body) else body)


def _engagement(payload):
    ms = payload.get("milestones") or [{"title": "Deliver", "acceptance": "-", "amount_usdc": payload["budget_usdc"]}]
    return {"engagement_id": "ENG-7Q2K", "agent_id": payload["agent_id"], "status": "scoped",
            "sow": {"outcome": payload["outcome"], "source_document": payload.get("source_document"),
                    "milestones": ms},
            "sow_hash": "0x" + "1" * 64, "milestones": ms, "screening": {"verdict": "PAY"}}


def make(scope=SCOPE, created=_engagement):
    session = FakeSession({("POST", "/api/sow/parse"): (200, scope),
                           ("POST", "/api/engagements"): (201, created)})
    return AgentListClient("http://api.test", "t0k", session=session), session


@pytest.fixture()
def sow_file(tmp_path):
    path = tmp_path / "sow.md"
    path.write_text("# Statement of Work\n\n## Objective\nA ranked competitor map.\n")
    return path


def test_parse_only_uploads_the_file_and_suggests_a_search(sow_file):
    client, session = make()
    out = tools.submit_sow(client, path=str(sow_file))
    assert out["ok"] and out["engagement_created"] is False and out["money_moved"] is False
    call = session.calls[0]
    assert call["path"] == "/api/sow/parse" and call["headers"]["Authorization"] == "Bearer t0k"
    name, data = call["files"]["file"]
    assert name == "sow.md" and data == sow_file.read_bytes()
    assert out["source_document"] == {"filename": "sow.md", "sha256": SHA}
    assert out["scope"]["budget_usdc"] == 2500 and "text" not in out["scope"]
    assert out["search_hint"] == {"query": SCOPE["brief"], "category": "Research"}
    assert "search_agents(" in out["next_step"] and "submit_sow(" in out["next_step"]
    assert len(session.calls) == 1


def test_text_input_is_sent_as_json():
    client, session = make()
    out = tools.submit_sow(client, text="Objective: a ranked competitor map.")
    assert out["ok"] and session.calls[0]["json"] == {"text": "Objective: a ranked competitor map."}


def test_with_agent_creates_the_engagement_with_source_document(sow_file):
    client, session = make()
    out = tools.submit_sow(client, path=str(sow_file), agent_id=AGENT.lower())
    assert out["engagement_created"] and out["engagement_id"] == "ENG-7Q2K"
    assert out["sow_hash"].startswith("0x") and out["money_moved"] is False
    payload = session.calls[1]["json"]
    assert payload["agent_id"] == AGENT and payload["budget_usdc"] == 2500
    assert payload["source_document"] == {"filename": "sow.md", "sha256": SHA}
    assert payload["deadline"] == "2026-12-15"
    assert [(m["title"], m["amount_usdc"]) for m in payload["milestones"]] == [
        ("Scope and sources", "600"), ("Findings draft", "1000"), ("Final report", "900")]
    assert payload["milestones"][1]["acceptance"] == "- Every claim sourced\n- Pricing for all 20"
    assert f"hire(engagement_id='ENG-7Q2K', agent_id='{AGENT}', confirm_amount_usdc=2500)" in out["next_step"]
    assert "World ID" in out["next_step"]


def test_even_split_when_the_document_has_no_milestone_amounts(sow_file):
    scope = {**SCOPE, "budget_usdc": 1000,
             "milestones": [{**m, "amount_usdc": None} for m in SCOPE["milestones"]]}
    client, session = make(scope)
    out = tools.submit_sow(client, path=str(sow_file), agent_id=AGENT)
    amounts = [m["amount_usdc"] for m in session.calls[1]["json"]["milestones"]]
    assert amounts == ["333.333333", "333.333333", "333.333334"]
    assert any("split evenly" in w for w in out["warnings"])


def test_mismatched_amounts_fall_back_to_one_milestone(sow_file):
    client, session = make()
    out = tools.submit_sow(client, path=str(sow_file), agent_id=AGENT, budget_usdc=3000)
    payload = session.calls[1]["json"]
    assert payload["budget_usdc"] == 3000 and "milestones" not in payload
    assert any("add up to 2500" in w for w in out["warnings"])


def test_no_budget_asks_the_human_first(sow_file):
    client, session = make({**SCOPE, "budget_usdc": None})
    out = tools.submit_sow(client, path=str(sow_file), agent_id=AGENT)
    assert out["ok"] and out["engagement_created"] is False
    assert "budget" in out["next_step"] and len(session.calls) == 1


def test_bad_agent_id_is_rejected_before_any_request(sow_file):
    client, session = make()
    body = AGENT[:-1]
    bad = body + next(c for c in agent_ids.CHECK_ALPHABET if c != AGENT[-1])
    out = tools.submit_sow(client, path=str(sow_file), agent_id=bad)
    assert out["ok"] is False and out["code"] == "INVALID_AGENT_ID"
    assert session.calls == []


@pytest.mark.parametrize("name, size, code", [
    ("sow.exe", 10, "UNSUPPORTED_FILE_TYPE"),
    ("sow.txt", 0, "EMPTY_DOCUMENT"),
    ("sow.txt", tools.SOW_MAX_BYTES + 1, "FILE_TOO_LARGE"),
])
def test_local_file_checks(tmp_path, name, size, code):
    path = tmp_path / name
    path.write_bytes(b"a" * size)
    client, session = make()
    out = tools.submit_sow(client, path=str(path))
    assert out["code"] == code and session.calls == []


def test_path_or_text_but_not_both(tmp_path):
    client, session = make()
    assert tools.submit_sow(client)["code"] == "INVALID_REQUEST"
    assert tools.submit_sow(client, path="a.md", text="b")["code"] == "INVALID_REQUEST"
    assert tools.submit_sow(client, path=str(tmp_path / "missing.md"))["code"] == "FILE_NOT_FOUND"
    assert session.calls == []


def test_api_errors_pass_through(sow_file):
    session = FakeSession({("POST", "/api/sow/parse"): (415, {"error": "Upload a .pdf…",
                                                              "code": "UNSUPPORTED_FILE_TYPE",
                                                              "field": "file"})})
    out = tools.submit_sow(AgentListClient("http://api.test", "", session=session), path=str(sow_file))
    assert out == {"ok": False, "code": "UNSUPPORTED_FILE_TYPE", "error": "Upload a .pdf…",
                   "field": "file", "http_status": 415}
