"""
specialists/market_research/checks.py - acceptance checks for the
market-research specialist.

Each check is fn(workspace, params, *, run=None) -> {"passed", "details",
"score"} and is listed in CHECK_DEFS; agent.py registers every one as an
automated check. Checks never trust what the agent wrote about its own work:
they re-read the ledger and snapshots, recompute tiers, coverage and sizing
arithmetic, and compare against the deliverables. Paths in params are
resolved inside the workspace (params may come from a client's brief). From
m2 on, the question tree is read as the client approved it: a plan changed
after the m1-plan submission fails the check (tools.load_plan).
"""
from __future__ import annotations

import csv
import json
import math
import re
from pathlib import Path
from typing import Any

from agentkit.errors import PolicyViolation
from agentkit.policy import jail_path
from specialists.market_research import tools

# "insufficient evidence: what was tried" - the note must start with the
# phrase and say what was tried in at least three words.
_INSUFFICIENT = re.compile(r"^\s*insufficient evidence\s*:\s*(\S+(?:\s+\S+){2,})", re.IGNORECASE)
_UNRESOLVED = re.compile(r"\bunresolved\b", re.IGNORECASE)


def _result(passed: bool | None, details: str, score: float | None = None) -> dict[str, Any]:
    return {"passed": passed, "details": details, "score": score}


def _fail_list(problems: list[str], ok: str, limit: int = 10) -> dict[str, Any]:
    if not problems:
        return _result(True, ok)
    more = f" (+{len(problems) - limit} more)" if len(problems) > limit else ""
    return _result(False, "; ".join(problems[:limit]) + more)


def _file(workspace: Path, rel: Any) -> tuple[Path | None, str]:
    """A workspace file named in check params (which may come from a
    client's brief), jailed to the workspace like the kit's own checks."""
    try:
        path = jail_path(Path(workspace), rel)
    except PolicyViolation as exc:
        return None, str(exc)
    return path, "" if path.is_file() else f"{rel} not found"


def _read_json(workspace: Path, rel: str) -> tuple[Any, str]:
    path, err = _file(workspace, rel)
    if err:
        return None, err
    try:
        return json.loads(path.read_text(encoding="utf-8")), ""
    except ValueError as exc:
        return None, f"{rel} is not valid JSON: {exc}"


def _read_plan(workspace: Path, rel: str) -> tuple[Any, str]:
    """The approved question tree (jailed; fails if changed after m1)."""
    _, err = _file(workspace, rel)
    if err:
        return None, err
    return tools.load_plan(workspace, rel)


def _read_csv(workspace: Path, rel: str) -> tuple[list[dict[str, str]] | None, list[str], str]:
    path, err = _file(workspace, rel)
    if err:
        return None, [], err
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        return list(reader), list(reader.fieldnames or []), ""


def _share(params: dict, key: str, default: float) -> float:
    return float(params.get(key, default))


# --- m1-plan -------------------------------------------------------------------

def question_tree_valid(workspace: Path, params: dict, *, run=None) -> dict[str, Any]:
    """Every leaf has enough source types, including a primary one; ids
    unique; the source map names the sites evidence will come from."""
    rel = params.get("path", tools.QUESTIONS_PATH)
    tree, err = _read_json(workspace, rel)
    if err:
        return _result(False, err)
    problems = tools.question_tree_problems(tree, min_source_types=int(params.get("min_source_types", 2)),
                                            min_primary=int(params.get("min_primary", 1)))
    if not problems:
        problems = tools.source_domain_problems(tree.get("source_domains"),
                                                min_domains=int(params.get("min_source_domains", 1)))
    if problems:
        return _fail_list(problems, "")
    return _result(True, f"{len(tools.leaf_questions(tree))} leaf questions, all with a valid source plan; "
                         f"{len(tree['source_domains'])} site(s) in the source map")


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
    """Each leaf has claims from >= min_sources independent sources (distinct
    sites or client files) or an 'insufficient evidence: <what was tried>'
    note, and at most max_insufficient_share of the leaves rely on the note.
    The score is the share of leaves with enough evidence."""
    tree, err = _read_plan(workspace, params.get("questions", tools.QUESTIONS_PATH))
    if err:
        return _result(False, err)
    cmap, err = _read_json(workspace, params.get("claim_map", tools.CLAIM_MAP_PATH))
    if err:
        return _result(False, err)
    min_sources = int(params.get("min_sources", 2))
    max_insufficient = _share(params, "max_insufficient_share", 0.5)
    status = tools.verify_claims(workspace)
    claims = cmap.get("claims", {}) if isinstance(cmap, dict) else {}
    notes = cmap.get("notes", {}) if isinstance(cmap, dict) else {}
    claims = claims if isinstance(claims, dict) else {}
    notes = notes if isinstance(notes, dict) else {}
    leaves = tools.leaf_questions(tree if isinstance(tree, dict) else {})
    if not leaves:
        return _result(False, "question tree has no leaf questions")
    problems, evidenced, insufficient = [], 0, []
    for leaf in leaves:
        qid = leaf["id"]
        sources = {tools.source_key(status[cid]["source"]) for cid, qs in claims.items()
                   if isinstance(qs, list) and qid in qs and status.get(cid, {}).get("verified")}
        if len(sources) >= min_sources:
            evidenced += 1
        elif _INSUFFICIENT.match(str(notes.get(qid, ""))):
            insufficient.append(qid)
        else:
            problems.append(f"{qid}: verified claims from {len(sources)} independent source(s) "
                            f"(< {min_sources}) and no 'insufficient evidence: <what was tried>' note")
    if len(insufficient) > max_insufficient * len(leaves) + 1e-9:
        problems.append(f"{len(insufficient)}/{len(leaves)} leaf questions rest on an insufficient-evidence "
                        f"note {insufficient} (at most {max_insufficient:.0%} may)")
    return {**_fail_list(problems, f"{evidenced}/{len(leaves)} leaf questions evidenced by >= {min_sources} "
                                   f"independent sources; {len(insufficient)} marked insufficient"),
            "score": round(evidenced / len(leaves), 4)}


def source_tier_mix(workspace: Path, params: dict, *, run=None) -> dict[str, Any]:
    """Share of the external sources behind verified claims that recompute
    to tier 1 meets the threshold. Counted per source, not per claim, so
    extra claims from one page do not move it. The mix measures the
    external evidence, so client documents are reported but not counted."""
    threshold = _share(params, "min_tier1_share", 0.6)
    _, problem = tools.load_plan(workspace)
    if problem and (Path(workspace) / tools.QUESTIONS_PATH).is_file():
        return _result(False, problem, 0.0)
    status = tools.verify_claims(workspace)
    classify_one = tools.source_classifier(workspace)
    sources = {s["source"]["id"]: classify_one(s["source"]) for s in status.values() if s["verified"]}
    external = {sid: c for sid, c in sources.items() if c["source_type"] != "client"}
    client = len(sources) - len(external)
    if not external:
        return _result(False, f"no verified claims from external sources ({client} client document(s))", 0.0)
    tier1 = sorted(sid for sid, c in external.items() if c["tier"] == 1)
    share = len(tier1) / len(external)
    return _result(share + 1e-9 >= threshold,
                   f"{len(tier1)}/{len(external)} external sources behind verified claims are tier 1 "
                   f"({share:.0%}; threshold {threshold:.0%}); {client} client document(s) not counted",
                   round(share, 4))


def claims_not_from_instructions(workspace: Path, params: dict, *, run=None) -> dict[str, Any]:
    """No claim quotes text that gives instructions to the agent (a planted
    figure), and, with params.log, every source carrying such text is named
    (by source id) in that log."""
    if not tools.verify_claims(workspace):
        return _result(False, "the ledger has no claims")
    planted = tools.planted_claims(workspace)
    problems = [f"{cid}: {reason}" for cid, reason in planted.items()]
    flagged = tools.flagged_sources(workspace)
    log = params.get("log")
    if log and flagged:
        path, err = _file(workspace, log)
        text = "" if err else path.read_text(encoding="utf-8")
        missing = [sid for sid in flagged if not re.search(rf"\b{re.escape(sid)}\b", text)]
        if err:
            problems.append(err)
        elif missing:
            problems.append(f"{log} does not name the sources whose text gives instructions: "
                            + ", ".join(f"{sid} ({flagged[sid]!r})" for sid in missing))
    return _fail_list(problems, f"no claim quotes instruction-like text; {len(flagged)} source(s) with such "
                                f"text{' logged' if log and flagged else ''}")


# --- m3-report -----------------------------------------------------------------

def matrix_cells_cited(workspace: Path, params: dict, *, run=None) -> dict[str, Any]:
    """Every filled matrix cell cites a verified claim; price columns and
    cells with a currency amount carry a date."""
    rel = params.get("path", tools.MATRIX_PATH)
    rows, header, err = _read_csv(workspace, rel)
    if err:
        return _result(False, err)
    if not rows or len(header) < 2:
        return _result(False, f"{rel} has no competitors or no dimensions")
    status = tools.verify_claims(workspace)
    planted = tools.planted_claims(workspace)
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
                elif cid in planted:
                    problems.append(f"{name}/{dim}: {cid} is quoted from planted instructions")
            if tools.needs_date(dim, cell) and not tools._ISO_DATE.search(cell):
                problems.append(f"{name}/{dim}: price or currency amount without a date")
    if not filled:
        return _result(False, f"{rel} has no filled cells")
    return _fail_list(problems, f"{filled} filled cells, all cited and verified")


def _name_key(name: Any) -> str:
    return " ".join(re.sub(r"[^0-9a-z]+", " ", str(name or "").casefold()).split())


def matrix_covers_competitors(workspace: Path, params: dict, *, run=None) -> dict[str, Any]:
    """Every competitor the client named (params.required, filled from the
    intake by agent.py) has a matrix row with at least one filled cell."""
    rel = params.get("path", tools.MATRIX_PATH)
    rows, header, err = _read_csv(workspace, rel)
    if err:
        return _result(False, err)
    required = [r for r in (params.get("required") or []) if _name_key(r)]
    if not required:
        return _result(True, "the client named no competitors")
    covered = [_name_key(r.get(header[0], "")) for r in rows
               if any((r.get(d) or "").strip() for d in header[1:])]
    covered = [c for c in covered if c]
    missing = [r for r in required
               if not any(f" {_name_key(r)} " in f" {c} " or f" {c} " in f" {_name_key(r)} " for c in covered)]
    if missing:
        return _result(False, f"no filled matrix row for competitor(s) the client named: {missing}",
                       round(1 - len(missing) / len(required), 4))
    return _result(True, f"all {len(required)} competitors the client named are in the matrix", 1.0)


def sizing_model_consistent(workspace: Path, params: dict, *, run=None) -> dict[str, Any]:
    """Recompute the sizing model; required anchors cite claims, every cited
    number appears in its claim's quote, shares stay within [0, 1], and the
    two methods agree or the gap is reconciled."""
    rel = params.get("path", tools.SIZING_PATH)
    model, err = _read_json(workspace, rel)
    if err:
        return _result(False, err)
    tolerance = float(params.get("tolerance", 0.30))
    required = params.get("required_claims", list(tools.REQUIRED_CLAIM_INPUTS))
    try:
        recomputed = tools.compute_sizing(model)
        inputs = tools.sizing_inputs(model)
        sensitivity = tools.compute_sensitivity(model)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        return _result(False, f"{rel} cannot be recomputed: {exc}")
    problems = []
    reported = model.get("outputs") or {}
    for key, want in recomputed.items():
        got = reported.get(key)
        if isinstance(got, bool) or not isinstance(got, (int, float)) or not math.isclose(
                got, want, rel_tol=1e-6, abs_tol=1e-6):
            problems.append(f"outputs.{key} is {got!r}, recomputed {want!r}")
    table = model.get("sensitivity_table") or []
    try:
        same = len(table) == len(sensitivity) and all(
            math.isclose(float(a.get("tam", -1)), b["tam"], rel_tol=1e-6) and a.get("input") == b["input"]
            for a, b in zip(table, sensitivity))
    except (AttributeError, TypeError, ValueError):
        same = False
    if not same:
        problems.append("sensitivity_table does not match a recomputation")
    problems += tools.sizing_trace_problems(model, tools.verify_claims(workspace),
                                            tools.planted_claims(workspace), required)
    if recomputed["gap"] > tolerance and len(tools.normalize_ws(str(model.get("reconciliation", ""))).split()) < 15:
        problems.append(f"top-down and bottom-up TAM differ by {recomputed['gap']:.0%} "
                        f"(> {tolerance:.0%}) without a reconciliation note")
    traced = sum(1 for _, item in inputs if item.get("claim"))
    return {**_fail_list(problems, f"sizing recomputes; {traced}/{len(inputs)} inputs cite claims whose "
                                   f"quotes state them, method gap {recomputed['gap']:.0%}"),
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
    citations (mapped to that question when a claim map exists) or 'unresolved',
    and at most max_unresolved_share of the leaves are unresolved."""
    tree, err = _read_plan(workspace, params.get("questions", tools.QUESTIONS_PATH))
    if err:
        return _result(False, err)
    rel = params.get("path", "deliverables/m3-report/report.md")
    path, err = _file(workspace, rel)
    if err:
        return _result(False, err)
    section = _section(path.read_text(encoding="utf-8"), params.get("section", "Traceability matrix"))
    if not section.strip():
        return _result(False, f"{rel} has no '{params.get('section', 'Traceability matrix')}' section")
    cmap, _ = _read_json(workspace, params.get("claim_map", tools.CLAIM_MAP_PATH))
    mapped = (cmap or {}).get("claims", {}) if isinstance(cmap, dict) else {}
    mapped = mapped if isinstance(mapped, dict) else {}
    status = tools.verify_claims(workspace)
    planted = tools.planted_claims(workspace)
    rows: dict[str, str] = {}
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if line.strip().startswith("|") and cells and cells[0]:
            rows.setdefault(cells[0].strip("`* "), line)
    leaves = tools.leaf_questions(tree if isinstance(tree, dict) else {})
    if not leaves:
        return _result(False, "question tree has no leaf questions")
    problems, answered, unresolved = [], 0, []
    for leaf in leaves:
        qid = leaf["id"]
        line = rows.get(qid)
        if line is None:
            problems.append(f"{qid}: no row in the traceability matrix")
            continue
        cited = tools.citations_in(line)
        bad = [c for c in cited if not status.get(c, {}).get("verified") or c in planted]
        if bad:
            problems.append(f"{qid}: cites unverified, unknown or planted claims {bad}")
        elif cited:
            if mapped and not any(isinstance(mapped.get(c), list) and qid in mapped[c] for c in cited):
                problems.append(f"{qid}: none of {cited} is mapped to this question in the claim map")
            else:
                answered += 1
        elif _UNRESOLVED.search(line):
            unresolved.append(qid)
        else:
            problems.append(f"{qid}: neither cited nor marked unresolved")
    max_unresolved = _share(params, "max_unresolved_share", 0.5)
    if len(unresolved) > max_unresolved * len(leaves) + 1e-9:
        problems.append(f"{len(unresolved)}/{len(leaves)} leaf questions unresolved {unresolved} "
                        f"(at most {max_unresolved:.0%} may be)")
    return {**_fail_list(problems, f"all {len(leaves)} leaf questions traced ({answered} answered, "
                                   f"{len(unresolved)} unresolved)"),
            "score": round(answered / len(leaves), 4)}


CHECK_DEFS = {
    "question_tree_valid": question_tree_valid,
    "evidence_export_matches_ledger": evidence_export_matches_ledger,
    "question_coverage": question_coverage,
    "source_tier_mix": source_tier_mix,
    "claims_not_from_instructions": claims_not_from_instructions,
    "matrix_cells_cited": matrix_cells_cited,
    "matrix_covers_competitors": matrix_covers_competitors,
    "sizing_model_consistent": sizing_model_consistent,
    "report_answers_questions": report_answers_questions,
}
