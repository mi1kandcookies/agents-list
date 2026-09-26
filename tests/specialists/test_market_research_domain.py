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
    assert status["C1"] == {**status["C1"], "verified": False, "reason": "quote not found in snapshot"}
    assert status["C4"]["reason"] == "snapshot missing"
    assert status["C2"]["verified"] is True


def test_verify_claims_detects_tampered_snapshot(ws):
    ledger = json.loads((ws / tools.LEDGER_PATH).read_text(encoding="utf-8"))
    ledger["sources"][1]["sha256"] = tools.snapshot_sha256(ws, "S2")
    (ws / tools.LEDGER_PATH).write_text(json.dumps(ledger), encoding="utf-8")
    assert tools.verify_claims(ws)["C3"]["verified"] is True
    snap = ws / tools.SNAPSHOT_DIR / "S2.txt"
    snap.write_text(snap.read_text(encoding="utf-8") + "edited\n", encoding="utf-8")
    assert tools.verify_claims(ws)["C3"]["reason"] == "snapshot hash mismatch"


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
