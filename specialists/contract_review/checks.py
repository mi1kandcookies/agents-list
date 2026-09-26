"""
specialists/contract_review/checks.py - acceptance checks for the
contract-review specialist.

Each check is fn(workspace, params, *, run=None) -> {"passed", "details",
"score"} and is listed in CHECK_DEFS; agent.py wraps them as kit checks.
They never trust what the agent says it did:

- the reviewed contract must be the one the brief's intake names (pinned
  under .agentkit/ by the harness before the run, or given as a
  "contracts" param), resolved under inputs/, with the recorded sha256;
- quotes are re-matched against it, and coverage is recomputed from the
  issues, the approved playbook and the contract's wording
  (tools.coverage_errors);
- the attorney's Markdown/CSV views must be the rendering of the recorded
  issue list;
- the redline is re-applied from its ops against the same contract, hash
  and base as the issue list, so the .docx (text and margin comments),
  redline.md and proposed text must agree with it;
- files approved in an earlier milestone (the playbook, the issue list)
  must be byte-for-byte what was approved.

The manifest pins every deliverable path, so a record cannot point a check
at a different file than the one delivered.
"""
from __future__ import annotations

import csv
import io
import re
from pathlib import Path
from typing import Any, Callable

from agentkit.checks.builtin import headings
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


def _contracts(workspace: Path, params: dict) -> list[str]:
    """The engagement's contract paths: params["contracts"], else what the
    harness pinned from the intake. Fails closed when neither exists."""
    given = params.get("contracts")
    if given is not None:
        return [str(c) for c in ([given] if isinstance(given, str) else given)]
    pinned = t.engagement_contracts(workspace)
    if pinned is None:
        raise ValueError("no contract is pinned for this engagement: run the milestone through the "
                         "harness (it records the intake's contract_files) or pass 'contracts'")
    return pinned


def _load_dict(workspace: Path, path: str) -> dict:
    doc = t.load_structured(t.resolve(workspace, path))
    if not isinstance(doc, dict):
        raise ValueError(f"{path} is not a JSON object")
    return doc


def _issue_doc(workspace: Path, path: str, params: dict) -> tuple[dict, list[str]]:
    """The issue list plus the contract paragraphs it was reviewed on, after
    checking the contract is the engagement's, its hash matches the
    recorded one, and its base is valid."""
    doc = _load_dict(workspace, path)
    contract = str(doc.get("contract", ""))
    src = t.input_file(workspace, contract)
    error = t.contract_error(workspace, src, _contracts(workspace, params))
    if error:
        raise ValueError(error)
    if doc.get("contract_sha256") != t.sha256_file(src):
        raise ValueError(f"{contract} changed since the issue list was recorded (sha256 mismatch)")
    base, error = t.contract_base(src, doc.get("base"))
    if error:
        raise ValueError(error)
    return doc, t.load_paragraphs(src, base)


def _families(workspace: Path, path: str) -> dict[str, dict]:
    data = t.load_structured(t.resolve(workspace, path))
    return t.playbook_families(data if isinstance(data, dict) else {})


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
    """params: issues (path to issues.json), contracts (optional). Every
    issue quote and every 'compliant' coverage quote must appear verbatim in
    the engagement's contract."""
    doc, paragraphs = _issue_doc(workspace, params["issues"], params)
    quotes = [(f"issue {i.get('id')}", i.get("quote")) for i in doc.get("issues", [])]
    quotes += [(f"coverage {c.get('family')}", c.get("quote")) for c in doc.get("coverage", [])
               if c.get("status") == "compliant"]
    if not quotes:
        return _result(False, "no quotes to verify", 0.0)
    bad = [label for label, q in quotes
           if not isinstance(q, str) or len(t.normalize(q)) < t.MIN_QUOTE or not t.locate(paragraphs, q)]
    score = (len(quotes) - len(bad)) / len(quotes)
    if bad:
        return _result(False, f"{len(bad)} of {len(quotes)} quotes not found verbatim: "
                              + ", ".join(bad[:12]), score)
    return _result(True, f"all {len(quotes)} quotes found verbatim in {doc['contract']}", 1.0)


@_guard
def playbook_coverage(workspace: Path, params: dict) -> dict[str, Any]:
    """params: issues (path), playbook (path; defaults to the one the issue
    list names, but the manifest pins it so a thinner playbook cannot be
    swapped in), contracts (optional). Every family must be addressed, and
    each row must stand up against the issues, the playbook's severities and
    the contract's wording (tools.coverage_errors)."""
    doc, paragraphs = _issue_doc(workspace, params["issues"], params)
    pb_path = params.get("playbook") or doc.get("playbook", "")
    fams = _families(workspace, pb_path)
    if not fams:
        return _result(False, f"playbook {pb_path} has no families", 0.0)
    instruction = t.carries_instruction(workspace, doc["contract"])
    problems = t.coverage_errors(doc, paragraphs, fams, instruction=instruction)
    bad = {fam for fam, _msg in problems if fam in fams}
    score = (len(fams) - len(bad)) / len(fams)
    if problems:
        return _result(False, _summary([msg for _fam, msg in problems]), score)
    return _result(True, f"all {len(fams)} playbook families addressed consistently", 1.0)


@_guard
def issue_list_valid(workspace: Path, params: dict) -> dict[str, Any]:
    """params: issues (path), playbook (optional, pins it), markdown and csv
    (optional paths), contracts (optional). The full record_issues
    validation, re-run; issues.md and issues.csv must be exactly what
    record_issues renders from issues.json, so the attorney's view cannot
    drift from the checked record."""
    recorded = _load_dict(workspace, params["issues"])
    doc = {**recorded, "playbook": params["playbook"]} if params.get("playbook") else recorded
    problems = t.issue_list_errors(workspace, doc, contracts=_contracts(workspace, params))
    if not problems:
        for key, render in (("markdown", t.render_issues_md), ("csv", t.render_issues_csv)):
            if params.get(key):
                view = t.resolve(workspace, params[key])
                rendered = render(recorded)
                if not view.is_file() or view.read_text(encoding="utf-8") != rendered:
                    problems.append(f"{params[key]} is not the rendering of {params['issues']}; "
                                    "call record_issues again instead of editing either file")
    if problems:
        return _result(False, _summary(problems), 0.0)
    return _result(True, f"{len(doc['issues'])} issues valid", 1.0)


def _pinned(params: dict, rec: dict, key: str) -> str:
    """A deliverable path: the manifest's (params) when given, else the record's."""
    return str(params.get(key) or rec.get(key) or "")


@_guard
def redline_roundtrip(workspace: Path, params: dict) -> dict[str, Any]:
    """params: redline (path to redline.json), issues (path; default the
    record's), contracts (optional) and the delivered proposed / docx /
    markdown paths (default: the record's). Re-applies the ops to the
    contract the issue list reviewed (same file, sha256 and base) and
    requires: each target matched exactly once; every op cites a recorded
    issue, and one that edits another section than its issue's quote
    carries a comment; no placeholder added; proposed.txt equals the result;
    in the .docx, reject-all equals the original, accept-all equals the
    proposal and the margin comments are exactly the ops' comments;
    redline.md is the rendering of the ops."""
    rec = _load_dict(workspace, params["redline"])
    try:
        src = t.input_file(workspace, str(rec.get("contract", "")))
    except ToolError as exc:
        return _result(False, f"redline: {exc}", 0.0)
    contract = str(rec["contract"])
    if rec.get("contract_sha256") != t.sha256_file(src):
        return _result(False, f"{contract} does not match the recorded sha256", 0.0)
    issues_path = params.get("issues") or rec.get("issues")
    if not issues_path:
        return _result(False, "the redline names no issue list to tie its ops to", 0.0)
    issue_doc = _load_dict(workspace, str(issues_path))
    base, problems = t.redline_basis(workspace, src, issue_doc, rec.get("base"),
                                     _contracts(workspace, params))
    if rec.get("base") != base:
        problems.append(f"redline.json records base {rec.get('base')!r}; the contract is read as {base!r}")
    original = t.load_paragraphs(src, base)
    ops = rec.get("ops") or []
    if not isinstance(ops, list) or not all(isinstance(op, dict) for op in ops):
        return _result(False, "redline ops must be a list of objects", 0.0)
    plan, errors = t.redline_plan(original, ops)
    problems += errors
    if not ops:
        problems.append("no redline ops")
    recorded = [i for i in issue_doc.get("issues") or [] if isinstance(i, dict)]
    known = {str(i.get("id")) for i in recorded}
    problems += [f"ops[{k}] cites unknown issue {op.get('issue_id')!r}"
                 for k, op in enumerate(ops) if str(op.get("issue_id")) not in known]
    cross = t.cross_section_ops(original, ops, recorded)
    problems += [f"ops[{c['op']}] changes \u00a7{c['section']}, outside the section its issue "
                 f"{c['issue_id']} quotes, without a comment" for c in cross
                 if not str(ops[c["op"]].get("comment") or "").strip()]
    problems += t.placeholder_errors(original, plan, ops)
    _orig, proposed = t.plan_views(plan)
    prop_path = t.resolve(workspace, _pinned(params, rec, "proposed"))
    if not prop_path.is_file():
        problems.append("proposed text file is missing")
    elif prop_path.read_text(encoding="utf-8") != "\n".join(proposed):
        problems.append("proposed.txt does not equal the original with the ops applied")
    docx = t.resolve(workspace, _pinned(params, rec, "docx"))
    if not docx.is_file():
        problems.append("redline .docx is missing")
    else:
        if t.docx_paragraphs(docx, "original") != original:
            problems.append("rejecting all changes in the .docx does not reproduce the original")
        if t.docx_paragraphs(docx, "accepted") != proposed:
            problems.append("accepting all changes in the .docx does not give the proposed text")
        if t.docx_comments(docx) != t.op_comments(ops):
            problems.append("the .docx margin comments are not exactly the ops' comments")
    markdown = _pinned(params, rec, "markdown")
    if markdown:
        md_path = t.resolve(workspace, markdown)
        rendered = t.render_redline_md(plan, ops, cross)
        if not md_path.is_file() or md_path.read_text(encoding="utf-8") != rendered:
            problems.append(f"{markdown} is not the rendering of the redline ops")
    if problems:
        return _result(False, _summary(problems), 0.0)
    moved = f", {len(cross)} cross-section edit(s) listed in redline.md" if cross else ""
    return _result(True, f"{len(ops)} ops re-applied to {contract} (base {base}); .docx reject-all = "
                         f"original, accept-all = proposed{moved}", 1.0)


@_guard
def references_resolve(workspace: Path, params: dict) -> dict[str, Any]:
    """params: redline (path), proposed (optional path, default the
    record's). The redline must not break a cross-reference or remove a
    definition whose term is still used. Breaks already in the
    counterparty's paper are reported but do not fail the check."""
    rec = _load_dict(workspace, params["redline"])
    src = t.input_file(workspace, str(rec.get("contract", "")))
    base, error = t.contract_base(src, rec.get("base"))
    if error:
        raise ValueError(error)
    original = t.load_paragraphs(src, base)
    proposed = t.resolve(workspace, _pinned(params, rec, "proposed")).read_text(
        encoding="utf-8").split("\n")
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


def _mentions(text: str, name: str) -> bool:
    return bool(re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", text, re.I))


@_guard
def memo_covers_issues(workspace: Path, params: dict) -> dict[str, Any]:
    """params: memo (path), issues (path), playbook (path; default the issue
    list's), severities (default critical, high). The memo must name every
    issue at those severities and every escalated issue by id, and every
    family marked absent or not applicable that the playbook rates at those
    severities or that is escalated, by family id or title."""
    memo = t.resolve(workspace, params["memo"]).read_text(encoding="utf-8")
    doc = _load_dict(workspace, params["issues"])
    wanted = set(params.get("severities") or ["critical", "high"])
    need = [str(i["id"]) for i in doc.get("issues", [])
            if i.get("severity") in wanted or i.get("escalate") is True]
    missing = [iid for iid in need if not _mentions(memo, iid)]
    fams = _families(workspace, params.get("playbook") or str(doc.get("playbook", "")))
    gaps = [r for r in doc.get("coverage", []) if isinstance(r, dict)
            and r.get("status") in ("absent", "not_applicable") and r.get("family") in fams
            and (r.get("escalate") is True or fams[r["family"]].get("severity", "high") in wanted)]
    need += [str(r["family"]) for r in gaps]
    missing += [str(r["family"]) for r in gaps if not _mentions(memo, str(r["family"]))
                and not _mentions(memo, str(fams[r["family"]].get("title") or r["family"]))]
    score = (len(need) - len(missing)) / len(need) if need else 1.0
    if missing:
        return _result(False, "memo does not mention: " + ", ".join(missing), score)
    return _result(True, f"memo covers all {len(need)} priority issues and missing protections", 1.0)


def _section_text(text: str, title: str) -> str:
    """The body under the first Markdown heading starting with title, up to
    the next heading of the same or a higher level."""
    body: list[str] | None = None
    level = 0
    for line in text.splitlines():
        m = re.match(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if m and body is not None and len(m.group(1)) <= level:
            break
        if m and body is None and m.group(2).strip().lower().startswith(title):
            body, level = [], len(m.group(1))
            continue
        if body is not None:
            body.append(line)
    return "\n".join(body or [])


@_guard
def hidden_content_disclosed(workspace: Path, params: dict) -> dict[str, Any]:
    """params: issues (path), report (path), contracts (optional). Re-scans
    the contract; if it carries hidden content, pending tracked changes or
    embedded instructions, the report must have a "Hidden content" heading
    whose section names each kind of finding."""
    doc, _paragraphs = _issue_doc(workspace, params["issues"], params)
    scan = t.scan_hidden_content(workspace, path=doc["contract"])
    kinds = sorted({f["kind"] for f in scan["findings"]})
    if not kinds:
        return _result(True, "no hidden content in the contract", 1.0)
    report = t.resolve(workspace, params["report"]).read_text(encoding="utf-8")
    if not any(h.startswith("hidden content") for h in headings(report)):
        return _result(False, f"contract has {', '.join(kinds)} but the report has no "
                              "'## Hidden content' heading", 0.0)
    section = _section_text(report, "hidden content").lower()
    missing = [k for k in kinds if k.replace("_", " ") not in section and k not in section]
    if missing:
        return _result(False, "hidden-content kinds not disclosed under Hidden content: "
                              + ", ".join(missing), 1 - len(missing) / len(kinds))
    return _result(True, f"disclosed {', '.join(kinds)}", 1.0)


@_guard
def approved_inputs_unchanged(workspace: Path, params: dict) -> dict[str, Any]:
    """params: paths (deliverables approved in earlier milestones). Each must
    be byte-for-byte what was approved: the artifact hash in its milestone's
    submission, else the hash the harness recorded before this milestone
    ran (tools.record_baseline). A file absent then must still be absent."""
    paths = params.get("paths") or []
    if not paths:
        return _result(False, "no approved paths to compare", 0.0)
    problems, ok = [], []
    for path in paths:
        expected, source = t.approved_sha(workspace, path)
        target = t.resolve(workspace, path)
        current = t.sha256_file(target) if target.is_file() else None
        if expected == "":
            problems.append(f"{path}: {source}")
        elif expected is None and current is not None:
            problems.append(f"{path} did not exist at {source}; a file written since is not approved")
        elif expected is None:
            ok.append(f"{path} absent, as at {source}")
        elif current is None:
            problems.append(f"{path} was deleted since {source}")
        elif current != expected:
            problems.append(f"{path} changed since {source} (approved sha256 {expected[:12]}, "
                            f"now {current[:12]}); restore it, or re-run and resubmit its milestone")
        else:
            ok.append(f"{path} sha256 {current[:12]} matches {source}")
    if problems:
        return _result(False, _summary(problems), len(ok) / len(paths))
    return _result(True, "; ".join(ok), 1.0)


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
    "approved_inputs_unchanged": approved_inputs_unchanged,
}
