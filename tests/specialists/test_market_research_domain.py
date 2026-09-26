"""
tests/specialists/test_market_research_domain.py - market-research domain
pack: tools, checks and manifest, offline against a synthetic workspace.
"""
from __future__ import annotations

import csv
import json
import shutil
from pathlib import Path

import pytest

from agentkit.errors import ToolError
from agentkit.ledger import Ledger
from specialists.market_research import tools

PKG = Path(__file__).resolve().parents[2] / "specialists" / "market_research"
FIXTURE = PKG / "evals" / "fixtures" / "hvac-scheduling"


@pytest.fixture()
def ws(tmp_path: Path) -> Path:
    """A copy of the synthetic HVAC workspace (inputs/ + ledger + snapshots)."""
    dest = tmp_path / "ws"
    shutil.copytree(FIXTURE, dest)
    (dest / "questions.json").unlink()
    return dest


def _tree() -> dict:
    return json.loads((FIXTURE / "questions.json").read_text(encoding="utf-8"))


def _save_tree(ws: Path) -> dict:
    tree = _tree()
    return tools.write_question_tree(ws, objective=tree["objective"], questions=tree["questions"],
                                     evidence_cutoff=tree["evidence_cutoff"])


def _sizing_args(**over) -> dict:
    args = {
        "currency": "USD",
        "horizon": "3 years",
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
    }
    args.update(over)
    return args


# --- tools -----------------------------------------------------------------------

def test_write_question_tree_saves_valid_tree(ws):
    out = _save_tree(ws)
    assert out["leaf_ids"] == ["Q1.1", "Q1.2", "Q2.1"]
    saved = json.loads((ws / tools.QUESTIONS_PATH).read_text(encoding="utf-8"))
    assert saved["evidence_cutoff"] == "2026-09-01"


def test_write_question_tree_rejects_leaf_without_primary_source(ws):
    tree = _tree()
    tree["questions"][1]["children"][0]["source_types"] = ["review", "news"]
    with pytest.raises(ToolError, match="primary"):
        tools.write_question_tree(ws, objective="x", questions=tree["questions"])
    assert not (ws / tools.QUESTIONS_PATH).exists()


def test_write_question_tree_rejects_duplicates_and_bad_cutoff(ws):
    tree = _tree()
    tree["questions"][1]["id"] = "Q1"
    with pytest.raises(ToolError, match="duplicate id"):
        tools.write_question_tree(ws, objective="x", questions=tree["questions"])
    with pytest.raises(ToolError, match="YYYY-MM-DD"):
        tools.write_question_tree(ws, objective="x", questions=_tree()["questions"], evidence_cutoff="Sept 1")


@pytest.mark.parametrize("uri,declared,tier,kind", [
    ("https://data.statistics-bureau.example.gov/x", None, 1, "government"),
    ("https://www.sec.gov/cgi-bin/browse-edgar", None, 1, "filing"),
    ("inputs/win-loss.md", None, 1, "client"),
    ("workspace:inputs/win-loss.md", None, 1, "client"),               # the kit's record_source uri
    ("workspace:deliverables/notes.md", "company", 1, "company"),       # declared, not client
    ("https://brightwrench.example.com/pricing", "company", 1, "company"),
    ("https://fieldops-weekly.example.net/a", "trade_press", 2, "trade_press"),
    ("https://www.reddit.com/r/hvac", "company", 3, "community"),       # host wins over the hint
    ("https://blog.example.com/stats", "government", 3, "other"),        # cannot claim government
    ("https://unknown.example.com/", None, 3, "other"),
])
def test_classify(uri, declared, tier, kind):
    got = tools.classify(uri, declared)
    assert (got["tier"], got["source_type"]) == (tier, kind)


def test_classify_source_persists_declared_type(ws):
    tools.classify_source(ws, uri="https://brightwrench.example.com/pricing", source_id="S2", source_type="company")
    saved = json.loads((ws / tools.SOURCE_TIERS_PATH).read_text(encoding="utf-8"))
    assert saved["S2"]["tier"] == 1
    with pytest.raises(ToolError):
        tools.classify_source(ws, uri="https://x.example.com", source_type="rumour")


def test_verify_claims_detects_forged_quote_and_missing_snapshot(ws):
    ledger = json.loads((ws / tools.LEDGER_PATH).read_text(encoding="utf-8"))
    ledger["claims"][0]["quote"] = "In 2025 there were 218,400 HVAC contractor establishments"
    (ws / tools.SNAPSHOT_DIR / "S3.txt").unlink()
    (ws / tools.LEDGER_PATH).write_text(json.dumps(ledger), encoding="utf-8")
    status = tools.verify_claims(ws)
    assert status["C1"] == {**status["C1"], "verified": False,
                            "reason": "quote not found verbatim in source S1"}
    assert status["C4"] == {**status["C4"], "verified": False, "reason": "snapshot of S3 is missing"}
    assert status["C2"]["verified"] is True


def test_verify_claims_detects_tampered_snapshot(ws):
    ledger = json.loads((ws / tools.LEDGER_PATH).read_text(encoding="utf-8"))
    ledger["sources"][1]["sha256"] = tools.snapshot_sha256(ws, "S2")
    (ws / tools.LEDGER_PATH).write_text(json.dumps(ledger), encoding="utf-8")
    assert tools.verify_claims(ws)["C3"]["verified"] is True
    snap = ws / tools.SNAPSHOT_DIR / "S2.txt"
    snap.write_text(snap.read_text(encoding="utf-8") + "edited\n", encoding="utf-8")
    assert tools.verify_claims(ws)["C3"] == {**tools.verify_claims(ws)["C3"], "verified": False,
                                             "reason": "snapshot of S2 changed after it was recorded"}


def test_verify_claims_follows_the_kit_ledger(tmp_path):
    ledger = Ledger(tmp_path)
    web = ledger.add_source("https://x.example.com/a", "A", "The vendor\u2019s  Pro plan costs $59 a month.",
                            kind="web")
    ledger.add_claim("Pro costs $59", web.id, "The vendor's Pro plan costs $59")   # normalized match
    notes = ledger.add_source("workspace:repo/NOTES.md", "notes", "Churn was 4% in 2025 per the team.",
                              kind="customer")
    ledger.add_claim("Churn was 4%", notes.id, "Churn was 4% in 2025")
    ledger.note_authored("repo/notes.md")        # the agent wrote that file: never a source
    status = tools.verify_claims(tmp_path)
    assert status["C1"]["verified"] is True and status["C1"]["source"]["kind"] == "web"
    assert status["C2"]["verified"] is False and "written during the engagement" in status["C2"]["reason"]
    kit_rejects = {p.split(":")[0] for p in Ledger(tmp_path).verify()}
    assert {c for c, s in status.items() if not s["verified"]} == kit_rejects == {"C2"}


def test_map_claims_merges_and_validates(ws):
    _save_tree(ws)
    tools.map_claims(ws, mapping={"C2": ["Q1.1"]})
    out = tools.map_claims(ws, mapping={"C1": ["Q1.1"], "C2": ["Q1.2"]}, notes={"Q2.1": "only two vendors"})
    assert out["claims_per_question"] == {"Q1.1": 2, "Q1.2": 1, "Q2.1": 0}
    saved = json.loads((ws / tools.CLAIM_MAP_PATH).read_text(encoding="utf-8"))
    assert saved["claims"]["C2"] == ["Q1.1", "Q1.2"]
    with pytest.raises(ToolError, match="not leaf"):
        tools.map_claims(ws, mapping={"C1": ["Q1"]})
    with pytest.raises(ToolError, match="unknown claim"):
        tools.map_claims(ws, mapping={"C99": ["Q1.1"]})


def test_map_claims_requires_tree(ws):
    with pytest.raises(ToolError, match="question tree"):
        tools.map_claims(ws, mapping={"C1": ["Q1.1"]})


def test_export_evidence_writes_verified_rows(ws):
    _save_tree(ws)
    tools.classify_source(ws, uri="https://brightwrench.example.com/pricing", source_id="S2", source_type="company")
    tools.map_claims(ws, mapping={"C1": ["Q1.1"], "C3": ["Q2.1"]})
    out = tools.export_evidence(ws)
    assert out["claims"] == 7 and out["unverified"] == []
    rows = list(csv.DictReader((ws / tools.CLAIMS_CSV_PATH).open(encoding="utf-8")))
    by_id = {r["claim_id"]: r for r in rows}
    assert by_id["C1"]["tier"] == "1" and by_id["C1"]["question_ids"] == "Q1.1"
    assert by_id["C3"]["source_type"] == "company"
    assert by_id["C6"]["tier"] == "3"
    assert len(by_id["C1"]["snapshot_sha256"]) == 64


def test_build_competitor_matrix(ws):
    out = tools.build_competitor_matrix(ws, dimensions=["Entry price", "Scheduling"], competitors=[
        {"name": "Brightwrench", "cells": {"Entry price": {"value": "$59/tech/month", "claim": "C3", "as_of": "2026-09-01"}}},
        {"name": "Tallyho Dispatch", "cells": {"Entry price": {"value": "$49/user/month", "claim": "C4", "as_of": "2026-09-01"}}},
    ])
    assert out["cited_claims"] == ["C3", "C4"] and out["empty_cells"] == 2
    text = (ws / tools.MATRIX_PATH).read_text(encoding="utf-8")
    assert "$59/tech/month (as of 2026-09-01) [C3]" in text


def test_build_competitor_matrix_rejects_undated_price_and_forged_claim(ws):
    with pytest.raises(ToolError, match="as_of"):
        tools.build_competitor_matrix(ws, dimensions=["Pricing"], competitors=[
            {"name": "Brightwrench", "cells": {"Pricing": {"value": "$59", "claim": "C3"}}}])
    ledger = json.loads((ws / tools.LEDGER_PATH).read_text(encoding="utf-8"))
    ledger["claims"][2]["quote"] = "Pro plan: $39 per technician per month"
    (ws / tools.LEDGER_PATH).write_text(json.dumps(ledger), encoding="utf-8")
    with pytest.raises(ToolError, match="do not verify"):
        tools.build_competitor_matrix(ws, dimensions=["Pricing"], competitors=[
            {"name": "Brightwrench", "cells": {"Pricing": {"value": "$39", "claim": "C3", "as_of": "2026-09-01"}}}])
    assert not (ws / tools.MATRIX_PATH).exists()


def test_build_sizing_model_computes_and_saves(ws):
    out = tools.build_sizing_model(ws, **_sizing_args(sensitivity=["bottom_up.price"]))
    assert out["tam_top_down"] == pytest.approx(418e6)
    assert out["tam_bottom_up"] == pytest.approx(91200 * 3888)
    assert out["sam"] == pytest.approx(91200 * 3888 * 0.6)
    assert out["som"] == pytest.approx(91200 * 3888 * 0.6 * 0.05)
    assert out["needs_reconciliation"] is False
    saved = json.loads((ws / tools.SIZING_PATH).read_text(encoding="utf-8"))
    assert [r["change"] for r in saved["sensitivity_table"]] == [-0.25, -0.10, 0.10, 0.25]
    assert saved["sensitivity_table"][0]["tam"] == pytest.approx(91200 * 3888 * 0.75)


def test_build_sizing_model_flags_gap_and_rejects_untraced_inputs(ws):
    args = _sizing_args()
    args["bottom_up"]["adoption"] = {"name": "adoption", "value": 0.38, "claim": "C5"}
    assert tools.build_sizing_model(ws, **args)["needs_reconciliation"] is True
    args = _sizing_args()
    args["sam_share"] = {"name": "sam", "value": 0.6}
    with pytest.raises(ToolError, match="assumption"):
        tools.build_sizing_model(ws, **args)
    with pytest.raises(ToolError, match="unknown claim"):
        tools.build_sizing_model(ws, **_sizing_args(som_share={"name": "som", "value": 0.1, "claim": "C42"}))
    with pytest.raises(ToolError, match="non-negative"):
        tools.build_sizing_model(ws, **_sizing_args(som_share={"name": "som", "value": -1, "assumption": "x"}))


def test_tool_defs_are_well_formed():
    names = [d["name"] for d in tools.TOOL_DEFS]
    assert len(names) == len(set(names))
    for d in tools.TOOL_DEFS:
        assert set(d) == {"name", "description", "input_schema", "risk", "function"}
        assert d["input_schema"]["type"] == "object" and callable(d["function"])
        assert d["risk"] in {"read", "write", "exec", "network", "external"}


# --- checks ----------------------------------------------------------------------

from specialists.market_research import checks  # noqa: E402

KIT_BUILTIN_CHECKS = {"file_exists", "files_exist", "markdown_sections", "no_placeholders", "word_count",
                      "json_valid", "csv_columns", "command_succeeds", "ledger_verified",
                      "citations_resolve", "disclaimer_present", "rubric_grader", "human_signoff"}
KIT_TOOLS = {"read_file", "write_file", "edit_file", "list_files", "search_files", "run_command",
             "http_fetch", "web_search", "read_document", "record_source", "record_claim",
             "ask_client", "post_progress", "submit_milestone"}

REPORT = """# HVAC scheduling market

## Executive summary
Small contractors are numerous [C2] and incumbents price near $50 [C3][C4].

## Traceability matrix
| Question | Finding | Claims |
|---|---|---|
| Q1.1 | 91,200 small contractors | [C2] |
| Q1.2 | 38% already use software | [C5] |
| Q2.1 | unresolved: only two vendors priced | |
"""


def _evidence_ws(ws: Path) -> Path:
    _save_tree(ws)
    tools.map_claims(ws, mapping={"C1": ["Q1.1"], "C2": ["Q1.1"], "C5": ["Q1.2"], "C3": ["Q2.1"], "C4": ["Q2.1"]},
                     notes={"Q1.2": "Insufficient evidence: one survey only", "Q2.1": "insufficient evidence"})
    tools.export_evidence(ws)
    return ws


def _edit_ledger(ws: Path, fn) -> None:
    path = ws / tools.LEDGER_PATH
    ledger = json.loads(path.read_text(encoding="utf-8"))
    fn(ledger)
    path.write_text(json.dumps(ledger), encoding="utf-8")


def test_question_tree_valid_check(ws):
    _save_tree(ws)
    assert checks.question_tree_valid(ws, {})["passed"] is True
    tree = json.loads((ws / tools.QUESTIONS_PATH).read_text(encoding="utf-8"))
    tree["questions"][0]["children"][0]["source_types"] = ["trade_press"]
    (ws / tools.QUESTIONS_PATH).write_text(json.dumps(tree), encoding="utf-8")
    res = checks.question_tree_valid(ws, {"min_source_types": 2})
    assert res["passed"] is False and "Q1.1" in res["details"]
    (ws / tools.QUESTIONS_PATH).write_text("{not json", encoding="utf-8")
    assert checks.question_tree_valid(ws, {})["passed"] is False


def test_evidence_export_matches_ledger(ws):
    _evidence_ws(ws)
    assert checks.evidence_export_matches_ledger(ws, {})["passed"] is True


def test_evidence_export_rejects_edited_csv(ws):
    _evidence_ws(ws)
    path = ws / tools.CLAIMS_CSV_PATH
    path.write_text(path.read_text(encoding="utf-8").replace("91,200", "191,200"), encoding="utf-8")
    res = checks.evidence_export_matches_ledger(ws, {})
    assert res["passed"] is False and "C2" in res["details"]


def test_evidence_export_rejects_inflated_tier_and_forged_quote(ws):
    _evidence_ws(ws)
    path = ws / tools.CLAIMS_CSV_PATH
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    for r in rows:
        if r["claim_id"] == "C6":
            r["tier"] = "1"
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=tools.CLAIMS_CSV_COLUMNS)
        w.writeheader()
        w.writerows(rows)
    assert "C6: tier" in checks.evidence_export_matches_ledger(ws, {})["details"]
    # forge a quote in the ledger and re-export: the export is honest, the check still fails
    _edit_ledger(ws, lambda led: led["claims"][6].update(quote="HVAC accounted for 42% of it"))
    tools.export_evidence(ws)
    res = checks.evidence_export_matches_ledger(ws, {})
    assert res["passed"] is False and "C7: quote does not verify" in res["details"]


def test_evidence_export_missing_file_or_empty_ledger(ws):
    assert checks.evidence_export_matches_ledger(ws, {})["passed"] is False
    _evidence_ws(ws)
    _edit_ledger(ws, lambda led: led.update(claims=[]))
    assert checks.evidence_export_matches_ledger(ws, {})["passed"] is False


def test_question_coverage(ws):
    _evidence_ws(ws)
    res = checks.question_coverage(ws, {"min_claims": 2})
    assert res["passed"] is True and res["score"] == 1.0
    res = checks.question_coverage(ws, {"min_claims": 3})   # Q1.1 has 2 claims and no note
    assert res["passed"] is False and "Q1.1" in res["details"]


def test_question_coverage_ignores_forged_claims(ws):
    _evidence_ws(ws)
    _edit_ledger(ws, lambda led: led["claims"][0].update(quote="there were 999,999 establishments"))
    res = checks.question_coverage(ws, {"min_claims": 2})
    assert res["passed"] is False and "Q1.1: 1 verified" in res["details"]


def test_source_tier_mix(ws):
    # C1, C2 come from a government host (tier 1); the rest are undeclared (tier 3): 2/7
    res = checks.source_tier_mix(ws, {"min_tier1_share": 0.6})
    assert res["passed"] is False and res["score"] == pytest.approx(2 / 7, abs=1e-4)
    for sid in ("S2", "S3"):
        tools.classify_source(ws, uri=f"https://x.example.com/{sid}", source_id=sid, source_type="company")
    assert checks.source_tier_mix(ws, {"min_tier1_share": 0.5})["passed"] is True


def test_source_tier_mix_cannot_be_raised_by_declared_government(ws):
    tools.classify_source(ws, uri="https://market-notes.example.org", source_id="S5", source_type="government")
    tools.classify_source(ws, uri="https://fieldops-weekly.example.net", source_id="S4", source_type="filing")
    assert checks.source_tier_mix(ws, {})["score"] == pytest.approx(2 / 7, abs=1e-4)


def _matrix(ws: Path) -> None:
    tools.build_competitor_matrix(ws, dimensions=["Entry price", "Scheduling"], competitors=[
        {"name": "Brightwrench", "cells": {"Entry price": {"value": "$59", "claim": "C3", "as_of": "2026-09-01"}}},
        {"name": "Tallyho Dispatch", "cells": {"Entry price": {"value": "$49", "claim": "C4", "as_of": "2026-09-01"}}},
    ])


def test_matrix_cells_cited(ws):
    _matrix(ws)
    assert checks.matrix_cells_cited(ws, {})["passed"] is True


def test_matrix_cells_cited_rejects_hand_edits(ws):
    _matrix(ws)
    path = ws / tools.MATRIX_PATH
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("$49 (as of 2026-09-01) [C4]", "$39 [C9]")
                        .replace(",\n", ",Drag-and-drop\n", 1), encoding="utf-8")
    res = checks.matrix_cells_cited(ws, {})
    assert res["passed"] is False
    assert "C9 is not a verified" in res["details"] and "without a date" in res["details"]
    assert "no [C#] citation" in res["details"]


def test_grouped_citations_read_like_the_kit(ws):
    assert tools.citations_in("see [C12] and [C3, C4; C3]") == ["C3", "C4", "C12"]
    _evidence_ws(ws)
    _write_report(ws, REPORT.replace("| [C5] |", "| [C5, C1] |"))
    res = checks.report_answers_questions(ws, {})
    assert res["passed"] is True and res["score"] == pytest.approx(2 / 3, abs=1e-4)
    _matrix(ws)
    path = ws / tools.MATRIX_PATH
    path.write_text(path.read_text(encoding="utf-8").replace("[C4]", "[C4; C99]"), encoding="utf-8")
    assert "C99 is not a verified" in checks.matrix_cells_cited(ws, {})["details"]


def test_sizing_model_consistent(ws):
    tools.build_sizing_model(ws, **_sizing_args())
    res = checks.sizing_model_consistent(ws, {"tolerance": 0.30})
    assert res["passed"] is True and res["score"] == pytest.approx(3 / 7, abs=1e-4)


def test_sizing_model_rejects_edited_outputs(ws):
    tools.build_sizing_model(ws, **_sizing_args())
    path = ws / tools.SIZING_PATH
    model = json.loads(path.read_text(encoding="utf-8"))
    model["outputs"]["som"] *= 3
    model["bottom_up"]["units"]["claim"] = "C77"
    path.write_text(json.dumps(model), encoding="utf-8")
    res = checks.sizing_model_consistent(ws, {})
    assert res["passed"] is False
    assert "outputs.som" in res["details"] and "C77" in res["details"]


def test_sizing_model_needs_reconciliation_for_large_gap(ws):
    args = _sizing_args()
    args["bottom_up"]["adoption"] = {"name": "adoption", "value": 0.38, "claim": "C5"}
    tools.build_sizing_model(ws, **args)
    res = checks.sizing_model_consistent(ws, {"tolerance": 0.30})
    assert res["passed"] is False and "reconciliation" in res["details"]
    args["reconciliation"] = ("The top-down base counts spend by all HVAC firms including large ones, "
                              "while bottom-up counts only current adopters among small firms; we lean on bottom-up.")
    tools.build_sizing_model(ws, **args)
    assert checks.sizing_model_consistent(ws, {"tolerance": 0.30})["passed"] is True


def test_sizing_model_unreadable(ws):
    assert checks.sizing_model_consistent(ws, {})["passed"] is False
    (ws / tools.SIZING_PATH).parent.mkdir(parents=True)
    (ws / tools.SIZING_PATH).write_text(json.dumps({"top_down": {}}), encoding="utf-8")
    assert "cannot be recomputed" in checks.sizing_model_consistent(ws, {})["details"]


def _write_report(ws: Path, text: str = REPORT) -> None:
    path = ws / "deliverables/m3-report/report.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_report_answers_questions(ws):
    _evidence_ws(ws)
    _write_report(ws)
    res = checks.report_answers_questions(ws, {})
    assert res["passed"] is True and res["score"] == pytest.approx(2 / 3, abs=1e-4)


def test_report_answers_questions_negative(ws):
    _evidence_ws(ws)
    _write_report(ws, REPORT.replace("| Q2.1 | unresolved: only two vendors priced | |\n", "")
                  .replace("| [C5] |", "| [C1] |"))
    res = checks.report_answers_questions(ws, {})
    assert res["passed"] is False
    assert "Q2.1: no row" in res["details"] and "Q1.2: none of ['C1']" in res["details"]
    _write_report(ws, REPORT.replace("[C2] |", "[C12] |"))
    assert "C12" in checks.report_answers_questions(ws, {})["details"]
    _write_report(ws, "# Report\n\nNo matrix here.\n")
    assert "no 'Traceability matrix' section" in checks.report_answers_questions(ws, {})["details"]


def test_check_defs_signature(ws):
    assert set(checks.CHECK_DEFS) == {"question_tree_valid", "evidence_export_matches_ledger",
                                      "question_coverage", "source_tier_mix", "matrix_cells_cited",
                                      "sizing_model_consistent", "report_answers_questions"}
    empty = ws.parent / "empty"
    empty.mkdir()
    for fn in checks.CHECK_DEFS.values():
        res = fn(empty, {}, run=None)
        assert set(res) == {"passed", "details", "score"} and res["passed"] is False


def test_checks_keep_param_paths_inside_the_workspace(ws):
    _save_tree(ws)
    outside = ws.parent / "outside.json"
    outside.write_text(json.dumps(_tree()), encoding="utf-8")   # a valid tree outside the workspace
    for rel in ("../outside.json", str(outside)):
        for fn, params in [(checks.question_tree_valid, {"path": rel}),
                           (checks.evidence_export_matches_ledger, {"path": rel}),
                           (checks.question_coverage, {"questions": rel}),
                           (checks.matrix_cells_cited, {"path": rel}),
                           (checks.sizing_model_consistent, {"path": rel}),
                           (checks.report_answers_questions, {"path": rel})]:
            res = fn(ws, params)
            assert res["passed"] is False and "outside the workspace" in res["details"], (fn, rel)


# --- manifest --------------------------------------------------------------------

def test_agent_yaml_parses_and_references_known_checks_and_tools():
    import yaml

    manifest = yaml.safe_load((PKG / "agent.yaml").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1 and manifest["slug"] == "market-research"
    assert manifest["human_gate"]["required"] is False
    assert "Not investment" in manifest["human_gate"]["disclaimer"]
    tool_names = {d["name"] for d in tools.TOOL_DEFS}
    assert not set(manifest["tools"]) - KIT_TOOLS - tool_names
    assert tool_names <= set(manifest["tools"])
    assert [m["id"] for m in manifest["milestones"]] == ["m1-plan", "m2-evidence", "m3-report"]
    used = set()
    for m in manifest["milestones"]:
        assert all(d.startswith(f"deliverables/{m['id']}/") for d in m["deliverables"])
        for crit in m["acceptance"]:
            used.add(crit["check"])
            assert crit["check"] in KIT_BUILTIN_CHECKS | set(checks.CHECK_DEFS), crit["check"]
            if crit["check"] == "rubric_grader":
                assert (PKG / crit["params"]["rubric"]).is_file()
    assert set(checks.CHECK_DEFS) <= used
    for rel in [manifest["prompts"]["system"], *manifest["prompts"]["include"]]:
        assert (PKG / rel).is_file(), rel


def test_rubrics_are_well_formed():
    import yaml

    for path in (PKG / "rubrics").glob("*.yaml"):
        rubric = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert set(rubric) == {"name", "criteria", "threshold"}
        assert 0 < rubric["threshold"] <= 1
        assert sum(c["weight"] for c in rubric["criteria"]) == pytest.approx(1.0)
        assert len({c["id"] for c in rubric["criteria"]}) == len(rubric["criteria"])


def test_eval_cases_are_well_formed():
    import yaml

    manifest = yaml.safe_load((PKG / "agent.yaml").read_text(encoding="utf-8"))
    milestone_ids = {m["id"] for m in manifest["milestones"]}
    cases = sorted((PKG / "evals" / "cases").glob("*.json"))
    assert cases
    for path in cases:
        case = json.loads(path.read_text(encoding="utf-8"))
        assert set(case) == {"name", "brief", "milestone", "notes"}
        assert case["name"] == path.stem and case["milestone"] in milestone_ids
        assert case["brief"]["specialist"] == "market-research" and case["brief"]["objective"]
