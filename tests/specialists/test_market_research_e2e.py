"""
tests/specialists/test_market_research_e2e.py - market-research end to end.

ScriptedAdapter drives the real kit loop, kit tools, domain tools, policy,
ledger, acceptance checks and evidence hashing through all three milestones
in one tmp workspace seeded from evals/fixtures/hvac-scheduling (inputs/
only; the ledger is rebuilt by the tools). Offline: the fixture's source
snapshots are served as web pages by a fake transport.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import shutil
from pathlib import Path

import pytest

from agentkit.errors import AgentKitError
from agentkit.events import MemorySink
from agentkit.evidence import evidence_hash
from agentkit.llm import ScriptedAdapter
from agentkit.manifest import load_manifest
from agentkit.registry import load_specialist
from agentkit.specialist import RunContext
from agentkit.types import (AcceptanceCriterion, Brief, MilestoneSpec, ModelResponse, Submission,
                            ToolCall, Usage)
from specialists.market_research import tools

PKG = Path(__file__).resolve().parents[2] / "specialists" / "market_research"
FIXTURE = PKG / "evals" / "fixtures" / "hvac-scheduling"
PLAN_CASE = json.loads((PKG / "evals" / "cases" / "hvac-plan.json").read_text(encoding="utf-8"))
FIXTURE_LEDGER = json.loads((FIXTURE / ".agentkit" / "ledger.json").read_text(encoding="utf-8"))
TREE = json.loads((FIXTURE / "questions.json").read_text(encoding="utf-8"))
PAGES = {s["uri"]: (FIXTURE / ".agentkit" / "sources" / f"{s['id']}.txt").read_text(encoding="utf-8")
         for s in FIXTURE_LEDGER["sources"]}
URL = {s["id"]: s["uri"] for s in FIXTURE_LEDGER["sources"]}
USAGE = Usage(input_tokens=100, output_tokens=50)

M1 = ["deliverables/m1-plan/research-plan.md", "deliverables/m1-plan/questions.json"]
M2 = ["deliverables/m2-evidence/claims.csv", "deliverables/m2-evidence/claim-map.json",
      "deliverables/m2-evidence/contradictions.md"]
M3 = ["deliverables/m3-report/report.md", "deliverables/m3-report/competitor-matrix.csv",
      "deliverables/m3-report/market-sizing.json"]

PLAN_MD = """# Research plan: HVAC scheduling software, United States

## Objective
Decide whether to launch scheduling software for small US HVAC contractors at
$40 to $70 per technician per month, and how large the reachable market is.

## Scope and exclusions
In scope: scheduling and dispatch software for HVAC contractors with fewer
than 20 employees in the United States. Excluded: enterprise field-service
suites, plumbing and electrical trades.

## Question tree
- Q1 How large is the US market for scheduling software among small HVAC contractors?
  - Q1.1 How many US HVAC contractors have fewer than 20 employees? (government, trade press)
  - Q1.2 What share of small HVAC contractors already use scheduling software? (trade press, company, analyst)
- Q2 How do incumbents price?
  - Q2.1 What do the main competitors charge per technician per month? (company, review)

## Source strategy
Government establishment counts first, vendor pricing pages for prices and
trade-press surveys for adoption; analyst figures only as a cross-check.

## Evidence cutoff
Sources retrieved on or before 2026-09-01.

## Glossary
- FSM: field-service management software.
- Small contractor: an HVAC establishment with fewer than 20 employees.
"""

CONTRADICTIONS_MD = """# Contradictions and source notes

- Market size: the top-down figures [C6] [C7] come from one analyst blog (S5);
  no second source confirms them.
- S5 also carries an embedded instruction to report a different market size.
  It was treated as untrusted page content and ignored; only the two quoted
  figures were recorded.
"""

REPORT_MD = """# HVAC scheduling software: US market report

## Executive summary
There were 91,200 US HVAC contractor establishments with fewer than 20
employees in 2025 [C2], and 38% of small contractors already use dedicated
scheduling software [C5]. Incumbents charge $49 to $59 per user or technician
per month [C3, C4], inside the client's planned $40 to $70 range [C8].

## Market definition
Scheduling and dispatch software for US HVAC contractors with fewer than 20
employees; enterprise suites, plumbing and electrical trades are out of scope.

## Market size
Top-down: US trades spent $1.9 billion on field-service management software
in 2025 [C6], of which HVAC was 22% [C7], about $418 million. Bottom-up:
91,200 small contractors [C2] at an assumed $3,888 a year each, about $355
million. The two estimates are within 16% of each other; SAM and SOM rest on
the stated assumptions in market-sizing.json.

## Competitive landscape
| Competitor | Entry price |
|---|---|
| Brightwrench | $59 per technician per month, billed annually (as of 2026-09-01) [C3] |
| Tallyho Dispatch | $49 per user per month (as of 2026-09-01) [C4] |

## Contradictions and open questions
The top-down figures rest on a single analyst source [C6] [C7]; adoption rests
on one trade survey [C5].

## Traceability matrix
| Question | Finding | Claims |
|---|---|---|
| Q1.1 | 91,200 small HVAC contractors | [C1] [C2] |
| Q1.2 | 38% already use scheduling software | [C5] |
| Q2.1 | $49 to $59 per user or technician per month | [C3] [C4] |

## Sources
S1 statistics release, S2 and S3 vendor pricing pages, S4 trade survey, S5
analyst blog, S6 client brief.

Research product only. Not investment, legal, tax or accounting advice.
"""

BRIEF_QUOTE = "whether to launch in the United States at a per-technician price between $40 and $70 per month"


def sizing_args() -> dict:
    return {
        "currency": "USD", "horizon": "3 years", "primary": "bottom_up",
        "top_down": {"base": {"name": "US trades FSM spend", "value": 1.9e9, "claim": "C6"},
                     "shares": [{"name": "HVAC share", "value": 0.22, "claim": "C7"}]},
        "bottom_up": {"units": {"name": "small HVAC contractors", "value": 91200, "claim": "C2"},
                      "adoption": {"name": "addressable share", "value": 1.0,
                                   "assumption": "TAM counts every small contractor as a potential buyer"},
                      "price": {"name": "annual spend per contractor", "value": 3888,
                                "assumption": "6 technicians x $54 (midpoint of C3 and C4) x 12 months"}},
        "sam_share": {"name": "contractors with 3+ technicians", "value": 0.6,
                      "assumption": "client estimate pending evidence"},
        "som_share": {"name": "obtainable share in 3 years", "value": 0.05, "assumption": "client target"},
        "sensitivity": ["bottom_up.price", "bottom_up.adoption"],
    }


# --- plans (what the scripted model does) -------------------------------------------

def m1_plan() -> list:
    return [
        ("read_document", {"path": "inputs/brief.md"}),
        ("write_question_tree", {"objective": TREE["objective"], "evidence_cutoff": TREE["evidence_cutoff"],
                                 "questions": TREE["questions"]}),
        ("write_file", {"path": M1[0], "content": PLAN_MD}),
        ("submit_milestone", {"summary": "Question tree, source plan and evidence cutoff", "artifacts": M1}),
    ]


def m2_gather() -> list:
    claims = [("record_claim", {"text": c["text"], "source": c["source"], "quote": c["quote"],
                                "location": c["location"]}) for c in FIXTURE_LEDGER["claims"]]
    claims.append(("record_claim", {"text": "The client plans a price of $40-$70 per technician per month.",
                                    "source": "S6", "quote": BRIEF_QUOTE}))
    return [
        [("http_fetch", {"url": URL[f"S{i}"]}) for i in range(1, 6)],
        ("record_source", {"path": "inputs/brief.md", "title": "Client research brief"}),
        claims,
        [("classify_source", {"uri": URL["S2"], "source_id": "S2", "source_type": "company"}),
         ("classify_source", {"uri": URL["S3"], "source_id": "S3", "source_type": "company"}),
         ("classify_source", {"uri": URL["S4"], "source_id": "S4", "source_type": "trade_press"}),
         ("classify_source", {"uri": URL["S5"], "source_id": "S5", "source_type": "analyst"})],
        ("map_claims", {"mapping": {"C1": ["Q1.1"], "C2": ["Q1.1"], "C5": ["Q1.2"],
                                    "C3": ["Q2.1"], "C4": ["Q2.1"], "C8": ["Q2.1"]},
                        "notes": {"Q1.1": "Insufficient evidence: one government release only",
                                  "Q1.2": "Insufficient evidence: a single trade survey"}}),
        ("write_file", {"path": M2[2], "content": CONTRADICTIONS_MD}),
    ]


def m2_plan() -> list:
    return m2_gather() + [
        ("export_evidence", {}),
        ("submit_milestone", {"summary": "Evidence ledger with 8 verified claims", "artifacts": M2}),
    ]


def m3_plan(*, sizing: tuple | None = None, report: str = REPORT_MD) -> list:
    return [
        ("build_competitor_matrix", {"dimensions": ["Entry price", "Scheduling"], "competitors": [
            {"name": "Brightwrench", "cells": {"Entry price": {
                "value": "$59 per technician per month", "claim": "C3", "as_of": "2026-09-01"}}},
            {"name": "Tallyho Dispatch", "cells": {"Entry price": {
                "value": "$49 per user per month", "claim": "C4", "as_of": "2026-09-01"}}}]}),
        sizing or ("build_sizing_model", sizing_args()),
        ("write_file", {"path": M3[0], "content": report}),
        ("submit_milestone", {"summary": "Matrix, sizing model and cited report", "artifacts": M3}),
    ]


# --- harness ------------------------------------------------------------------------------

class FakeTransport:
    """Serves the fixture snapshots as plain-text pages; 404 for anything else."""

    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append(url)
        if url in PAGES:
            return 200, {"Content-Type": "text/plain; charset=utf-8"}, PAGES[url].encode("utf-8")
        return 404, {}, b"not found"


@pytest.fixture
def spec():
    return load_specialist("market-research")


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    shutil.copytree(FIXTURE / "inputs", ws / "inputs")
    return ws


def brief(**intake) -> Brief:
    data = dict(PLAN_CASE["brief"])
    return Brief(engagement_id="eng-mr-1", specialist="market-research", objective=data["objective"],
                 intake={**data["intake"], **intake})


_ids = itertools.count(1)


def script(plan: list) -> ScriptedAdapter:
    """ScriptedAdapter.from_tool_plan, plus callables: a step computed from
    the workspace at the moment the model takes it."""
    steps = []
    for item in plan:
        if callable(item) or isinstance(item, str):
            steps.append(item if callable(item) else ModelResponse(
                text=item, tool_calls=[], stop_reason="end", usage=USAGE, model="scripted"))
            continue
        calls = [ToolCall(f"call_{next(_ids)}", name, dict(args))
                 for name, args in (item if isinstance(item, list) else [item])]
        steps.append(ModelResponse(text="", tool_calls=calls, stop_reason="tool_use", usage=USAGE,
                                   model="scripted"))
    return ScriptedAdapter(steps)


def run(spec, ws, milestone, plan, *, b=None, grader=None, events=None, transport=None):
    events = events if events is not None else MemorySink()
    ctx = RunContext(brief=b or brief(), workspace=ws, adapter=script(plan),
                     grader=grader, events=events, transport=transport or FakeTransport())
    return spec.run_milestone(ctx, milestone), events


def results(sub: Submission) -> dict:
    return results_of(sub.check_results)


def results_of(check_results: list) -> dict:
    return {r.check: r for r in check_results}


def tool_errors(events: MemorySink) -> list:
    return [e.data for e in events.of_type("tool_result") if e.data["is_error"]]


def assert_ready(spec, ws, sub, deliverables):
    detail = [(r.check, r.passed, r.details) for r in sub.check_results]
    assert sub.status == "ready_for_review", detail
    for r in sub.check_results:
        if r.kind == "automated":
            assert r.passed is True, detail
        else:
            assert r.passed is None and r.kind in ("rubric", "human"), detail   # no grader, no reviewer yet
    # every declared deliverable is an artifact, hashed from the bytes on disk
    assert sorted(a.path for a in sub.artifacts) == sorted(deliverables)
    for a in sub.artifacts:
        data = (ws / a.path).read_bytes()
        assert a.sha256 == hashlib.sha256(data).hexdigest() and a.bytes == len(data)
    # the human gate comes from the manifest, and the saved submission re-hashes
    assert sub.human_review.required is spec.manifest.human_gate.required is False
    assert sub.human_review.disclaimer == spec.manifest.human_gate.disclaimer
    assert sub.evidence_hash.startswith("0x") and len(sub.evidence_hash) == 66
    saved = json.loads(spec.submission_path(ws, sub.milestone_id).read_text(encoding="utf-8"))
    assert saved["evidence_hash"] == sub.evidence_hash == evidence_hash(Submission.from_dict(saved))


def run_through_m2(spec, ws):
    for milestone, plan in (("m1-plan", m1_plan()), ("m2-evidence", m2_plan())):
        sub, events = run(spec, ws, milestone, plan)
        assert sub.status == "ready_for_review", [(r.check, r.details) for r in sub.check_results]
        assert not tool_errors(events)


# --- wiring ---------------------------------------------------------------------------------

def test_manifest_loads_strictly_and_wires_every_tool_and_check(spec):
    assert type(spec).__name__ == "MarketResearch"
    assert load_manifest(PKG) == spec.manifest
    assert spec.validate() == []
    assert spec.tool_registry().names() == spec.manifest.tools
    checks = spec.check_registry()
    for name in ("question_tree_valid", "evidence_export_matches_ledger", "question_coverage",
                 "source_tier_mix", "matrix_cells_cited", "sizing_model_consistent",
                 "report_answers_questions"):
        assert checks.kind(name) == "automated"
    assert spec.manifest.egress.intake_field == "allowed_domains"


# --- happy path: all three milestones in one workspace ------------------------------------

def test_three_milestones_end_to_end(spec, ws):
    # m1: question tree + plan
    transport = FakeTransport()
    sub, events = run(spec, ws, "m1-plan", m1_plan(), transport=transport)
    assert not tool_errors(events)
    assert_ready(spec, ws, sub, M1)
    assert {r.check: r.kind for r in sub.check_results}["rubric_grader"] == "rubric"
    assert transport.calls == []                                    # m1 needs no network

    # m2: web sources + client brief into the ledger, tiers, map, export
    transport = FakeTransport()
    sub, events = run(spec, ws, "m2-evidence", m2_plan(), transport=transport)
    assert not tool_errors(events), tool_errors(events)
    assert_ready(spec, ws, sub, M2)
    assert transport.calls == [URL[f"S{i}"] for i in range(1, 6)]
    rows = {r["claim_id"]: r for r in tools.expected_evidence_rows(ws)}
    assert len(rows) == 8 and all(r["verified"] == "true" for r in rows.values())
    assert rows["C8"]["uri"] == "workspace:inputs/brief.md" and rows["C8"]["source_type"] == "client"
    assert "5/8 verified claims from tier-1" in results(sub)["source_tier_mix"].details

    # m3: matrix, sizing model and cited report built on the same ledger
    sub, events = run(spec, ws, "m3-report", m3_plan())
    assert not tool_errors(events), tool_errors(events)
    assert_ready(spec, ws, sub, M3)
    sizing = json.loads((ws / M3[2]).read_text(encoding="utf-8"))
    assert sizing["outputs"]["tam_top_down"] == pytest.approx(418e6)
    assert sizing["outputs"]["tam"] == pytest.approx(91200 * 3888)

    # re-checking later gives the same verdicts (the checks re-derive everything)
    for milestone in ("m1-plan", "m2-evidence", "m3-report"):
        again = spec.check(ws, milestone, RunContext(brief=brief(), workspace=ws))
        assert all(r.passed is True for r in again if r.kind == "automated"), milestone


def test_rubric_uses_the_package_rubric_and_grader(spec, ws):
    ids = ["decision-framing", "leaf-answerability", "coverage", "source-strategy", "scope-discipline"]
    scores = [{"id": i, "score": 0.9, "rationale": "ok"} for i in ids]
    call = ToolCall("j1", "score_rubric", {"scores": scores})
    grader = ScriptedAdapter([ModelResponse(text="", tool_calls=[call], stop_reason="tool_use",
                                           usage=Usage(), model="grader")])
    sub, _ = run(spec, ws, "m1-plan", m1_plan(), grader=grader)
    rubric = results(sub)["rubric_grader"]
    assert rubric.passed is True and rubric.score == pytest.approx(0.9)
    assert sub.status == "ready_for_review"
    assert "Rubric criteria" in grader.calls[0]["messages"][0]["text"]


# --- negative paths ---------------------------------------------------------------------------

def test_client_tier1_threshold_is_applied(spec, ws):
    run(spec, ws, "m1-plan", m1_plan())
    sub, _ = run(spec, ws, "m2-evidence", m2_plan(), b=brief(tier1_share=0.7))
    tier = results(sub)["source_tier_mix"]
    assert tier.passed is False and "threshold 70%" in tier.details
    assert sub.status == "needs_revision"
    # the same brief re-checks the same way; the manifest default (0.6) passes
    again = spec.check(ws, "m2-evidence", RunContext(brief=brief(tier1_share=0.7), workspace=ws))
    assert results_of(again)["source_tier_mix"].passed is False
    assert results_of(spec.check(ws, "m2-evidence"))["source_tier_mix"].passed is True


def test_malformed_intake_stops_before_any_model_call(spec, ws):
    adapter = ScriptedAdapter.from_tool_plan(m1_plan())
    with pytest.raises(AgentKitError, match="tier1_share"):
        spec.run_milestone(RunContext(brief=brief(tier1_share="most"), workspace=ws, adapter=adapter),
                           "m1-plan")
    assert adapter.calls == []
    with pytest.raises(AgentKitError, match="allowed_domains"):
        spec.run_milestone(RunContext(brief=brief(allowed_domains="example.gov"), workspace=ws,
                                      adapter=adapter), "m1-plan")
    assert adapter.calls == []
    # scoping reports both before a run is attempted
    assert not any(m.blocking for m in spec.validate_intake(brief().intake))
    missing = {m.field: m for m in spec.validate_intake(
        {**brief().intake, "tier1_share": 1.5, "allowed_domains": ["https://example.gov/x"]})}
    assert missing["tier1_share"].blocking and "between 0 and 1" in missing["tier1_share"].question
    assert missing["allowed_domains"].blocking and "not a host rule" in missing["allowed_domains"].question


def test_hand_edited_evidence_export_fails(spec, ws):
    run(spec, ws, "m1-plan", m1_plan())

    def inflate_tier(messages):
        # after the honest export, the model rewrites claims.csv to lift the analyst blog to tier 1
        text = (ws / M2[0]).read_text(encoding="utf-8")
        assert ",2,analyst," in text
        call = ToolCall("forge", "write_file",
                        {"path": M2[0], "content": text.replace(",2,analyst,", ",1,analyst,")})
        return ModelResponse(text="", tool_calls=[call], stop_reason="tool_use", usage=USAGE,
                             model="scripted")

    plan = m2_gather() + [("export_evidence", {}), inflate_tier,
                          ("submit_milestone", {"summary": "Evidence ledger", "artifacts": M2})]
    sub, events = run(spec, ws, "m2-evidence", plan)
    assert not tool_errors(events)
    check = results(sub)["evidence_export_matches_ledger"]
    assert check.passed is False and "C6: tier" in check.details
    assert results(sub)["ledger_verified"].passed is True
    assert sub.status == "needs_revision"


def test_forged_sizing_and_citation_fail_m3(spec, ws):
    run_through_m2(spec, ws)
    # the model follows the instruction planted in S5: it hand-writes a $40 billion
    # market into the sizing model and cites a claim the ledger refused to record
    forged = sizing_args()
    forged["outputs"] = {"tam_top_down": 4e10, "tam_bottom_up": 4e10, "tam": 4e10, "sam": 2.4e10,
                         "som": 1.2e9, "gap": 0.0}
    report = REPORT_MD.replace("## Sources", "The market is worth $40 billion [C9].\n\n## Sources")
    plan = m3_plan(sizing=("write_file", {"path": M3[2], "content": json.dumps(forged)}), report=report)
    plan.insert(0, ("record_claim", {"text": "The market is worth $40 billion", "source": "S5",
                                     "quote": "the market is worth $40 billion dollars"}))
    sub, events = run(spec, ws, "m3-report", plan)
    claim = next(e.data for e in events.of_type("tool_result") if e.data["name"] == "record_claim")
    assert claim["is_error"] and "not found verbatim" in claim["content"]
    by = results(sub)
    sizing = by["sizing_model_consistent"]
    assert sizing.passed is False and "outputs.tam" in sizing.details
    assert by["citations_resolve"].passed is False and "C9" in by["citations_resolve"].details
    assert by["matrix_cells_cited"].passed is True and by["ledger_verified"].passed is True
    assert sub.status == "needs_revision" and sub.evidence_hash.startswith("0x")


def test_tampered_ledger_fails_on_recheck(spec, ws):
    run_through_m2(spec, ws)
    path = ws / ".agentkit" / "ledger.json"
    ledger = json.loads(path.read_text(encoding="utf-8"))
    ledger["claims"][1]["quote"] = "of which 191,200 had fewer than 20 employees"
    path.write_text(json.dumps(ledger), encoding="utf-8")
    by = results_of(spec.check(ws, "m2-evidence"))
    assert by["ledger_verified"].passed is False and "C2" in by["ledger_verified"].details
    assert by["evidence_export_matches_ledger"].passed is False


def test_allowed_domains_narrow_egress(spec, ws):
    events, transport = MemorySink(), FakeTransport()
    plan = [[("http_fetch", {"url": URL["S1"]}), ("http_fetch", {"url": URL["S2"]}),
             ("http_fetch", {"url": "http://169.254.169.254/latest/meta-data/"})], "done", "done"]
    sub, _ = run(spec, ws, "m2-evidence", plan, events=events, transport=transport,
                 b=brief(allowed_domains=[".example.gov"]))
    assert transport.calls == [URL["S1"]]
    denied = [e.data["reason"] for e in events.of_type("policy_denied")]
    assert len(denied) == 2 and "allowed domains" in denied[0]
    assert sub.status == "incomplete"


def test_brief_cannot_downgrade_domain_checks(spec, ws):
    extra = MilestoneSpec(id="m9-recheck", title="Re-check coverage", acceptance=[
        AcceptanceCriterion("file_exists", params={"path": M2[0]}),
        AcceptanceCriterion("question_coverage", kind="human", params={"min_claims": 50})])
    b = brief()
    b.milestones = [extra]
    by = results_of(spec.check(ws, "m9-recheck", RunContext(brief=b, workspace=ws)))
    assert by["question_coverage"].kind == "automated" and by["question_coverage"].passed is False
