"""
specialists/contract_review/checks.py - acceptance checks for the
contract-review specialist.

Each check is fn(workspace, params, *, run=None) -> {"passed", "details",
"score"} and is listed in CHECK_DEFS; agent.py wraps them as kit checks.
They never trust what the agent says it did: quotes are re-matched against
the contract under inputs/ (whose sha256 must match the one recorded),
coverage is recomputed from the issues themselves, and the redline is
re-applied from the ops so the .docx and proposed text must agree with it.
"""
from __future__ import annotations

import csv
import io
import re
from pathlib import Path
from typing import Any, Callable

from agentkit.errors import ToolError
from specialists.contract_review import tools as t


def _result(passed: bool | None, details: str, score: float | None = None) -> dict[str, Any]:
    return {"passed": passed, "details": details, "score": score}


def _guard(fn: Callable) -> Callable:
    """A missing or malformed deliverable is a failed check, not a crash."""
    def wrapper(workspace: Path, params: dict, *, run=None) -> dict[str, Any]:
        try:
            return fn(Path(workspace), dict(params or {}))
        except (ToolError, KeyError, TypeError, ValueError, AttributeError) as exc:
            return _result(False, f"{type(exc).__name__}: {exc}", 0.0)
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


def _summary(problems: list[str], limit: int = 12) -> str:
    extra = f"\n... and {len(problems) - limit} more" if len(problems) > limit else ""
    return "\n".join(problems[:limit]) + extra


def _issue_doc(workspace: Path, path: str) -> tuple[dict, list[str]]:
    """The issue list plus the original contract paragraphs, after checking
    the contract is a customer input whose hash matches the recorded one."""
    doc = t.load_structured(t.resolve(workspace, path))
    if not isinstance(doc, dict):
        raise ValueError(f"{path} is not a JSON object")
    contract = str(doc.get("contract", ""))
    if not contract.startswith("inputs/"):
        raise ValueError("issue list must name a contract under inputs/")
    src = t.resolve(workspace, contract)
    if doc.get("contract_sha256") != t.sha256_file(src):
        raise ValueError(f"{contract} changed since the issue list was recorded (sha256 mismatch)")
    return doc, t.load_paragraphs(src)


@_guard
def playbook_schema_valid(workspace: Path, params: dict) -> dict[str, Any]:
    """params: path, required_families (list, optional), min_families (default 12)."""
    data = t.load_structured(t.resolve(workspace, params["path"]))
    problems = t.playbook_errors(data, params.get("required_families"))
    count = len(t.playbook_families(data)) if isinstance(data, dict) else 0
    minimum = int(params.get("min_families", 12))
    if count < minimum:
        problems.append(f"{count} clause families; at least {minimum} required")
    if problems:
        return _result(False, _summary(problems), 0.0)
    return _result(True, f"{count} clause families, each with preferred, fallback and walk-away "
                         "positions", 1.0)


@_guard
def quotes_in_contract(workspace: Path, params: dict) -> dict[str, Any]:
    """params: issues (path to issues.json). Every issue quote and every
    'compliant' coverage quote must appear verbatim in the contract."""
    doc, paragraphs = _issue_doc(workspace, params["issues"])
    quotes = [(f"issue {i.get('id')}", i.get("quote")) for i in doc.get("issues", [])]
    quotes += [(f"coverage {c.get('family')}", c.get("quote")) for c in doc.get("coverage", [])
               if c.get("status") == "compliant"]
    if not quotes:
        return _result(False, "no quotes to verify", 0.0)
    bad = [label for label, q in quotes
           if not isinstance(q, str) or len(t.normalize(q)) < 12 or not t.locate(paragraphs, q)]
    score = (len(quotes) - len(bad)) / len(quotes)
    if bad:
        return _result(False, f"{len(bad)} of {len(quotes)} quotes not found verbatim: "
                              + ", ".join(bad[:12]), score)
    return _result(True, f"all {len(quotes)} quotes found verbatim in {doc['contract']}", 1.0)


@_guard
def playbook_coverage(workspace: Path, params: dict) -> dict[str, Any]:
    """params: issues (path), playbook (path; defaults to the one the issue
    list names, but the manifest pins it so a thinner playbook cannot be
    swapped in). Every family must be addressed and consistent with the issues."""
    doc = t.load_structured(t.resolve(workspace, params["issues"]))
    pb_path = params.get("playbook") or doc.get("playbook", "")
    families = t.playbook_families(t.load_structured(t.resolve(workspace, pb_path)))
    if not families:
        return _result(False, f"playbook {pb_path} has no families", 0.0)
    issues = [i for i in doc.get("issues", []) if isinstance(i, dict)]
    raised: dict[str, list[str]] = {}
    for issue in issues:
        raised.setdefault(issue.get("family"), []).append(str(issue.get("id")))
    rows = {r.get("family"): r for r in doc.get("coverage", []) if isinstance(r, dict)}
    problems, ok = [], 0
    for fid in families:
        row = rows.get(fid)
        status = (row or {}).get("status")
        if row is None:
            problems.append(f"{fid}: not addressed")
        elif status not in t.COVERAGE_STATUSES:
            problems.append(f"{fid}: invalid status {status!r}")
        elif (status == "deviation") != bool(raised.get(fid)):
            problems.append(f"{fid}: status {status} disagrees with issues {raised.get(fid, [])}")
        elif status in ("absent", "not_applicable") and not str(row.get("note") or "").strip():
            problems.append(f"{fid}: {status} without a note")
        else:
            ok += 1
    unknown = sorted(str(f) for f in raised if f not in families)
    problems += [f"issue family {f} is not in the playbook" for f in unknown]
    for issue in issues:
        if issue.get("severity") not in t.SEVERITIES:
            problems.append(f"issue {issue.get('id')}: invalid severity")
        elif issue.get("severity") == "critical" and not issue.get("escalate"):
            problems.append(f"issue {issue.get('id')}: critical but not escalated")
    score = ok / len(families)
    if problems:
        return _result(False, _summary(problems), score)
    return _result(True, f"all {len(families)} playbook families addressed", 1.0)


@_guard
def issue_list_valid(workspace: Path, params: dict) -> dict[str, Any]:
    """params: issues (path). The full record_issues validation, re-run."""
    doc = t.load_structured(t.resolve(workspace, params["issues"]))
    if params.get("playbook") and isinstance(doc, dict):
        doc = {**doc, "playbook": params["playbook"]}
    problems = t.issue_list_errors(workspace, doc)
    if problems:
        return _result(False, _summary(problems), 0.0)
    return _result(True, f"{len(doc['issues'])} issues valid", 1.0)


@_guard
def redline_roundtrip(workspace: Path, params: dict) -> dict[str, Any]:
    """params: redline (path to redline.json), issues (optional path).
    Re-applies the ops to the original contract and requires: each target
    matched exactly once; proposed.txt equals the result; in the .docx,
    reject-all equals the original and accept-all equals the proposal."""
    rec = t.load_structured(t.resolve(workspace, params["redline"]))
    contract = str(rec.get("contract", ""))
    if not contract.startswith("inputs/"):
        return _result(False, "redline must name a contract under inputs/", 0.0)
    src = t.resolve(workspace, contract)
    if rec.get("contract_sha256") != t.sha256_file(src):
        return _result(False, f"{contract} does not match the recorded sha256", 0.0)
    original = t.load_paragraphs(src)
    ops = rec.get("ops") or []
    plan, problems = t.redline_plan(original, ops)
    if not ops:
        problems.append("no redline ops")
    _orig, proposed = t.plan_views(plan)
    prop_path = t.resolve(workspace, rec.get("proposed", ""))
    if not prop_path.is_file():
        problems.append("proposed text file is missing")
    elif prop_path.read_text(encoding="utf-8") != "\n".join(proposed):
        problems.append("proposed.txt does not equal the original with the ops applied")
    docx = t.resolve(workspace, rec.get("docx", ""))
    if not docx.is_file():
        problems.append("redline .docx is missing")
    else:
        if t.docx_paragraphs(docx, "original") != original:
            problems.append("rejecting all changes in the .docx does not reproduce the original")
        if t.docx_paragraphs(docx, "accepted") != proposed:
            problems.append("accepting all changes in the .docx does not give the proposed text")
    issues_path = params.get("issues") or rec.get("issues")
    if issues_path:
        known = {str(i.get("id")) for i in
                 t.load_structured(t.resolve(workspace, issues_path)).get("issues", [])}
        problems += [f"ops[{k}] cites unknown issue {op.get('issue_id')!r}"
                     for k, op in enumerate(ops) if str(op.get("issue_id")) not in known]
    if problems:
        return _result(False, _summary(problems), 0.0)
    return _result(True, f"{len(ops)} ops re-applied; .docx reject-all = original, "
                         "accept-all = proposed", 1.0)


@_guard
def references_resolve(workspace: Path, params: dict) -> dict[str, Any]:
    """params: redline (path). The redline must not break a cross-reference
    or remove a definition whose term is still used. Breaks already in the
    counterparty's paper are reported but do not fail the check."""
    rec = t.load_structured(t.resolve(workspace, params["redline"]))
    original = t.load_paragraphs(t.resolve(workspace, rec["contract"]))
    proposed = t.resolve(workspace, rec["proposed"]).read_text(encoding="utf-8").split("\n")
    before, after = t.reference_report(original), t.reference_report(proposed)
    old_refs = {r["reference"] for r in before["unresolved_references"]}
    new_refs = sorted({r["reference"] for r in after["unresolved_references"]} - old_refs)
    body = "\n".join(proposed)
    lost = sorted(term for term in set(before["defined_terms"]) - set(after["defined_terms"])
                  if term in body)
    problems = [f"redline breaks cross-reference {r}" for r in new_refs]
    problems += [f"redline removes the definition of {term!r}, which is still used" for term in lost]
    if problems:
        return _result(False, _summary(problems), 0.0)
    note = f" (pre-existing: {', '.join(sorted(old_refs))})" if old_refs else ""
    return _result(True, "no cross-reference or definition broken by the redline" + note, 1.0)


@_guard
def csv_formula_safe(workspace: Path, params: dict) -> dict[str, Any]:
    """params: path. No cell may start with = + - @ tab or CR."""
    text = t.resolve(workspace, params["path"]).read_text(encoding="utf-8")
    bad = [f"row {r} col {c}" for r, row in enumerate(csv.reader(io.StringIO(text)), 1)
           for c, cell in enumerate(row, 1) if cell.startswith(t.CSV_DANGEROUS)]
    if bad:
        return _result(False, "formula-injectable cells: " + ", ".join(bad[:12]), 0.0)
    return _result(True, "no formula-injectable cells", 1.0)


@_guard
def memo_covers_issues(workspace: Path, params: dict) -> dict[str, Any]:
    """params: memo (path), issues (path), severities (default critical, high).
    The memo must name every issue at those severities and every escalation."""
    memo = t.resolve(workspace, params["memo"]).read_text(encoding="utf-8")
    doc = t.load_structured(t.resolve(workspace, params["issues"]))
    wanted = set(params.get("severities") or ["critical", "high"])
    need = [str(i["id"]) for i in doc.get("issues", [])
            if i.get("severity") in wanted or i.get("escalate")]
    missing = [iid for iid in need if not re.search(rf"(?<![\w-]){re.escape(iid)}(?![\w-])", memo)]
    score = (len(need) - len(missing)) / len(need) if need else 1.0
    if missing:
        return _result(False, "memo does not mention issues: " + ", ".join(missing), score)
    return _result(True, f"memo covers all {len(need)} priority issues", 1.0)


@_guard
def hidden_content_disclosed(workspace: Path, params: dict) -> dict[str, Any]:
    """params: issues (path), report (path). Re-scans the contract; if it
    carries hidden content or embedded instructions, the report must have a
    'Hidden content' heading naming each kind of finding."""
    doc, _paragraphs = _issue_doc(workspace, params["issues"])
    scan = t.scan_hidden_content(workspace, path=doc["contract"])
    kinds = sorted({f["kind"] for f in scan["findings"]})
    if not kinds:
        return _result(True, "no hidden content in the contract", 1.0)
    report = t.resolve(workspace, params["report"]).read_text(encoding="utf-8")
    lower = report.lower()
    if "hidden content" not in lower:
        return _result(False, f"contract has {', '.join(kinds)} but the report has no "
                              "'Hidden content' section", 0.0)
    missing = [k for k in kinds if k.replace("_", " ") not in lower and k not in lower]
    if missing:
        return _result(False, "hidden-content kinds not disclosed: " + ", ".join(missing),
                       1 - len(missing) / len(kinds))
    return _result(True, f"disclosed {', '.join(kinds)}", 1.0)


CHECK_DEFS: dict[str, Callable[..., dict[str, Any]]] = {
    "playbook_schema_valid": playbook_schema_valid,
    "quotes_in_contract": quotes_in_contract,
    "playbook_coverage": playbook_coverage,
    "issue_list_valid": issue_list_valid,
    "redline_roundtrip": redline_roundtrip,
    "references_resolve": references_resolve,
    "csv_formula_safe": csv_formula_safe,
    "memo_covers_issues": memo_covers_issues,
    "hidden_content_disclosed": hidden_content_disclosed,
}
