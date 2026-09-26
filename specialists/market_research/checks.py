"""
specialists/market_research/checks.py - acceptance checks for the
market-research specialist.

Each check is fn(workspace, params, *, run=None) -> {"passed", "details",
"score"} and is listed in CHECK_DEFS. Checks never trust what the agent wrote
about its own work: they re-read the ledger and snapshots, recompute tiers,
coverage and sizing arithmetic, and compare against the deliverables.
"""
from __future__ import annotations

import csv
import json
import math
import re
from pathlib import Path
from typing import Any

from specialists.market_research import tools

_INSUFFICIENT = re.compile(r"insufficient evidence", re.IGNORECASE)
_UNRESOLVED = re.compile(r"\bunresolved\b", re.IGNORECASE)


def _result(passed: bool | None, details: str, score: float | None = None) -> dict[str, Any]:
    return {"passed": passed, "details": details, "score": score}


def _fail_list(problems: list[str], ok: str, limit: int = 10) -> dict[str, Any]:
    if not problems:
        return _result(True, ok)
    more = f" (+{len(problems) - limit} more)" if len(problems) > limit else ""
    return _result(False, "; ".join(problems[:limit]) + more)


def _read_json(workspace: Path, rel: str) -> tuple[Any, str]:
    path = Path(workspace) / rel
    if not path.is_file():
        return None, f"{rel} not found"
    try:
        return json.loads(path.read_text(encoding="utf-8")), ""
    except ValueError as exc:
        return None, f"{rel} is not valid JSON: {exc}"


def _read_csv(workspace: Path, rel: str) -> tuple[list[dict[str, str]] | None, list[str], str]:
    path = Path(workspace) / rel
    if not path.is_file():
        return None, [], f"{rel} not found"
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        return list(reader), list(reader.fieldnames or []), ""


# --- m1-plan -------------------------------------------------------------------

def question_tree_valid(workspace: Path, params: dict, *, run=None) -> dict[str, Any]:
    """Every leaf has enough source types, including a primary one; ids unique."""
    rel = params.get("path", tools.QUESTIONS_PATH)
    tree, err = _read_json(workspace, rel)
    if err:
        return _result(False, err)
    problems = tools.question_tree_problems(tree, min_source_types=int(params.get("min_source_types", 2)),
                                            min_primary=int(params.get("min_primary", 1)))
    leaves = len(tools.leaf_questions(tree)) if not problems else 0
    return _fail_list(problems, f"{leaves} leaf questions, all with a valid source plan")


# --- m2-evidence ---------------------------------------------------------------

def evidence_export_matches_ledger(workspace: Path, params: dict, *, run=None) -> dict[str, Any]:
    """claims.csv equals what the ledger and snapshots imply, and every claim verifies."""
    rel = params.get("path", tools.CLAIMS_CSV_PATH)
    rows, _, err = _read_csv(workspace, rel)
    if err:
        return _result(False, err)
    expected = {r["claim_id"]: r for r in tools.expected_evidence_rows(workspace)}
    if not expected:
        return _result(False, "the ledger has no claims")
    problems = []
    got = {}
    for row in rows:
        cid = row.get("claim_id", "")
        if cid in got:
            problems.append(f"{cid}: duplicate row")
        got[cid] = row
    for cid in sorted(set(got) - set(expected)):
        problems.append(f"{cid}: in {rel} but not in the ledger")
    for cid in sorted(set(expected) - set(got)):
        problems.append(f"{cid}: in the ledger but missing from {rel}")
    for cid in sorted(set(got) & set(expected)):
        for col, want in expected[cid].items():
            if (got[cid].get(col) or "") != want:
                problems.append(f"{cid}: {col} is {got[cid].get(col)!r}, ledger says {want!r}")
    unverified = [cid for cid, r in expected.items() if r["verified"] != "true"]
    problems += [f"{cid}: quote does not verify against its snapshot" for cid in unverified]
    return _fail_list(problems, f"{len(expected)} claims exported; all quotes verify")


def question_coverage(workspace: Path, params: dict, *, run=None) -> dict[str, Any]:
    """Each leaf has >= min_claims verified mapped claims or an insufficient-evidence note."""
    tree, err = _read_json(workspace, params.get("questions", tools.QUESTIONS_PATH))
    if err:
        return _result(False, err)
    cmap, err = _read_json(workspace, params.get("claim_map", tools.CLAIM_MAP_PATH))
    if err:
        return _result(False, err)
    min_claims = int(params.get("min_claims", 3))
    status = tools.verify_claims(workspace)
    claims = cmap.get("claims", {}) if isinstance(cmap, dict) else {}
    notes = cmap.get("notes", {}) if isinstance(cmap, dict) else {}
    leaves = tools.leaf_questions(tree or {})
    if not leaves:
        return _result(False, "question tree has no leaf questions")
    problems, covered = [], 0
    for leaf in leaves:
        qid = leaf["id"]
        n = sum(1 for cid, qs in claims.items() if qid in qs and status.get(cid, {}).get("verified"))
        if n >= min_claims:
            covered += 1
        elif _INSUFFICIENT.search(str(notes.get(qid, ""))):
            covered += 1
        else:
            problems.append(f"{qid}: {n} verified claims (< {min_claims}) and no insufficient-evidence note")
    return {**_fail_list(problems, f"all {len(leaves)} leaf questions covered"),
            "score": round(covered / len(leaves), 4)}


def source_tier_mix(workspace: Path, params: dict, *, run=None) -> dict[str, Any]:
    """Share of verified claims whose source recomputes to tier 1 meets the threshold."""
    threshold = float(params.get("min_tier1_share", 0.6))
    ledger = tools.load_ledger(workspace)
    status = tools.verify_claims(workspace, ledger)
    verified = [cid for cid, s in status.items() if s["verified"]]
    if not verified:
        return _result(False, "no verified claims in the ledger", 0.0)
    tier1 = sum(1 for cid in verified if tools.source_classification(workspace, status[cid]["source"])["tier"] == 1)
    share = tier1 / len(verified)
    return _result(share + 1e-9 >= threshold,
                   f"{tier1}/{len(verified)} verified claims from tier-1 sources ({share:.0%}; "
                   f"threshold {threshold:.0%})", round(share, 4))


# --- m3-report -----------------------------------------------------------------

def matrix_cells_cited(workspace: Path, params: dict, *, run=None) -> dict[str, Any]:
    """Every filled matrix cell cites a verified claim; pricing cells carry a date."""
    rel = params.get("path", tools.MATRIX_PATH)
    rows, header, err = _read_csv(workspace, rel)
    if err:
        return _result(False, err)
    if not rows or len(header) < 2:
        return _result(False, f"{rel} has no competitors or no dimensions")
    status = tools.verify_claims(workspace)
    problems, filled = [], 0
    for row in rows:
        name = row.get(header[0], "")
        for dim in header[1:]:
            cell = (row.get(dim) or "").strip()
            if not cell:
                continue
            filled += 1
            cited = tools.citations_in(cell)
            if not cited:
                problems.append(f"{name}/{dim}: no [C#] citation")
            for cid in cited:
                if not status.get(cid, {}).get("verified"):
                    problems.append(f"{name}/{dim}: {cid} is not a verified ledger claim")
            if tools.is_pricing_dimension(dim) and not tools._ISO_DATE.search(cell):
                problems.append(f"{name}/{dim}: pricing cell without a date")
    if not filled:
        return _result(False, f"{rel} has no filled cells")
    return _fail_list(problems, f"{filled} filled cells, all cited and verified")


def sizing_model_consistent(workspace: Path, params: dict, *, run=None) -> dict[str, Any]:
    """Recompute the sizing model; inputs traced; methods agree or are reconciled."""
    rel = params.get("path", tools.SIZING_PATH)
    model, err = _read_json(workspace, rel)
    if err:
        return _result(False, err)
    tolerance = float(params.get("tolerance", 0.30))
    try:
        recomputed = tools.compute_sizing(model)
        inputs = tools.sizing_inputs(model)
        sensitivity = tools.compute_sensitivity(model)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        return _result(False, f"{rel} cannot be recomputed: {exc}")
    status = tools.verify_claims(workspace)
    problems = []
    reported = model.get("outputs") or {}
    for key, want in recomputed.items():
        got = reported.get(key)
        if not isinstance(got, (int, float)) or not math.isclose(got, want, rel_tol=1e-6, abs_tol=1e-6):
            problems.append(f"outputs.{key} is {got!r}, recomputed {want!r}")
    table = model.get("sensitivity_table") or []
    if len(table) != len(sensitivity) or any(
            not math.isclose(float(a.get("tam", -1)), b["tam"], rel_tol=1e-6) or a.get("input") != b["input"]
            for a, b in zip(table, sensitivity)):
        problems.append("sensitivity_table does not match a recomputation")
    for label, item in inputs:
        claim = str(item.get("claim", ""))
        if claim:
            if not status.get(claim, {}).get("verified"):
                problems.append(f"{label}: {claim} is not a verified ledger claim")
        elif not tools.normalize_ws(str(item.get("assumption", ""))):
            problems.append(f"{label}: neither a claim nor an assumption")
    if recomputed["gap"] > tolerance and len(tools.normalize_ws(str(model.get("reconciliation", ""))).split()) < 15:
        problems.append(f"top-down and bottom-up TAM differ by {recomputed['gap']:.0%} "
                        f"(> {tolerance:.0%}) without a reconciliation note")
    traced = sum(1 for _, item in inputs if item.get("claim"))
    return {**_fail_list(problems, f"sizing recomputes; {traced}/{len(inputs)} inputs cite claims, "
                                   f"method gap {recomputed['gap']:.0%}"),
            "score": round(traced / len(inputs), 4) if inputs else None}


def _section(markdown: str, heading: str) -> str:
    """Body of the first '#'-heading whose text matches `heading` (case-insensitive)."""
    lines = markdown.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"^(#+)\s+(.*?)\s*#*\s*$", line)
        if m and m.group(2).strip().lower() == heading.lower():
            level = len(m.group(1))
            body = []
            for nxt in lines[i + 1:]:
                n = re.match(r"^(#+)\s", nxt)
                if n and len(n.group(1)) <= level:
                    break
                body.append(nxt)
            return "\n".join(body)
    return ""


def report_answers_questions(workspace: Path, params: dict, *, run=None) -> dict[str, Any]:
    """The report's traceability matrix has a row per leaf question with verified
    citations (mapped to that question when a claim map exists) or 'unresolved'."""
    tree, err = _read_json(workspace, params.get("questions", tools.QUESTIONS_PATH))
    if err:
        return _result(False, err)
    rel = params.get("path", "deliverables/m3-report/report.md")
    path = Path(workspace) / rel
    if not path.is_file():
        return _result(False, f"{rel} not found")
    section = _section(path.read_text(encoding="utf-8"), params.get("section", "Traceability matrix"))
    if not section.strip():
        return _result(False, f"{rel} has no '{params.get('section', 'Traceability matrix')}' section")
    cmap, _ = _read_json(workspace, params.get("claim_map", tools.CLAIM_MAP_PATH))
    mapped = (cmap or {}).get("claims", {}) if isinstance(cmap, dict) else {}
    status = tools.verify_claims(workspace)
    rows: dict[str, str] = {}
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if line.strip().startswith("|") and cells and cells[0]:
            rows.setdefault(cells[0].strip("`* "), line)
    leaves = tools.leaf_questions(tree or {})
    problems, answered = [], 0
    for leaf in leaves:
        qid = leaf["id"]
        line = rows.get(qid)
        if line is None:
            problems.append(f"{qid}: no row in the traceability matrix")
            continue
        cited = tools.citations_in(line)
        bad = [c for c in cited if not status.get(c, {}).get("verified")]
        if bad:
            problems.append(f"{qid}: cites unverified or unknown claims {bad}")
        elif cited:
            if mapped and not any(qid in mapped.get(c, []) for c in cited):
                problems.append(f"{qid}: none of {cited} is mapped to this question in the claim map")
            else:
                answered += 1
        elif not _UNRESOLVED.search(line):
            problems.append(f"{qid}: neither cited nor marked unresolved")
    if not leaves:
        return _result(False, "question tree has no leaf questions")
    return {**_fail_list(problems, f"all {len(leaves)} leaf questions traced ({answered} answered)"),
            "score": round(answered / len(leaves), 4)}


CHECK_DEFS = {
    "question_tree_valid": question_tree_valid,
    "evidence_export_matches_ledger": evidence_export_matches_ledger,
    "question_coverage": question_coverage,
    "source_tier_mix": source_tier_mix,
    "matrix_cells_cited": matrix_cells_cited,
    "sizing_model_consistent": sizing_model_consistent,
    "report_answers_questions": report_answers_questions,
}
