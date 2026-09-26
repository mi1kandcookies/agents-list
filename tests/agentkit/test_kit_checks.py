"""Acceptance checks (builtins, rubric grader, human sign-off) and evidence hashing."""
import hashlib
import json
import re
import sys
from types import SimpleNamespace

import pytest

from agentkit.checks import CheckContext, CheckRegistry, default_registry, run_check, run_checks
from agentkit.errors import ModelError
from agentkit.evidence import (artifact_for, canonical_json, evidence_hash, media_type,
                               platform_evidence, sha256_file, submission_digest, submission_record)
from agentkit.ledger import Ledger
from agentkit.llm import ScriptedAdapter
from agentkit.policy import PolicyGate, executable_name
from agentkit.tools import ToolContext
from agentkit.types import (AcceptanceCriterion, Artifact, CheckResult, HumanReview, ModelResponse,
                            Submission, ToolCall, Usage)

REPORT = "deliverables/m1/report.md"
GOOD = """# Market Report

## Summary
The widget market reached $4.2 billion in 2025 [C1].

## Sources
See ledger. This is a draft for review, not advice.
"""


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "deliverables/m1").mkdir(parents=True)
    (tmp_path / REPORT).write_text(GOOD, encoding="utf-8")
    led = Ledger(tmp_path)
    led.add_source("https://example.org/r", "r", "The widget market reached $4.2 billion in 2025.")
    led.add_claim("Market size", "S1", "widget market reached $4.2 billion")
    return tmp_path


def _check(ws, name, params=None, kind="automated", ctx=None):
    return run_check(AcceptanceCriterion(check=name, params=params or {}, kind=kind), ws,
                     default_registry(), ctx or CheckContext())


@pytest.mark.parametrize("name,params,passed", [
    ("file_exists", {"path": REPORT}, True),
    ("file_exists", {"path": "deliverables/m1/nope.md"}, False),
    ("files_exist", {"paths": [REPORT, "x.md"]}, False),
    ("markdown_sections", {"path": REPORT, "sections": ["Summary", "sources"]}, True),
    ("markdown_sections", {"path": REPORT, "sections": ["Methodology"]}, False),
    ("no_placeholders", {"path": REPORT}, True),
    ("word_count", {"path": REPORT, "min": 10, "max": 100}, True),
    ("word_count", {"path": REPORT, "min": 500}, False),
    ("ledger_verified", {}, True),
    ("ledger_verified", {"min_claims": 2}, False),
    ("citations_resolve", {"path": REPORT}, True),
    ("citations_resolve", {"path": REPORT, "min_citations": 2}, False),
    ("disclaimer_present", {"path": REPORT, "text": "a draft for  review, NOT advice"}, True),
    ("disclaimer_present", {"path": REPORT, "text": "Not legal advice"}, False),
    ("file_exists", {"path": "../outside.md"}, False),
    ("no_such_check", {}, False),
])
def test_builtin_checks(ws, name, params, passed):
    assert _check(ws, name, params).passed is passed


def test_placeholders_json_csv(ws):
    (ws / "d.md").write_text("ok\nTODO: finish\n[Insert client name]\n", encoding="utf-8")
    r = _check(ws, "no_placeholders", {"paths": ["d.md"]})
    assert r.passed is False and "d.md:2" in r.details and "d.md:3" in r.details
    (ws / "a.json").write_text('{"x": 1}', encoding="utf-8")
    assert _check(ws, "json_valid", {"path": "a.json", "required_keys": ["x"]}).passed
    assert not _check(ws, "json_valid", {"path": "a.json", "required_keys": ["y"]}).passed
    (ws / "b.json").write_text("{bad", encoding="utf-8")
    assert not _check(ws, "json_valid", {"path": "b.json"}).passed
    (ws / "c.csv").write_text("date,amount\n2025-01-01,10\n\n", encoding="utf-8")
    assert _check(ws, "csv_columns", {"path": "c.csv", "columns": ["amount"], "min_rows": 1}).passed
    assert not _check(ws, "csv_columns", {"path": "c.csv", "columns": ["memo"]}).passed
    assert not _check(ws, "csv_columns", {"path": "c.csv", "columns": ["amount"], "min_rows": 2}).passed


@pytest.mark.parametrize("name,params", [
    ("no_placeholders", {}), ("no_placeholders", {"file": REPORT}),
    ("disclaimer_present", {"text": "draft for review"}), ("citations_resolve", {"min_citations": 0}),
])
def test_path_checks_without_a_path_fail_instead_of_passing(ws, name, params):
    r = _check(ws, name, params)
    assert r.passed is False and "no 'path' or 'paths'" in r.details


def test_grouped_citations_are_resolved(ws):
    (ws / REPORT).write_text(GOOD + "\nSee also [C1, C9] and [C1;C1].\n", encoding="utf-8")
    r = _check(ws, "citations_resolve", {"path": REPORT})
    assert r.passed is False and "C9" in r.details


def test_markdown_sections_ignore_code_fences_and_partial_words(ws):
    doc = ("# Report\n\n## 1. Executive Summary\n\n## Non-risk items\n\n```python\n# Methodology\n```\n"
           "~~~\n## Findings\n~~~\n")
    (ws / "d.md").write_text(doc, encoding="utf-8")
    assert _check(ws, "markdown_sections", {"path": "d.md", "sections": ["Summary", "executive summary"]}).passed
    for missing in ("Methodology", "Findings", "Risk"):
        r = _check(ws, "markdown_sections", {"path": "d.md", "sections": [missing]})
        assert r.passed is False, missing


def test_forged_citation_and_tampered_ledger_fail(ws):
    (ws / REPORT).write_text(GOOD + "\nAlso $9 billion by 2030 [C7].\n", encoding="utf-8")
    r = _check(ws, "citations_resolve", {"path": REPORT})
    assert r.passed is False and "C7" in r.details
    data = json.loads((ws / ".agentkit/ledger.json").read_text(encoding="utf-8"))
    data["claims"][0]["quote"] = "widget market reached $9.9 billion"
    (ws / ".agentkit/ledger.json").write_text(json.dumps(data), encoding="utf-8")
    r = _check(ws, "ledger_verified")
    assert r.passed is False and "C1" in r.details


def test_command_succeeds_uses_shell_policy(ws):
    tctx = ToolContext(workspace=ws, policy=PolicyGate(shell_allow=[executable_name(sys.executable)]))
    ctx = CheckContext(run=tctx.run)
    assert _check(ws, "command_succeeds", {"argv": [sys.executable, "-c", "pass"]}, ctx=ctx).passed
    assert not _check(ws, "command_succeeds", {"argv": [sys.executable, "-c", "raise SystemExit(2)"]},
                      ctx=ctx).passed
    denied = _check(ws, "command_succeeds", {"argv": ["curl", "https://x.test"]}, ctx=ctx)
    assert denied.passed is False and "PolicyViolation" in denied.details
    assert _check(ws, "command_succeeds", {"argv": ["python"]}).passed is False  # no runner


def test_human_signoff_is_always_pending(ws):
    r = _check(ws, "human_signoff", {}, kind="human")
    assert r.passed is None and r.kind == "human"
    # a builtin keeps its own kind whatever the criterion claims: human_signoff
    # stays human even if marked automated...
    r = _check(ws, "human_signoff", {}, kind="automated")
    assert r.passed is None and r.kind == "human"
    # ...and an automated check cannot be turned into a pending "human" one
    # (that would drop it from the ready_for_review rule)
    (ws / REPORT).write_text("# Report\n", encoding="utf-8")
    r = _check(ws, "disclaimer_present", {"path": REPORT, "text": "Not legal advice"}, kind="human")
    assert r.passed is False and r.kind == "automated"


def test_domain_check_kind_comes_from_its_def_else_the_criterion(ws):
    reg = default_registry()
    reg.add_defs([{"name": "grader_replay", "function": lambda w, p: {"passed": True}, "kind": "rubric"}])
    reg.add_defs({"ties": lambda w, p: {"passed": True}})
    ctx = CheckContext()
    assert run_check(AcceptanceCriterion("grader_replay"), ws, reg, ctx).kind == "rubric"
    assert run_check(AcceptanceCriterion("ties", kind="human"), ws, reg, ctx).passed is None
    with pytest.raises(ValueError):
        reg.add_defs([{"name": "bad", "function": lambda w, p: {}, "kind": "optional"}])


# --- rubric grader ---------------------------------------------------------------

RUBRIC = "criteria:\n  - {id: accuracy, description: Figures are sourced, weight: 2}\n" \
         "  - {id: clarity, description: Easy to read}\n"


def _grader(scores, name="score_rubric", invalid=None):
    call = ToolCall("j1", name, {"scores": scores}, invalid=invalid)
    return ScriptedAdapter([ModelResponse(text="", tool_calls=[call], stop_reason="tool_use",
                                          usage=Usage(), model="grader")])


def _rubric(ws, grader, threshold=0.8):
    # Rubrics live in the specialist package (the manifest's directory).
    pack = ws.parent / f"{ws.name}-pack"
    pack.mkdir(exist_ok=True)
    (pack / "rubric.yaml").write_text(RUBRIC, encoding="utf-8")
    return _check(ws, "rubric_grader", {"rubric": "rubric.yaml", "paths": [REPORT], "threshold": threshold},
                  kind="rubric", ctx=CheckContext(grader=grader, manifest=SimpleNamespace(base_dir=pack)))


def test_rubric_never_comes_from_the_workspace(ws):
    # e.g. a brief naming a rubric the model wrote under deliverables/
    (ws / "deliverables" / "easy.yaml").write_text("criteria:\n  - {id: x, description: anything}\n",
                                                   encoding="utf-8")
    grader = _grader([{"id": "x", "score": 1, "rationale": ""}])
    pack = ws.parent / f"{ws.name}-pack"
    pack.mkdir(exist_ok=True)
    for rubric, ctx in (("deliverables/easy.yaml", CheckContext(grader=grader)),
                        ("../" + ws.name + "/deliverables/easy.yaml",
                         CheckContext(grader=grader, manifest=SimpleNamespace(base_dir=pack)))):
        r = _check(ws, "rubric_grader", {"rubric": rubric, "paths": [REPORT]}, kind="rubric", ctx=ctx)
        assert r.passed is False and "check error" in r.details
    assert grader.calls == []


def test_rubric_without_grader_is_pending(ws):
    r = _rubric(ws, None)
    assert r.passed is None and r.kind == "rubric" and "no grader" in r.details


def test_rubric_weighted_score(ws):
    grader = _grader([{"id": "accuracy", "score": 0.9, "rationale": "cited"},
                    {"id": "clarity", "score": 0.6, "rationale": "dense"}])
    r = _rubric(ws, grader)
    assert r.passed is True and r.score == pytest.approx(0.8)
    call = grader.calls[0]
    assert call["tools"] == ["score_rubric"]
    assert "<untrusted" in call["messages"][0]["text"] and "accuracy" in call["messages"][0]["text"]
    grader = _grader([{"id": "accuracy", "score": 0.5, "rationale": ""},
                    {"id": "clarity", "score": 0.5, "rationale": ""}])
    assert _rubric(ws, grader).passed is False


@pytest.mark.parametrize("grader", [
    _grader([{"id": "accuracy", "score": 1, "rationale": ""}]),                         # skipped one
    _grader([{"id": "accuracy", "score": 3, "rationale": ""},
            {"id": "clarity", "score": 1, "rationale": ""}]),                          # out of range
    _grader([], name="other_tool"),                                                     # wrong tool
    _grader([], invalid="{oops"),                                                       # bad JSON
    ScriptedAdapter.from_tool_plan(["Looks great, 10/10"]),                                           # prose only
])
def test_rubric_malformed_grader_is_pending(ws, grader):
    assert _rubric(ws, grader).passed is None


def test_rubric_grader_error_is_pending(ws):
    class Broken:
        def complete(self, **kw):
            raise ModelError("down", provider="x")
    assert _rubric(ws, Broken()).passed is None


# --- registry + domain checks --------------------------------------------------------

def test_domain_check_defs_wrapped(ws):
    calls = []

    def ties_out(workspace, params, *, run=None):
        calls.append(run)
        return {"passed": params.get("ok", True), "details": "tied", "score": 1.0}

    def no_run(workspace, params):
        return {"passed": None, "details": "pending"}

    def crashes(workspace, params):
        raise RuntimeError("boom")

    reg = default_registry()
    reg.add_defs({"ties_out": ties_out, "no_run": no_run})
    reg.add_defs([{"name": "crashes", "function": crashes}])
    runner = object()
    results = run_checks([AcceptanceCriterion("ties_out"), AcceptanceCriterion("ties_out", params={"ok": False}),
                          AcceptanceCriterion("no_run"), AcceptanceCriterion("crashes")],
                         ws, reg, CheckContext(run=runner))
    assert [r.passed for r in results] == [True, False, None, False]
    assert calls == [runner, runner] and results[0].score == 1.0 and "boom" in results[3].details
    with pytest.raises(ValueError):
        reg.add_defs({"file_exists": ties_out})
    assert "ties_out" in reg and isinstance(CheckRegistry().names(), list)


# --- evidence --------------------------------------------------------------------------

def _submission(ws, **kw):
    art = artifact_for(ws, REPORT)
    fields = dict(engagement_id="ENG-1", milestone_id="m1", status="ready_for_review", summary="s",
                  artifacts=[art], human_review=HumanReview(required=True, reviewer_role="editor"),
                  check_results=[CheckResult(check="file_exists", passed=True, score=0.8)],
                  usage=Usage(10, 5, cost_usd=0.123457), milestone_idx=1)
    fields.update(kw)
    return Submission(**fields)


def test_evidence_hash_is_the_hash_of_the_platform_evidence_text(ws):
    art = artifact_for(ws, REPORT)
    assert art.sha256 == sha256_file(ws / REPORT) and art.media_type == "text/markdown"
    sub = _submission(ws)
    text = platform_evidence(sub)
    h = evidence_hash(sub)
    assert h == "0x" + hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert re.fullmatch(r"0x[0-9a-f]{64}", h) and text == text.strip() and len(text) <= 4000
    sub.evidence_hash = h
    assert evidence_hash(sub) == h == evidence_hash(json.loads(json.dumps(sub.to_dict())))
    assert evidence_hash(sub, 1) == h
    for change in ({"summary": "changed"}, {"milestone_idx": 2}, {"usage": Usage(10, 5, cost_usd=0.2)},
                   {"check_results": [CheckResult(check="file_exists", passed=True, score=0.81)]},
                   {"models": ["anthropic:claude-opus-4-8"]}):
        assert evidence_hash(_submission(ws, **change)) != h, change
    assert media_type("x.patch") == "text/x-diff" and media_type("x.unknownext") == "application/octet-stream"


def test_evidence_needs_the_sow_milestone_index(ws):
    with pytest.raises(ValueError, match="milestone_idx"):
        evidence_hash(_submission(ws, milestone_idx=None))
    assert evidence_hash(_submission(ws, milestone_idx=None), 1) == evidence_hash(_submission(ws))
    with pytest.raises(ValueError, match="not 3"):
        platform_evidence(_submission(ws), 3)
    for bad in (-1, True, "1"):
        with pytest.raises(ValueError):
            platform_evidence(_submission(ws, milestone_idx=None), bad)


def test_platform_evidence_lists_artifacts_and_checks(ws):
    sub = _submission(ws)
    data = json.loads(platform_evidence(sub))
    assert data == {
        "v": 1, "kind": "agentkit.submission", "engagement_id": "ENG-1", "milestone_idx": 1,
        "milestone_id": "m1", "status": "ready_for_review", "human_review": True,
        "artifacts": [{"path": REPORT, "sha256": sub.artifacts[0].sha256, "bytes": sub.artifacts[0].bytes}],
        "checks": [{"check": "file_exists", "kind": "automated", "passed": True}],
        "submission": submission_digest(sub),
    }
    assert platform_evidence(sub) == canonical_json(data).decode("utf-8")


def test_platform_evidence_digests_long_lists_to_fit_the_limit(ws):
    arts = [Artifact(path=f"deliverables/part-{i:03}/section.md", sha256="a" * 64, bytes=i,
                     media_type="text/markdown") for i in range(60)]
    sub = _submission(ws, artifacts=arts)
    text = platform_evidence(sub)
    data = json.loads(text)
    assert len(text) <= 4000 and "artifacts" not in data and data["artifacts_count"] == 60
    listed = [{"path": a.path, "sha256": a.sha256, "bytes": a.bytes} for a in arts]
    assert data["artifacts_sha256"] == "0x" + hashlib.sha256(canonical_json(listed)).hexdigest()
    assert data["checks"] == [{"check": "file_exists", "kind": "automated", "passed": True}]
    checks = [CheckResult(check=f"domain_check_number_{i:03}", passed=True) for i in range(80)]
    data = json.loads(platform_evidence(_submission(ws, artifacts=arts, check_results=checks)))
    assert "checks" not in data and data["checks_count"] == 80 and data["checks_sha256"].startswith("0x")
    with pytest.raises(ValueError, match="4000"):
        platform_evidence(_submission(ws, engagement_id="E" * 4000))


def test_submission_record_is_integers_and_strings_only(ws):
    record = submission_record(_submission(ws))
    assert record["usage"]["cost_micro_usd"] == 123457
    assert record["check_results"][0]["score_bps"] == 8000
    assert "evidence_hash" not in record and "cost_usd" not in record["usage"]
    canonical_json(record)          # no float anywhere
    none = submission_record(_submission(ws, usage=Usage(1, 1),
                                         check_results=[CheckResult(check="c", passed=None)]))
    assert none["usage"]["cost_micro_usd"] is None and none["check_results"][0]["score_bps"] is None
    for bad in ({"usage": Usage(cost_usd=float("nan"))},
                {"check_results": [CheckResult(check="c", passed=True, score="high")]}):
        with pytest.raises(TypeError):
            submission_record(_submission(ws, **bad))


def test_canonical_json_follows_the_platform_rules():
    assert canonical_json({"b": 1, "a": "é", "c": [True, None, -3]}) == \
        '{"a":"é","b":1,"c":[true,null,-3]}'.encode("utf-8")
    assert canonical_json("a\"b\\c\n\x01 ") == '"a\\"b\\\\c\\n\\u0001 "'.encode()
    # keys sort by code point: U+FF61 before U+10000
    assert canonical_json({"\U00010000": 2, "｡": 1}).decode() == '{"｡":1,"\U00010000":2}'
    for bad in (523.0, 0.1, float("nan"), {"cost": 1.5}, [0.0], {1: "x"}, object(), b"x"):
        with pytest.raises(TypeError):
            canonical_json(bad)
    with pytest.raises(ValueError):                 # not encodable as UTF-8
        canonical_json("x\ud800y")


def test_check_scores_are_kept_to_basis_points(ws):
    reg = CheckRegistry()
    scores = {"fraction": 0.123456, "percent": 87.5, "word": "high", "nan": float("nan"),
              "flag": True, "none": None, "whole": 1}
    reg.add_defs({name: (lambda value: lambda w, p: {"passed": True, "score": value})(value)
                  for name, value in scores.items()})
    got = {name: run_check(AcceptanceCriterion(check=name), ws, reg, CheckContext()).score
           for name in scores}
    assert got == {"fraction": 0.1235, "percent": 87.5, "word": None, "nan": None, "flag": None,
                   "none": None, "whole": 1.0}


def test_media_types_do_not_depend_on_the_host():
    assert media_type("a.ts") == "text/x-typescript"            # Windows registry says video/...
    assert media_type("a.XLSX").endswith("spreadsheetml.sheet")
    assert media_type("a.weird") == media_type("noext") == "application/octet-stream"
