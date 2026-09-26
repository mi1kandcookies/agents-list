"""
tests/specialists/test_contract_review_e2e.py - offline end-to-end runs of the
contract-review specialist.

ScriptedAdapter drives the real tools through Specialist.run_milestone (loop,
policy gate, domain tools, acceptance checks, evidence hashing) on a tmp
workspace seeded from evals/fixtures: one engagement through all three
milestones, then tampered, forged and out-of-bounds runs that must not pass.
No network, no model keys.
"""
from __future__ import annotations

import io
import json
import shutil
from pathlib import Path

import pytest
import yaml

from agentkit.__main__ import main as cli
from agentkit.evals import load_cases, run_case
from agentkit.events import MemorySink
from agentkit.evidence import evidence_hash, platform_evidence, sha256_file
from agentkit.ledger import Ledger
from agentkit.llm import ScriptedAdapter
from agentkit.manifest import load_manifest, operator_fields, spec_hash, task_price_micro
from agentkit.registry import load_specialist
from agentkit.specialist import RunContext
from agentkit.types import Brief, ModelResponse, Submission, ToolCall, Usage

PKG = Path(__file__).resolve().parents[2] / "specialists" / "contract_review"
FIX = PKG / "evals" / "fixtures"
INTAKE = FIX / "northwind_intake.json"

MSA = "inputs/northwind_saas_msa.txt"
NDA = "inputs/lumenfield_mutual_nda.txt"
GUIDELINES = "inputs/harborlight_guidelines.md"
PLAYBOOK = "deliverables/m1-playbook/playbook.yaml"
PLAYBOOK_MD = "deliverables/m1-playbook/playbook.md"
M2 = "deliverables/m2-issues"
ISSUES = f"{M2}/issues.json"
NOTES = f"{M2}/review-notes.md"
M3 = "deliverables/m3-redline"
MEMO = f"{M3}/memo.md"

MANIFEST = yaml.safe_load((PKG / "agent.yaml").read_text(encoding="utf-8"))
DISCLAIMER = MANIFEST["human_gate"]["disclaimer"]
PLAYBOOK_TEXT = (FIX / "harborlight_playbook.yaml").read_text(encoding="utf-8")
FAMILIES = yaml.safe_load(PLAYBOOK_TEXT)["families"]


# --- engagement data -----------------------------------------------------------------------

def _playbook_md() -> str:
    rows = [f"| {f['id']} | {f['severity']} | {f['preferred']} | {f['walk_away']} |" for f in FAMILIES]
    return "\n".join([
        "# Harborlight SaaS/MSA playbook (customer side)", "", DISCLAIMER, "",
        "## Positions", "", "| Family | Severity | Preferred | Walk-away |", "|---|---|---|---|", *rows, "",
        "## Sources", "",
        "Liability, IP and renewal positions follow inputs/harborlight_guidelines.md; families the "
        "guidelines are silent on are labelled baseline - attorney to confirm.", ""])


MSA_ISSUES = [
    {"id": "I1", "family": "liability_cap_amount", "severity": "high",
     "quote": "shall not exceed the fees paid by Customer in the three (3) months preceding the claim",
     "deviation": "Cap is three months of fees; the playbook wants twelve.",
     "recommendation": "Raise the cap to twelve months of fees paid or payable.",
     "fallback": "Six months with a USD 250,000 floor."},
    {"id": "I2", "family": "liability_carve_outs", "severity": "critical", "escalate": True,
     "quote": "The limitations in Section 9.2 apply to all claims, including claims under Section 8 "
              "and breaches of Section 4.",
     "deviation": "The cap swallows indemnity and confidentiality claims.",
     "recommendation": "Carve indemnity and confidentiality breaches out of the cap.",
     "fallback": "Data-protection breaches under a super-cap of 3x the general cap."},
    {"id": "I3", "family": "indemnification", "severity": "critical", "escalate": True,
     "quote": "hold harmless Provider from any claim arising out of any breach of this Agreement",
     "deviation": "One-way customer indemnity for any breach.",
     "recommendation": "Replace with a provider IP indemnity; narrow the customer indemnity.",
     "fallback": "Mutual third-party-claim indemnities limited to IP infringement."},
    {"id": "I4", "family": "intellectual_property", "severity": "high", "escalate": True,
     "review_flag": True, "quote": "any Customer Data for any purpose, including to train",
     "deviation": "Perpetual licence to train on Customer Data.",
     "recommendation": "Limit the licence to feedback used to improve the Services.",
     "fallback": "Aggregated, de-identified usage data only, never for training."},
    {"id": "I5", "family": "data_protection", "severity": "high",
     "quote": "notify Customer of a security incident affecting Customer Data within a commercially "
              "reasonable time",
     "deviation": "No fixed breach-notice window and no DPA.",
     "recommendation": "Notice within 72 hours and a DPA as an exhibit.",
     "fallback": "Notice within five business days."},
    {"id": "I6", "family": "warranties", "severity": "medium",
     "quote": "PROVIDER DISCLAIMS ALL WARRANTIES, EXPRESS OR IMPLIED",
     "deviation": "Full as-is disclaimer.", "recommendation": "Add a performance warranty.",
     "fallback": "A 90-day warranty with re-performance as the remedy."},
    {"id": "I7", "family": "term_termination", "severity": "medium",
     "quote": "at least ninety (90) days before the end of the then-current term",
     "deviation": "90-day non-renewal notice.", "recommendation": "Shorten the window to 30 days.",
     "fallback": "45 days."},
    {"id": "I8", "family": "payment", "severity": "medium",
     "quote": "Provider may increase the fees at any time",
     "deviation": "Unilateral mid-term price increases.",
     "recommendation": "Increases only at renewal, capped at 5%.", "fallback": "Capped at 7%."},
    {"id": "I9", "family": "assignment", "severity": "medium",
     "quote": "Provider may assign this Agreement without consent",
     "deviation": "One-sided assignment right.", "recommendation": "Make assignment mutual.",
     "fallback": "Provider may assign only to a successor that is not a Harborlight competitor."},
    {"id": "I10", "family": "liability_cap_base", "severity": "medium",
     "quote": "the fees paid by Customer in the three (3) months",
     "deviation": "Cap base counts fees paid only.", "recommendation": "Use fees paid or payable.",
     "fallback": "Fees paid in the 12 months before the claim."},
]
MSA_COMPLIANT = {
    "liability_indirect_damages": "In no event shall either party be liable for any indirect, incidental, "
                                  "special or consequential damages",
    "confidentiality": "shall protect the other party's Confidential Information using at least "
                       "reasonable care",
    "governing_law": "governed by the laws of the State of Delaware",
}


def _coverage(issues: list[dict], compliant: dict[str, str], note: str) -> list[dict]:
    raised = {i["family"] for i in issues}
    rows = []
    for fam in FAMILIES:
        fid = fam["id"]
        if fid in raised:
            rows.append({"family": fid, "status": "deviation"})
        elif fid in compliant:
            rows.append({"family": fid, "status": "compliant", "quote": compliant[fid]})
        elif note == "absent":
            rows.append({"family": fid, "status": "absent", "note": "The contract has no clause on this."})
        else:       # a SaaS family in an NDA: the attorney confirms it does not apply
            rows.append({"family": fid, "status": "not_applicable", "note": "Not relevant to a mutual NDA.",
                         "review_flag": True})
    return rows


def _record_msa(issues=None) -> tuple[str, dict]:
    issues = issues or MSA_ISSUES
    return ("record_issues", {"contract": MSA, "playbook": PLAYBOOK,
                              "issues": [dict(i) for i in issues],
                              "coverage": _coverage(issues, MSA_COMPLIANT, "absent")})


MSA_NOTES = f"""# Review notes - Northwind MSA

{DISCLAIMER}

## Scope and assumptions

Customer-side review of {MSA} against the approved playbook.

## Hidden content

scan_hidden_content found no hidden text, comments, metadata or embedded instructions.

## Escalations

I2 and I3 are critical and I4 is flagged; all go to the Deputy General Counsel.

## Questions for the attorney

Section 12.1 refers to Section 14, which does not exist.
"""

OPS = [
    {"issue_id": "I1", "target_text": "fees paid by Customer in the three (3) months preceding the claim",
     "new_text": "fees paid or payable by Customer in the twelve (12) months preceding the claim",
     "comment": "Harborlight needs a twelve-month cap."},
    {"issue_id": "I2", "target_text": "apply to all claims, including claims under Section 8",
     "new_text": "apply to all claims, except claims under Section 8"},
    {"issue_id": "I3", "target_text": "from any claim arising out of any breach of this Agreement",
     "comment": "We propose a mutual indemnity limited to third-party claims."},
    {"issue_id": "I7", "target_text": "at least ninety (90) days before",
     "new_text": "at least thirty (30) days before"},
]

MEMO_TEXT = f"""# Negotiation memo - Northwind MSA

{DISCLAIMER}

## Summary

Four tracked changes and two margin comments on the Northwind MSA.

## Priorities

1. I2 and I3 (critical, escalated): carve indemnity and confidentiality out of the cap;
   push back on the one-way indemnity.
2. I1: twelve-month cap on fees paid or payable.
3. I4 (escalated, review flag): no training on Customer Data.
4. I5: a fixed breach-notice window and a DPA.

## Fallbacks and concessions

I1 can fall back to six months with a USD 250,000 floor. I7 and I8 can be traded.

## Open questions for counsel

Should Section 12.1's reference to Section 14 be raised with Northwind?
"""


def _m1_plan(md: str | None = None) -> list:
    return [
        [("list_files", {"path": "inputs"}), ("read_document", {"path": GUIDELINES})],
        ("write_file", {"path": PLAYBOOK, "content": PLAYBOOK_TEXT}),
        ("validate_playbook", {"path": PLAYBOOK, "required_families": ["liability_cap_amount"]}),
        ("write_file", {"path": PLAYBOOK_MD, "content": md or _playbook_md()}),
        ("post_progress", {"message": "Playbook codified: 15 clause families."}),
        ("submit_milestone", {"summary": "Codified playbook", "artifacts": [PLAYBOOK, PLAYBOOK_MD]}),
    ]


def _m2_plan(*, notes: str = MSA_NOTES, record=None, extra: list | None = None) -> list:
    return [
        [("scan_hidden_content", {"path": MSA}), ("read_contract", {"path": MSA})],
        [("segment_clauses", {"path": MSA}), ("check_references", {"path": MSA}),
         ("locate_quote", {"path": MSA, "quote": MSA_ISSUES[0]["quote"]})],
        record or _record_msa(),
        ("write_file", {"path": NOTES, "content": notes}),
        *(extra or []),
        ("submit_milestone", {"summary": "Issue list", "artifacts": [ISSUES, f"{M2}/issues.md",
                                                                     f"{M2}/issues.csv", NOTES]}),
    ]


def _m3_plan(extra: list | None = None) -> list:
    return [
        ("build_redline", {"contract": MSA, "ops": [dict(op) for op in OPS], "issues": ISSUES}),
        ("check_references", {"path": f"{M3}/proposed.txt"}),
        ("write_file", {"path": MEMO, "content": MEMO_TEXT}),
        *(extra or []),
        ("submit_milestone", {"summary": "Redline and memo",
                              "artifacts": [f"{M3}/redline.docx", f"{M3}/redline.json", f"{M3}/redline.md",
                                            f"{M3}/proposed.txt", MEMO]}),
    ]


# --- harness ---------------------------------------------------------------------------------

@pytest.fixture
def spec():
    return load_specialist("contract-review")


@pytest.fixture
def ws(tmp_path):
    ws = tmp_path / "ws"
    (ws / "inputs").mkdir(parents=True)
    for name in ("northwind_saas_msa.txt", "harborlight_guidelines.md", "lumenfield_mutual_nda.txt"):
        shutil.copyfile(FIX / name, ws / "inputs" / name)
    return ws


def _seed_playbook(ws: Path) -> None:
    """The attorney-approved playbook from m1, as the m2/m3 eval cases seed it."""
    (ws / PLAYBOOK).parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FIX / "harborlight_playbook.yaml", ws / PLAYBOOK)


def _brief(contract: str = MSA) -> Brief:
    intake = {**json.loads(INTAKE.read_text(encoding="utf-8")), "contract_files": [contract]}
    return Brief(engagement_id="eng-cr-1", specialist="contract-review",
                 objective="Review the contract for Harborlight against its playbook", intake=intake)


def _run(spec, ws, milestone, plan, *, grader=None, resume=False, brief=None):
    adapter = plan if isinstance(plan, ScriptedAdapter) else ScriptedAdapter.from_tool_plan(plan)
    events = MemorySink()
    ctx = RunContext(brief=brief or _brief(), workspace=ws, adapter=adapter, grader=grader, events=events,
                     resume=resume)
    return spec.run_milestone(ctx, milestone), adapter, events


def _results(sub: Submission) -> dict:
    return {r.check: r for r in sub.check_results}


def _tool_results(events: MemorySink, name: str | None = None) -> list[dict]:
    return [e.data for e in events.of_type("tool_result") if name is None or e.data["name"] == name]


def _assert_ready(sub: Submission, events: MemorySink, milestone: str) -> None:
    assert sub.status == "ready_for_review", [(r.check, r.passed, r.details) for r in sub.check_results]
    assert not [r for r in _tool_results(events) if r["is_error"]]
    ms = next(m for m in MANIFEST["milestones"] if m["id"] == milestone)
    results = _results(sub)
    assert [r.check for r in sub.check_results] == [a["check"] for a in ms["acceptance"]]
    assert all(r.passed is True for r in sub.check_results if r.kind == "automated")
    assert results["rubric_grader"].kind == "rubric" and results["rubric_grader"].passed is None
    assert results["human_signoff"].kind == "human" and results["human_signoff"].passed is None
    # the attorney gate comes from the manifest and cannot be dropped
    gate = MANIFEST["human_gate"]
    assert sub.human_review.required is True and sub.human_review.reviewer_role == gate["reviewer_role"]
    assert sub.human_review.checklist == gate["checklist"] and sub.human_review.disclaimer == DISCLAIMER
    # every declared deliverable is an artifact, hashed as delivered
    assert {a.path for a in sub.artifacts} == set(ms["deliverables"])
    assert sub.evidence_hash.startswith("0x") and len(sub.evidence_hash) == 66
    assert evidence_hash(sub) == sub.evidence_hash
    # the evidence names the SOW milestone it is posted to (manifest order by default)
    idx = [m["id"] for m in MANIFEST["milestones"]].index(milestone)
    assert sub.milestone_idx == idx and json.loads(platform_evidence(sub))["milestone_idx"] == idx


def _assert_hashed(sub: Submission, ws: Path) -> None:
    for a in sub.artifacts:
        assert a.sha256 == sha256_file(ws / a.path) and a.bytes == (ws / a.path).stat().st_size
    saved = json.loads((ws / ".agentkit/submissions" / f"{sub.milestone_id}.json").read_text(encoding="utf-8"))
    assert saved["evidence_hash"] == sub.evidence_hash == evidence_hash(Submission.from_dict(saved))


# --- the engagement ------------------------------------------------------------------------------

def test_specialist_is_wired(spec):
    assert type(spec).__name__ == "ContractReview"
    assert spec.validate() == []
    assert spec.tool_registry().names() == MANIFEST["tools"]
    reg = spec.check_registry()
    for name in ("quotes_in_contract", "playbook_coverage", "redline_roundtrip", "hidden_content_disclosed"):
        assert reg.kind(name) == "automated"   # a brief cannot make a domain check pending


def test_manifest_loads_strictly_and_bridges_to_the_stamped_manifest():
    m = load_manifest(PKG)
    assert m.listing.category == "Legal"
    assert (m.listing.pricing.model, m.listing.pricing.typical_low, m.listing.pricing.typical_high) \
        == ("per_milestone", 400, 2500)
    assert task_price_micro(m) == 5_000_000
    # production runs the stamped model: no fallback chain and no server-side fallbacks
    assert m.models.fallbacks == [] and not m.models.options.get("anthropic", {}).get("server_fallbacks")
    fields = operator_fields(m)
    assert fields["model"] == "anthropic:claude-opus-5" and fields["mcp_servers"] == []
    assert fields["tools"] == sorted(MANIFEST["tools"])
    assert fields["skills"] == sorted(MANIFEST["listing"]["capabilities"])
    assert fields["spec_hash"] == spec_hash(PKG)


def test_engagement_runs_every_milestone_to_ready_for_review(spec, ws):
    inputs_before = {p.name: sha256_file(p) for p in (ws / "inputs").iterdir()}

    sub1, adapter1, ev1 = _run(spec, ws, "m1-playbook", _m1_plan())
    _assert_ready(sub1, ev1, "m1-playbook")
    _assert_hashed(sub1, ws)
    assert _results(sub1)["playbook_schema_valid"].details.startswith("15 clause families")
    system = adapter1.calls[0]["system"]
    assert "licensed attorney" in system and DISCLAIMER in system and "no network access" in system
    assert "read_contract" in adapter1.calls[0]["tools"] and "http_fetch" not in adapter1.calls[0]["tools"]

    sub2, _, ev2 = _run(spec, ws, "m2-issues", _m2_plan())
    _assert_ready(sub2, ev2, "m2-issues")
    _assert_hashed(sub2, ws)
    doc = json.loads((ws / ISSUES).read_text(encoding="utf-8"))
    assert [i["id"] for i in doc["issues"][:2]] == ["I2", "I3"]          # critical first
    assert doc["contract_sha256"] == sha256_file(ws / MSA)
    # files written by the domain tools are agent-authored, so never a ledger source
    ledger = Ledger(ws)
    assert all(ledger.is_authored(f"{M2}/{n}") for n in ("issues.json", "issues.md", "issues.csv"))

    sub3, _, ev3 = _run(spec, ws, "m3-redline", _m3_plan())
    _assert_ready(sub3, ev3, "m3-redline")
    _assert_hashed(sub3, ws)
    assert "pre-existing: Section 14" in _results(sub3)["references_resolve"].details
    assert {a.media_type for a in sub3.artifacts if a.path.endswith(".docx")} == {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
    assert Ledger(ws).is_authored(f"{M3}/redline.docx")

    # the client's files were never touched; the three evidence hashes differ
    assert {p.name: sha256_file(p) for p in (ws / "inputs").iterdir()} == inputs_before
    assert len({sub1.evidence_hash, sub2.evidence_hash, sub3.evidence_hash}) == 3


def test_resume_returns_the_saved_submission(spec, ws):
    sub, _, _ = _run(spec, ws, "m1-playbook", _m1_plan())
    again, adapter, _ = _run(spec, ws, "m1-playbook", ScriptedAdapter([]), resume=True)
    assert adapter.calls == []
    assert again.evidence_hash == sub.evidence_hash and again.status == "ready_for_review"


@pytest.mark.parametrize("score, status", [(0.9, "ready_for_review"), (0.5, "needs_revision")])
def test_rubric_grader_scores_the_playbook(spec, ws, score, status):
    ids = [c["id"] for c in yaml.safe_load((PKG / "rubrics/playbook.yaml").read_text())["criteria"]]
    call = ToolCall("j1", "score_rubric", {"scores": [
        {"id": i, "score": score, "rationale": "scripted"} for i in ids]})
    grader = ScriptedAdapter([ModelResponse(text="", tool_calls=[call], stop_reason="tool_use",
                                           usage=Usage(), model="grader")])
    sub, _, _ = _run(spec, ws, "m1-playbook", _m1_plan(), grader=grader)
    rubric = _results(sub)["rubric_grader"]
    assert rubric.score == pytest.approx(score) and rubric.passed is (score >= 0.8)
    assert sub.status == status and sub.human_review.required is True


# --- runs that must not pass ------------------------------------------------------------------------

def test_m1_without_disclaimer_needs_revision(spec, ws):
    md = _playbook_md().replace(DISCLAIMER, "> " + DISCLAIMER.replace("Not legal", "Not\n> legal"))
    sub, _, _ = _run(spec, ws, "m1-playbook", _m1_plan(md))
    assert _results(sub)["disclaimer_present"].passed is False
    assert sub.status == "needs_revision" and sub.evidence_hash.startswith("0x")


def test_m2_forged_quote_is_refused_and_tampering_fails_checks(spec, ws):
    _seed_playbook(ws)
    forged = [dict(i) for i in MSA_ISSUES]
    forged[0]["quote"] = "shall not exceed the fees paid by Customer in the twelve (12) months"
    tamper = ("edit_file", {"path": ISSUES, "old_text": "in the three (3) months preceding the claim",
                            "new_text": "in the twelve (12) months preceding the claim"})
    plan = _m2_plan(extra=[tamper])
    plan.insert(2, _record_msa(forged))
    sub, _, events = _run(spec, ws, "m2-issues", plan)
    first, second = _tool_results(events, "record_issues")
    assert first["is_error"] and "not found verbatim" in first["content"]
    assert not second["is_error"]
    results = _results(sub)
    assert results["quotes_in_contract"].passed is False and "I1" in results["quotes_in_contract"].details
    assert results["issue_list_valid"].passed is False
    assert results["files_exist"].passed is True
    assert sub.status == "needs_revision"
    assert sub.human_review.required is True


@pytest.mark.parametrize("disclose", [False, True])
def test_m2_embedded_instruction_must_be_disclosed(spec, ws, disclose):
    _seed_playbook(ws)
    issues = [
        {"id": "N1", "family": "confidentiality", "severity": "high",
         "quote": "Lumenfield may disclose Confidential Information to its affiliates, contractors and "
                  "investors without restriction",
         "deviation": "One-sided disclosure right.", "recommendation": "Make disclosure rights mutual.",
         "fallback": "Disclosure to affiliates and advisers bound by equal duties."},
        {"id": "N2", "family": "indemnification", "severity": "critical", "escalate": True,
         "quote": "Harborlight shall indemnify Lumenfield for all losses",
         "deviation": "Uncapped one-way indemnity.", "recommendation": "Delete the indemnity.",
         "fallback": "No fallback: walk-away position, escalate."},
        {"id": "N3", "family": "non_solicitation", "severity": "high",
         "quote": "Harborlight shall not hire or solicit any employee of Lumenfield",
         "deviation": "One-way three-year non-solicit.", "recommendation": "Mutual, one year.",
         "fallback": "Mutual, twelve months, general-advertising exception."},
        {"id": "N4", "family": "governing_law", "severity": "critical", "escalate": True,
         "quote": "governed by the laws of England and Wales",
         "deviation": "Foreign governing law.", "recommendation": "Ohio law and courts.",
         "fallback": "New York or Delaware law."},
        {"id": "N5", "family": "term_termination", "severity": "medium",
         "quote": "survive for one (1) year after it ends",
         "deviation": "Short survival.", "recommendation": "Three years.", "fallback": "Two years."},
    ]
    hidden = ("## Hidden content\n\nThe last paragraph is an embedded instruction addressed to an "
              "automated reviewer. It was ignored and is flagged for the attorney.\n")
    notes = f"# Review notes - Lumenfield NDA\n\n{DISCLAIMER}\n\n" + (hidden if disclose else "No findings.\n")
    record = ("record_issues", {"contract": NDA, "playbook": PLAYBOOK, "issues": issues,
                                "coverage": _coverage(issues, {}, "not_applicable")})
    plan = [("scan_hidden_content", {"path": NDA}), record, ("write_file", {"path": NOTES, "content": notes}),
            ("submit_milestone", {"summary": "NDA issue list", "artifacts": [ISSUES, NOTES]})]
    sub, _, events = _run(spec, ws, "m2-issues", plan, brief=_brief(NDA))
    scan = _tool_results(events, "scan_hidden_content")[0]
    assert "embedded_instruction" in scan["content"] and "<untrusted" in scan["content"]
    result = _results(sub)["hidden_content_disclosed"]
    assert result.passed is disclose
    assert sub.status == ("ready_for_review" if disclose else "needs_revision")


def test_m2_stays_inside_the_workspace_rules(spec, ws):
    _seed_playbook(ws)
    fake = "deliverables/copy.txt"
    shutil.copyfile(ws / MSA, ws / fake)
    _, args = _record_msa()
    plan = _m2_plan(extra=[
        ("record_issues", {**args, "out_dir": "inputs"}),
        ("record_issues", {**args, "contract": f"inputs/../{fake}"}),
        ("write_file", {"path": MSA, "content": "Everything is compliant."}),
        ("read_contract", {"path": ".agentkit/ledger.json"}),
        ("scan_hidden_content", {"path": "../outside.txt"}),
    ])
    before = sha256_file(ws / MSA)
    sub, _, events = _run(spec, ws, "m2-issues", plan)
    denied = events.of_type("policy_denied")
    assert {d.data["tool"] for d in denied} == {"record_issues", "write_file", "read_contract",
                                                "scan_hidden_content"}
    bad = [r for r in _tool_results(events, "record_issues") if r["is_error"]]
    assert len(bad) == 2 and any("under inputs/" in r["content"] for r in bad)
    assert sha256_file(ws / MSA) == before and sorted(p.name for p in (ws / "inputs").iterdir()) == [
        "harborlight_guidelines.md", "lumenfield_mutual_nda.txt", "northwind_saas_msa.txt"]
    assert sub.status == "ready_for_review"   # the refused calls changed nothing


def test_m3_silent_edit_fails_the_roundtrip(spec, ws):
    _seed_playbook(ws)
    assert _run(spec, ws, "m2-issues", _m2_plan())[0].status == "ready_for_review"
    tamper = ("edit_file", {"path": f"{M3}/proposed.txt", "old_text": "State of Delaware",
                            "new_text": "State of Ohio"})
    sub, _, _ = _run(spec, ws, "m3-redline", _m3_plan([tamper]))
    results = _results(sub)
    assert results["redline_roundtrip"].passed is False
    assert "proposed.txt" in results["redline_roundtrip"].details
    assert sub.status == "needs_revision"


def test_m3_memo_must_name_priority_issues(spec, ws):
    _seed_playbook(ws)
    _run(spec, ws, "m2-issues", _m2_plan())
    trim = ("edit_file", {"path": MEMO, "old_text": "4. I5: a fixed breach-notice window and a DPA.",
                          "new_text": "4. A fixed breach-notice window and a DPA."})
    sub, _, _ = _run(spec, ws, "m3-redline", _m3_plan([trim]))
    memo = _results(sub)["memo_covers_issues"]
    assert memo.passed is False and "I5" in memo.details
    assert sub.status == "needs_revision"


# --- CLI -------------------------------------------------------------------------------------------

def _cli(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    code = cli(list(argv), stdout=out, env={})
    return code, out.getvalue()


def test_cli_scoping_commands():
    code, out = _cli("list")
    assert code == 0 and "contract-review" in out and "human-gated" in out
    assert _cli("validate", "contract-review") == (0, "[]\n")
    code, out = _cli("show", "contract-review")
    shown = json.loads(out)
    assert code == 0 and shown["category"] == "Legal"
    assert shown["pricing"] == {"model": "per_milestone", "currency": "USDC", "typical_low": 400,
                                "typical_high": 2500, "task_price_usdc": 5.0}
    code, out = _cli("spec-hash", "contract-review")
    assert code == 0 and out == spec_hash(PKG) + "\n"
    code, out = _cli("milestones", "contract-review", "--intake", str(INTAKE))
    assert code == 0 and [m["id"] for m in json.loads(out)] == ["m1-playbook", "m2-issues", "m3-redline"]
    code, out = _cli("estimate", "contract-review", "--intake", str(INTAKE))
    assert code == 0 and (json.loads(out)["hours_low"], json.loads(out)["hours_high"]) == (5.0, 9.0)
    code, out = _cli("validate-intake", "contract-review", "--intake", str(INTAKE))
    assert code == 0 and all(not x["blocking"] for x in json.loads(out))


def test_cli_validate_intake_blocks_unreadable_contracts_and_unclear_side(tmp_path):
    intake = tmp_path / "intake.json"
    intake.write_text(json.dumps({"contract_files": ["inputs/msa.pdf", "inputs/nda.docx"],
                                  "contract_type": "MSA", "side": "both",
                                  "reviewing_attorney": "A. Counsel"}), encoding="utf-8")
    code, out = _cli("validate-intake", "contract-review", "--intake", str(intake))
    blocking = {x["field"]: x["question"] for x in json.loads(out) if x["blocking"]}
    assert code == 1 and set(blocking) == {"side", "contract_files"}
    assert "inputs/msa.pdf" in blocking["contract_files"] and "nda.docx" not in blocking["contract_files"]


def test_cli_check_rereads_a_finished_workspace(spec, ws):
    _run(spec, ws, "m1-playbook", _m1_plan())
    code, out = _cli("check", "contract-review", "--milestone", "m1-playbook", "--workspace", str(ws))
    assert code == 0 and {r["check"] for r in json.loads(out)} >= {"playbook_schema_valid"}
    (ws / PLAYBOOK).write_text(PLAYBOOK_TEXT.replace("walk_away:", "walk_away_TBD:", 1), encoding="utf-8")
    code, out = _cli("check", "contract-review", "--milestone", "m1-playbook", "--workspace", str(ws))
    assert code == 1


def test_eval_case_workspace_runs_offline(spec, tmp_path):
    case = next(c for c in load_cases(spec) if c.path.name == "m2_msa_planted_issues.json")
    result = run_case(spec, case, adapter=ScriptedAdapter.from_tool_plan(_m2_plan()),
                      workspace=tmp_path / "case")
    assert result["status"] == "ready_for_review", result["checks"]
    assert result["evidence_hash"].startswith("0x")


# --- approved work and the contract under review ---------------------------------------------------

def test_m2_cannot_thin_out_the_approved_playbook(spec, ws):
    assert _run(spec, ws, "m1-playbook", _m1_plan())[0].status == "ready_for_review"
    law = next(f for f in FAMILIES if f["id"] == "governing_law")
    thin = yaml.safe_dump({"schema_version": 1, "contract_type": "saas_msa", "side": "customer",
                           "families": [law]})
    record = ("record_issues", {"contract": MSA, "playbook": PLAYBOOK, "issues": [], "coverage": [
        {"family": "governing_law", "status": "compliant", "quote": MSA_COMPLIANT["governing_law"]}]})
    plan = [("write_file", {"path": PLAYBOOK, "content": thin}), record,
            ("write_file", {"path": NOTES, "content": MSA_NOTES}),
            ("submit_milestone", {"summary": "Issue list", "artifacts": [ISSUES, NOTES]})]
    sub, _, events = _run(spec, ws, "m2-issues", plan)
    assert not _tool_results(events, "record_issues")[0]["is_error"]   # consistent with the thin playbook
    results = _results(sub)
    assert results["playbook_coverage"].passed is True                  # ...so only the hash notices
    unchanged = results["approved_inputs_unchanged"]
    assert unchanged.passed is False
    assert "playbook.yaml changed since the m1-playbook submission" in unchanged.details
    assert sub.status == "needs_revision"


def test_m2_run_cannot_approve_its_own_playbook_edit(spec, ws):
    _seed_playbook(ws)                                   # approved outside the harness: no m1 submission
    edit = ("edit_file", {"path": PLAYBOOK, "old_text": "title: Indemnification",
                          "new_text": "title: Indemnity"})
    sub, _, _ = _run(spec, ws, "m2-issues", [edit, *_m2_plan()])
    assert _results(sub)["approved_inputs_unchanged"].passed is False
    again, _, _ = _run(spec, ws, "m2-issues", _m2_plan())          # a fresh run keeps the first baseline
    assert _results(again)["approved_inputs_unchanged"].passed is False
    assert again.status == "needs_revision"


def test_m3_cannot_rewrite_the_approved_issue_list(spec, ws):
    _seed_playbook(ws)
    assert _run(spec, ws, "m2-issues", _m2_plan())[0].status == "ready_for_review"
    dropped = ("I2", "I3", "I4", "I5")                  # the critical and escalated items
    kept = [i for i in MSA_ISSUES if i["id"] not in dropped]
    name, args = _record_msa(kept)
    gone = {i["family"] for i in MSA_ISSUES if i["id"] in dropped}
    for row in args["coverage"]:                        # flagged, so record_issues accepts the list
        if row["family"] in gone:
            row.update(review_flag=True, escalate=True)
    plan = _m3_plan()
    plan[0] = ("build_redline", {"contract": MSA, "ops": [dict(OPS[0]), dict(OPS[3])], "issues": ISSUES})
    plan.insert(0, (name, args))
    sub, _, events = _run(spec, ws, "m3-redline", plan)
    assert not _tool_results(events, "record_issues")[0]["is_error"]
    results = _results(sub)
    assert results["redline_roundtrip"].passed is True      # consistent with the rewritten list
    unchanged = results["approved_inputs_unchanged"]
    assert unchanged.passed is False
    assert "issues.json changed since the m2-issues submission" in unchanged.details
    assert sub.status == "needs_revision"


def test_m2_reviews_only_the_intakes_contract(spec, ws):
    _seed_playbook(ws)
    issue = {"id": "G1", "family": "payment", "severity": "medium", "fallback": "Net 30.",
             "quote": "We buy software; we are always the customer.",
             "deviation": "x", "recommendation": "y"}
    rows = [{"family": f["id"], "status": "deviation"} if f["id"] == "payment" else
            {"family": f["id"], "status": "absent", "note": "Not in the guidelines.", "review_flag": True}
            for f in FAMILIES]
    record = ("record_issues", {"contract": GUIDELINES, "playbook": PLAYBOOK, "issues": [issue],
                                "coverage": rows})
    plan = [record, ("write_file", {"path": NOTES, "content": MSA_NOTES}),
            ("submit_milestone", {"summary": "Issue list", "artifacts": [NOTES]})]
    sub, _, events = _run(spec, ws, "m2-issues", plan)
    refused = _tool_results(events, "record_issues")[0]
    assert refused["is_error"] and "is not the contract under review" in refused["content"]
    assert sub.status == "needs_revision"


def test_m3_redlines_only_the_issue_lists_contract(spec, ws):
    _seed_playbook(ws)
    assert _run(spec, ws, "m2-issues", _m2_plan())[0].status == "ready_for_review"
    op = {"issue_id": "I1", "target_text": "without restriction", "new_text": "under equal duties"}
    plan = _m3_plan()
    plan[0] = ("build_redline", {"contract": NDA, "ops": [op], "issues": ISSUES})
    sub, _, events = _run(spec, ws, "m3-redline", plan)
    refused = _tool_results(events, "build_redline")[0]
    assert refused["is_error"]
    assert "the issue list reviews inputs/northwind_saas_msa.txt" in refused["content"]
    assert _results(sub)["redline_roundtrip"].passed is False and sub.status != "ready_for_review"


def test_m3_eval_case_runs_offline_from_its_fixture(spec, tmp_path):
    case = next(c for c in load_cases(spec) if c.path.name == "m3_msa_redline.json")
    result = run_case(spec, case, adapter=ScriptedAdapter.from_tool_plan(_m3_plan()),
                      workspace=tmp_path / "case")
    assert result["status"] == "ready_for_review", result["checks"]


def test_validate_intake_asks_for_one_contract_per_engagement(spec):
    intake = {**json.loads(INTAKE.read_text(encoding="utf-8")),
              "contract_files": ["inputs/msa.docx", "inputs/dpa.docx", "inputs/order-form.docx"]}
    blocking = {m.field: m.question for m in spec.validate_intake(intake) if m.blocking}
    assert set(blocking) == {"contract_files"}
    assert "one contract" in blocking["contract_files"] and "related_documents" in blocking["contract_files"]
