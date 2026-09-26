"""Proposal-writer end to end: ScriptedAdapter drives the real kit loop,
policy, ledger, domain tools and acceptance checks through
Specialist.run_milestone, on a copy of the synthetic fixtures in
specialists/proposal_writer/evals/fixtures. Offline: no model, no network.
"""
from __future__ import annotations

import hashlib
import io
import json
import shutil
from pathlib import Path

import pytest

from agentkit.__main__ import main as cli
from agentkit.evidence import evidence_hash
from agentkit.events import MemorySink
from agentkit.llm import ScriptedAdapter
from agentkit.registry import load_specialist
from agentkit.specialist import RunContext
from agentkit.types import Brief, ModelResponse, Submission, ToolCall, Usage
from specialists.proposal_writer import tools as T
from specialists.proposal_writer.agent import CITATION_RULES, ProposalWriter
from specialists.proposal_writer.checks import CHECK_DEFS

FIXTURES = Path(__file__).resolve().parents[2] / "specialists" / "proposal_writer" / "evals" / "fixtures"
RFP = "inputs/solicitation/rfp-2026-14.md"
QUESTIONNAIRE = "inputs/security-questionnaire.csv"
BID_BRIEF = "deliverables/m1-shred/bid-brief.md"
GAPS = "deliverables/m2-outline/gaps.md"
M1_FILES = [T.REQUIREMENTS_PATH, T.MATRIX_PATH, T.FORMAT_RULES_PATH, BID_BRIEF]
M2_FILES = [T.EVIDENCE_MAP_PATH, GAPS, T.OUTLINE_PATH]
M3_FILES = [T.DRAFT_PATH, T.FINAL_MATRIX_PATH, T.CHECKLIST_PATH]

RFP_INTAKE = {"solicitation_files": [RFP], "engagement_mode": "rfp",
              "knowledge_base_files": ["inputs/kb/"], "submission_deadline": "2026-10-15"}
Q_INTAKE = {"solicitation_files": [QUESTIONNAIRE], "engagement_mode": "questionnaire",
            "knowledge_base_files": ["inputs/kb/"], "submission_deadline": "2026-10-09"}

BRIEF_MD = """# Bid / No-Bid Brief: RFP-2026-14 Rider Information Mobile App

## Summary
The Harbor Point Regional Transit District is buying the design, build and operation of a
rider information app for iOS and Android (Section A). The scope covers real-time arrivals
from the GTFS-Realtime feed, trip planning across bus and ferry, push service alerts,
accessibility, rider data security and backend hosting (Section C).

## Key dates
- RFP issued: September 8, 2026
- Pre-proposal conference (optional): September 17, 2026
- Written questions due: September 24, 2026
- Answers posted by the District: October 1, 2026
- Proposals due: October 15, 2026 at 2:00 PM Pacific

## Evaluation criteria
Technical approach, past performance, key personnel and price, as weighted in Section M.
The District may award to other than the lowest priced offeror.

## Risks and open questions
- Only one knowledge-base reference is a transit rider app; the second is a ferry app, and
  Section L.3 asks for two references of similar scope.
- The support desk during District service hours (C.4) has no supporting material yet.

## Recommendation
Conditional bid: bid if the operations lead confirms support desk coverage. The decision is
the customer's.
"""

GAPS_MD = """# SME questions

## Operations lead
- R-011: The knowledge base has nothing on support desk coverage. Which hours can the
  support desk be staffed, and by whom?
"""

OUTLINE_MD = """# Volume I Technical
## Executive Summary [pages: 1] [covers: R-012]
## Technical Approach [pages: 3] [covers: R-002, R-003, R-019, R-023]
## Service Alerts and Accessibility [pages: 1.5] [covers: R-004, R-005, R-006]
## Security and Hosting [pages: 1.5] [covers: R-007, R-008, R-009, R-010, R-011]
## Past Performance [pages: 2] [covers: R-020]
## Key Personnel [pages: 1] [covers: R-021]
# Volume II Price
## Price Summary [pages: 1]
"""

DRAFT_MD = """# Volume I Technical

## Executive Summary
<!-- R-012 -->
Lumen Fieldworks proposes a rider information app for iOS and Android built on its existing arrival engine. Lumen Fieldworks has delivered rider information apps for 6 transit agencies [KB:company-overview.md#p2].

## Technical Approach
<!-- R-002, R-003, R-019, R-023 -->
Our real-time arrival engine consumes GTFS-Realtime feeds and refreshes predictions every 15 seconds [KB:company-overview.md#p2]. The app will plan trips across bus and ferry modes.

## Service Alerts and Accessibility
<!-- R-004, R-005, R-006 -->
Our ferry departure app delivers alerts within 30 seconds of publication [KB:past-performance.md#p2], inside the 60 second requirement [REQ:R-004]. Every release is tested against WCAG 2.1 Level AA with screen reader users [KB:company-overview.md#p3].

## Security and Hosting
<!-- R-007, R-008, R-009, R-010 -->
Rider data is encrypted in transit with TLS 1.2 or higher and at rest with AES-256 [KB:security-practices.md#p1]. Location data is never sold or shared with third parties [KB:security-practices.md#p1]. We notify customers of a security incident within 48 hours of discovery [KB:security-practices.md#p2], within the 72 hour window [REQ:R-008]. Backend services target 99.9% monthly availability [KB:security-practices.md#p3], above the required 99.5% [REQ:R-010].

## Past Performance
<!-- R-020 -->
From 2022 to 2024 Lumen Fieldworks built and operated the Cedar Valley Transit rider app, serving 120,000 monthly active riders [KB:past-performance.md#p1] [C1]. In 2023 Lumen Fieldworks delivered a ferry departure app for the Northgate Ferry Cooperative [KB:past-performance.md#p2].

## Key Personnel
<!-- R-021 -->
Dana Okafor will serve as project manager and has 11 years of experience managing mobile app delivery for public-sector clients [KB:key-personnel.md#p1] [C2].

# Volume II Price

## Price Summary
Pricing is entered by the customer's authorized representative.
"""

CHECKLIST_MD = """# Submission Checklist

## Format
<!-- R-013, R-014, R-015, R-016, R-017, R-018 -->
- Technical Volume within its page limit; Price Volume within its page limit.
- Executive Summary within its word limit; letter-size pages, margins and font as instructed.
- One PDF under the size cap.

## Logistics
<!-- R-001 -->
- Questions sent in writing before the questions deadline.

## Forms
<!-- R-022 -->
- Form P-1 signed by the customer's authorized representative.

## Open items
<!-- R-011 -->
- Support desk coverage during District service hours: the operations lead confirms staffing
  before the Technical Volume is final.
"""

Q_COVER_MD = """# Security Questionnaire Response

## Cover note
Lumen Fieldworks encrypts rider data at rest with AES-256 [KB:security-practices.md#p1] [C1].
Two questions are marked NEEDS REVIEW because the knowledge base does not answer them; the
security lead confirms those answers before the questionnaire is returned.
"""

Q_CHECKLIST_MD = """# Submission Checklist

## Open items
- The security lead answers the two NEEDS REVIEW rows (SEC-04 and SEC-05).
- The authorized representative reviews every answer before returning the questionnaire.
"""


# --- harness -----------------------------------------------------------------------------

@pytest.fixture(scope="module")
def spec() -> ProposalWriter:
    spec = load_specialist("proposal-writer")
    assert isinstance(spec, ProposalWriter)
    return spec


def _workspace(tmp_path: Path, fixture: str) -> Path:
    ws = tmp_path / "ws"
    shutil.copytree(FIXTURES / fixture, ws)
    return ws


def _brief(spec, intake=RFP_INTAKE, *, milestones=True) -> Brief:
    return Brief(engagement_id="eng-pw-1", specialist="proposal-writer",
                 objective="Respond to the customer's solicitation from its knowledge base.",
                 intake=dict(intake), milestones=spec.propose_milestones(intake) if milestones else [])


def _run(spec, ws, milestone, plan, *, brief=None, grader=None, resume=False):
    adapter = plan if isinstance(plan, ScriptedAdapter) else ScriptedAdapter.from_tool_plan(plan)
    events = MemorySink()
    ctx = RunContext(brief=brief or _brief(spec), workspace=ws, adapter=adapter, grader=grader,
                     events=events, resume=resume)
    return spec.run_milestone(ctx, milestone), adapter, events


def _results(sub: Submission) -> dict:
    return {r.check: r for r in sub.check_results}


def _failed(sub: Submission) -> list:
    return [(r.check, r.details) for r in sub.check_results if r.kind == "automated" and r.passed is not True]


def _tool_errors(events: MemorySink) -> list:
    return [(e.data["name"], e.data["content"][:200]) for e in events.of_type("tool_result")
            if e.data["is_error"]]


def _assert_ready(sub: Submission, ws: Path, milestone: str, files: list[str], spec) -> None:
    assert sub.status == "ready_for_review", _failed(sub)
    assert sub.milestone_id == milestone and sub.engagement_id == "eng-pw-1"
    assert all(r.passed is True for r in sub.check_results if r.kind == "automated")
    # every domain check the manifest lists for this milestone ran as automated and passed
    wanted = {a.check for a in spec.manifest.milestone(milestone).acceptance} & set(CHECK_DEFS)
    assert wanted and {r.check for r in sub.check_results if r.kind == "automated"} >= wanted
    results = _results(sub)
    assert results["rubric_grader"].kind == "rubric" and results["rubric_grader"].passed is None
    assert results["human_signoff"].kind == "human" and results["human_signoff"].passed is None
    # the human gate comes from the manifest, whatever the run did
    gate = spec.manifest.human_gate
    assert sub.human_review.required is gate.required is False
    assert sub.human_review.reviewer_role == gate.reviewer_role
    assert sub.human_review.checklist == gate.checklist
    # artifacts are the deliverables, hashed as they are on disk
    arts = {a.path: a for a in sub.artifacts}
    assert set(files) <= set(arts)
    for rel, art in arts.items():
        data = (ws / rel).read_bytes()
        assert art.sha256 == hashlib.sha256(data).hexdigest() and art.bytes == len(data)
    # the evidence hash is over the saved submission
    assert sub.evidence_hash.startswith("0x") and len(sub.evidence_hash) == 66
    saved = json.loads(spec.submission_path(ws, milestone).read_text(encoding="utf-8"))
    assert saved["evidence_hash"] == sub.evidence_hash == evidence_hash(Submission.from_dict(saved))


# --- plans ---------------------------------------------------------------------------------

def _m1_plan(brief_md=BRIEF_MD):
    return [
        ("list_files", {"path": "inputs"}),
        ("read_document", {"path": RFP}),
        [("shred_requirements", {"path": RFP}), ("extract_format_rules", {"path": RFP}),
         ("extract_dates", {"path": RFP})],
        ("write_file", {"path": BID_BRIEF, "content": brief_md}),
        ("submit_milestone", {"summary": "23 requirements shredded; conditional bid", "artifacts": M1_FILES}),
    ]


# build_evidence_map's lexical candidate for R-011 (support desk) is the
# incident-response passage; reading it shows it does not support the
# requirement, so the agent demotes the row to a gap for the SME list.
R011_CANDIDATE = "R-011,instruction,mapped,security-practices.md#p2;past-performance.md#p2,candidate; verify"
R011_GAP = "R-011,instruction,gap,,no KB passage on support desk coverage; asked the operations lead"


def _m2_plan(gaps_md=GAPS_MD):
    return [
        ("build_evidence_map", {}),
        ("kb_search", {"query": "support desk service hours", "top_k": 3}),
        ("edit_file", {"path": T.EVIDENCE_MAP_PATH, "old_text": R011_CANDIDATE, "new_text": R011_GAP}),
        ("write_file", {"path": GAPS, "content": gaps_md}),
        ("write_file", {"path": T.OUTLINE_PATH, "content": OUTLINE_MD}),
        ("check_page_budget", {}),
        ("submit_milestone", {"summary": "Evidence map with one gap; outline within limits",
                              "artifacts": M2_FILES}),
    ]


def _m3_plan(draft_md=DRAFT_MD, pp_quote="serving 120,000 monthly active riders"):
    return [
        [("record_source", {"path": "inputs/kb/past-performance.md", "title": "Past performance"}),
         ("record_source", {"path": "inputs/kb/key-personnel.md", "title": "Key personnel"})],
        [("record_claim", {"text": "Cedar Valley app serves 120,000 monthly riders", "source": "S1",
                           "quote": pp_quote}),
         ("record_claim", {"text": "Project manager has 11 years of experience", "source": "S2",
                           "quote": "Dana has 11 years of experience managing mobile app delivery"})],
        ("write_file", {"path": T.DRAFT_PATH, "content": draft_md}),
        ("write_file", {"path": T.CHECKLIST_PATH, "content": CHECKLIST_MD}),
        ("update_compliance_matrix", {}),
        [("grounding_report", {}), ("check_page_budget", {})],
        ("submit_milestone", {"summary": "Grounded draft, matrix and checklist", "artifacts": M3_FILES}),
    ]


def _grader(rubric_ids, score=0.9):
    call = ToolCall("j1", "score_rubric", {"scores": [
        {"id": cid, "score": score, "rationale": "scripted"} for cid in rubric_ids]})
    return ScriptedAdapter([ModelResponse(text="", tool_calls=[call], stop_reason="tool_use",
                                          usage=Usage(), model="grader")])


# --- wiring --------------------------------------------------------------------------------

def test_specialist_wiring_is_valid(spec):
    assert spec.validate() == []
    assert set(spec.tool_registry().names()) == set(spec.manifest.tools)
    for name in ("shred_complete", "claims_grounded", "questionnaire_answers_grounded"):
        assert spec.check_registry().kind(name) == "automated"   # no brief can demote them
    assert spec.check_registry().kind("rubric_grader") == "rubric"


def test_intake_and_milestones(spec):
    assert [m for m in spec.validate_intake(RFP_INTAKE) if m.blocking] == []
    bad = spec.validate_intake({**RFP_INTAKE, "engagement_mode": "grant"})
    assert [m.field for m in bad if m.blocking] == ["engagement_mode"]
    assert "grant" in bad[-1].question
    rfp = {m.id: m for m in spec.propose_milestones(RFP_INTAKE)}
    q = {m.id: m for m in spec.propose_milestones(Q_INTAKE)}
    assert T.ANSWERS_PATH not in rfp["m3-draft"].deliverables
    assert T.ANSWERS_PATH in q["m3-draft"].deliverables
    assert T.ANSWERS_PATH not in spec.manifest.milestone("m3-draft").deliverables  # manifest untouched


def test_cli_commands(spec, tmp_path):
    intake = tmp_path / "intake.json"
    intake.write_text(json.dumps(RFP_INTAKE), encoding="utf-8")

    def run(*argv):
        out = io.StringIO()
        return cli(list(argv), stdout=out, env={}), out.getvalue()

    code, out = run("list")
    assert code == 0 and "proposal-writer" in out
    assert run("validate", "proposal-writer") == (0, "[]\n")
    code, out = run("show", "proposal-writer")
    assert code == 0 and json.loads(out)["category"] == "Business Ops"
    code, out = run("milestones", "proposal-writer", "--intake", str(intake))
    assert code == 0 and [m["id"] for m in json.loads(out)] == ["m1-shred", "m2-outline", "m3-draft"]
    code, out = run("estimate", "proposal-writer", "--intake", str(intake))
    assert code == 0 and json.loads(out)["hours_low"] == 11
    code, out = run("validate-intake", "proposal-writer", "--intake", str(intake))
    assert code == 0 and not any(m["blocking"] for m in json.loads(out))


# --- happy paths ---------------------------------------------------------------------------

def test_rfp_engagement_all_milestones(spec, tmp_path):
    ws = _workspace(tmp_path, "transit-app")
    solicitation = (ws / RFP).read_bytes()

    sub1, adapter, events = _run(spec, ws, "m1-shred", _m1_plan())
    assert _tool_errors(events) == []
    _assert_ready(sub1, ws, "m1-shred", M1_FILES, spec)
    reqs = T.load_requirements(ws)["requirements"]
    assert len(reqs) == 23 and reqs[12]["text"] == "The Technical Volume shall not exceed ten (10) pages."
    # the model saw the domain tools, the playbooks, the kit rules and the citation rule
    first = adapter.calls[0]
    assert {"shred_requirements", "kb_search", "set_answer"} <= set(first["tools"])
    assert "http_fetch" not in first["tools"] and "run_command" not in first["tools"]
    assert "Shred before you write" in first["system"] and CITATION_RULES in first["system"]
    assert "no network access" in first["system"]
    # domain tool output reaches the model as untrusted data
    shred = next(e.data for e in events.of_type("tool_result") if e.data["name"] == "shred_requirements")
    assert shred["content"].startswith("<untrusted")

    sub2, _, events = _run(spec, ws, "m2-outline", _m2_plan())
    assert _tool_errors(events) == []
    _assert_ready(sub2, ws, "m2-outline", M2_FILES, spec)
    assert _results(sub2)["evidence_map_complete"].score == pytest.approx(14 / 15, abs=1e-4)

    sub3, _, events = _run(spec, ws, "m3-draft", _m3_plan())
    assert _tool_errors(events) == []
    _assert_ready(sub3, ws, "m3-draft", M3_FILES, spec)
    results = _results(sub3)
    assert results["ledger_verified"].passed is True
    assert results["matrix_consistent"].score == 1.0
    assert "not a questionnaire" in results["questionnaire_answers_grounded"].details
    assert (ws / RFP).read_bytes() == solicitation          # inputs/ never changed
    # three milestones, three distinct evidence hashes
    assert len({sub1.evidence_hash, sub2.evidence_hash, sub3.evidence_hash}) == 3


def test_rubric_scored_by_a_grader(spec, tmp_path):
    ws = _workspace(tmp_path, "transit-app")
    ids = ["requirements_summary", "dates_complete", "evaluation_criteria", "risks", "recommendation"]
    sub, _, _ = _run(spec, ws, "m1-shred", _m1_plan(), grader=_grader(ids))
    rubric = _results(sub)["rubric_grader"]
    assert rubric.passed is True and rubric.score == pytest.approx(0.9)
    assert sub.status == "ready_for_review"

    ws2 = _workspace(tmp_path / "low", "transit-app")
    low, _, _ = _run(spec, ws2, "m1-shred", _m1_plan(), grader=_grader(ids, score=0.5))
    assert _results(low)["rubric_grader"].passed is False and low.status == "needs_revision"


def test_resume_returns_the_saved_submission(spec, tmp_path):
    ws = _workspace(tmp_path, "transit-app")
    first, _, _ = _run(spec, ws, "m1-shred", _m1_plan())
    again, adapter, _ = _run(spec, ws, "m1-shred", ScriptedAdapter([]), resume=True)
    assert adapter.calls == [] and again.evidence_hash == first.evidence_hash
    # the CLI re-check of the finished workspace agrees
    out = io.StringIO()
    code = cli(["check", "proposal-writer", "--milestone", "m1-shred", "--workspace", str(ws)],
               stdout=out, env={})
    assert code == 0, out.getvalue()


def test_questionnaire_engagement_answers_or_abstains(spec, tmp_path):
    ws = _workspace(tmp_path, "questionnaire")
    kb = "[KB:security-practices.md#p{}]"
    plan = [
        ("shred_questionnaire", {"path": QUESTIONNAIRE}),
        ("init_answer_sheet", {}),
        [("set_answer", {"question_id": "Q-001", "status": "answered",
                         "answer": "Yes. Data at rest is encrypted with AES-256.", "citations": [kb.format(1)]}),
         ("set_answer", {"question_id": "Q-002", "status": "answered",
                         "answer": "Yes, with TLS 1.2 or higher.", "citations": [kb.format(1)]}),
         ("set_answer", {"question_id": "Q-003", "status": "answered",
                         "answer": "Within 48 hours of discovery.", "citations": [kb.format(2)]}),
         ("set_answer", {"question_id": "Q-004", "status": "answered",      # a fabrication: refused
                         "answer": "Yes, we hold a SOC 2 Type II report.", "citations": [kb.format(1)]}),
         ("set_answer", {"question_id": "Q-004", "status": "needs_review",
                         "answer": "No attestation report in the knowledge base; ask the security lead."}),
         ("set_answer", {"question_id": "Q-005", "status": "needs_review",
                         "answer": "No federal authorization in the knowledge base; ask the security lead."}),
         ("set_answer", {"question_id": "Q-006", "status": "answered",
                         "answer": "A 99.9% monthly availability target.", "citations": [kb.format(3)]})],
        ("record_source", {"path": "inputs/kb/security-practices.md"}),
        ("record_claim", {"text": "Data at rest is encrypted with AES-256", "source": "S1",
                          "quote": "at rest with AES-256"}),
        [("write_file", {"path": T.DRAFT_PATH, "content": Q_COVER_MD}),
         ("write_file", {"path": T.CHECKLIST_PATH, "content": Q_CHECKLIST_MD})],
        ("update_compliance_matrix", {"answers": T.ANSWERS_PATH}),
        # the answer sheet is not listed: finalize adds it to the hashed artifacts
        ("submit_milestone", {"summary": "4 answered, 2 need review", "artifacts": M3_FILES}),
    ]
    # a brief without milestones runs the manifest's m3-draft, which does not
    # declare the answer sheet
    sub, _, events = _run(spec, ws, "m3-draft", plan, brief=_brief(spec, Q_INTAKE, milestones=False))
    errors = _tool_errors(events)
    assert [name for name, _ in errors] == ["set_answer"] and "SOC 2" in errors[0][1]
    _assert_ready(sub, ws, "m3-draft", M3_FILES + [T.ANSWERS_PATH], spec)
    grounded = _results(sub)["questionnaire_answers_grounded"]
    assert grounded.details.startswith("4 answered, 2 abstained of 6")
    rows = {r["req_id"]: r for r in T.read_csv_rows(ws, T.FINAL_MATRIX_PATH)}
    assert rows["Q-004"]["status"] == "needs_review" and rows["Q-001"]["status"] == "addressed"


# --- negative paths ------------------------------------------------------------------------

def test_tampered_shred_fails_and_inputs_stay_read_only(spec, tmp_path):
    ws = _workspace(tmp_path, "transit-app")
    solicitation = (ws / RFP).read_bytes()
    plan = _m1_plan()
    plan[3:3] = [
        # attempts on customer material and on the kit's folder are denied
        [("write_file", {"path": RFP, "content": "The Technical Volume shall not exceed 12 pages."}),
         ("shred_requirements", {"path": ".agentkit/ledger.json"}),
         ("shred_requirements", {"path": RFP, "out": "inputs/requirements.json"})],
        # the agent loosens a page limit in its own shred
        ("edit_file", {"path": T.REQUIREMENTS_PATH, "old_text": "shall not exceed ten (10) pages",
                       "new_text": "shall not exceed twelve (12) pages"}),
    ]
    sub, _, events = _run(spec, ws, "m1-shred", plan)
    assert (ws / RFP).read_bytes() == solicitation
    denied = [e.data["tool"] for e in events.of_type("policy_denied")]
    assert denied == ["write_file", "shred_requirements"]
    assert [n for n, _ in _tool_errors(events)] == ["write_file", "shred_requirements", "shred_requirements"]
    results = _results(sub)
    assert results["shred_complete"].passed is False and "R-013" in results["shred_complete"].details
    assert results["matrix_consistent"].passed is False
    assert sub.status == "needs_revision"
    assert sub.evidence_hash.startswith("0x") and sub.human_review.required is False


def test_unlisted_gap_fails_the_evidence_map(spec, tmp_path):
    ws = _workspace(tmp_path, "transit-app")
    _run(spec, ws, "m1-shred", _m1_plan())
    sub, _, _ = _run(spec, ws, "m2-outline", _m2_plan(gaps_md="# SME questions\n\nNone open.\n"))
    check = _results(sub)["evidence_map_complete"]
    assert check.passed is False and "R-011: gap not in the SME question list" in check.details
    assert sub.status == "needs_revision"


def test_forged_figures_fail_grounding_and_the_ledger(spec, tmp_path):
    ws = _workspace(tmp_path, "transit-app")
    _run(spec, ws, "m1-shred", _m1_plan())
    _run(spec, ws, "m2-outline", _m2_plan())
    forged = DRAFT_MD.replace("for 6 transit agencies", "for 16 transit agencies").replace(
        "11 years", "15 years")
    sub, _, events = _run(spec, ws, "m3-draft",
                          _m3_plan(forged, pp_quote="serving 250,000 monthly active riders"))
    claim_errors = [c for n, c in _tool_errors(events) if n == "record_claim"]
    assert len(claim_errors) == 1 and "not found verbatim" in claim_errors[0]
    results = _results(sub)
    grounding = results["claims_grounded"]
    assert grounding.passed is False and "16" in grounding.details and "15" in grounding.details
    # the forged quote was never recorded, so the draft's [C2] cites nothing
    # (the one claim that was recorded still verifies)
    assert results["ledger_verified"].passed is True
    assert results["citations_resolve"].passed is False and "C2" in results["citations_resolve"].details
    assert sub.status == "needs_revision"
