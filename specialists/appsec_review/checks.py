"""
specialists/appsec_review/checks.py - acceptance checks for the application
security review specialist.

Checks are deterministic verifiers the platform (and the buyer) can re-run:
`fn(workspace: Path, params: dict, *, run=None) -> {"passed", "details", "score"}`
where passed is True/False, or None when a person still has to decide. Every
check recomputes from the source of truth (the SARIF file, the actual source
tree, the signed scope) rather than trusting whatever the agent claimed, so a
forged finding, a moved line or an out-of-scope target fails here. CHECK_DEFS at
the bottom is the name -> function map the manifest references.

Paths from params and from the SARIF log are jailed (the workspace, and for
SARIF locations the code root) before anything is read. Evidence must be
customer source: a location or secret in a file the agent wrote during the
engagement (the claim ledger's "authored" list, kept by write_file and the
domain tools) never counts, so the agent cannot plant the code it reports.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

from agentkit.errors import PolicyViolation
from agentkit.ledger import Ledger
from agentkit.policy import INTERNAL_DIR, jail_path
from specialists.appsec_review.tools import _host_in_scope, _host_of, scan_secrets


def _result(passed, details, score=None) -> dict:
    return {"passed": passed, "details": details, "score": score}


def _authored(workspace: Path) -> Callable[[str], bool]:
    """is_authored(rel) from the run's claim ledger. An unreadable ledger
    treats every file as authored, so a broken ledger fails closed."""
    try:
        return Ledger(Path(workspace)).is_authored
    except (OSError, ValueError, TypeError):
        return lambda rel: True


def _rel(workspace: Path, path: Path) -> str:
    return path.relative_to(Path(workspace).resolve()).as_posix()


def _load_sarif(workspace: Path, params: dict) -> tuple[dict | None, str]:
    """Read and JSON-parse the SARIF file named in params['path']."""
    rel = params.get("path", "deliverables/m2-findings/findings.sarif")
    try:
        path = jail_path(Path(workspace), rel)
    except PolicyViolation as exc:
        return None, f"SARIF path refused: {exc}"
    if not path.exists():
        return None, f"SARIF file not found: {rel}"
    try:
        return json.loads(path.read_text(encoding="utf-8")), rel
    except (json.JSONDecodeError, OSError) as exc:
        return None, f"SARIF file is not valid JSON: {exc}"


def _iter_results(log: dict):
    for run in log.get("runs", []) or []:
        for res in run.get("results", []) or []:
            yield run, res


def _norm_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


# --- structural validity -----------------------------------------------------

def sarif_valid(workspace: Path, params: dict, *, run=None) -> dict:
    """SARIF 2.1.0 has the required envelope, a tool driver and well-formed results."""
    log, ref = _load_sarif(workspace, params)
    if log is None:
        return _result(False, ref, 0.0)
    problems: list[str] = []
    if log.get("version") != "2.1.0":
        problems.append(f"version is {log.get('version')!r}, expected '2.1.0'")
    runs = log.get("runs")
    if not isinstance(runs, list) or not runs:
        return _result(False, "runs[] missing or empty", 0.0)
    driver = ((runs[0].get("tool") or {}).get("driver") or {})
    if not driver.get("name"):
        problems.append("runs[0].tool.driver.name missing")
    total = 0
    rule_ids = {r.get("id") for r in driver.get("rules", []) or []}
    for _, res in _iter_results(log):
        total += 1
        if not res.get("ruleId"):
            problems.append("a result has no ruleId")
        if res.get("level") not in ("error", "warning", "note", "none", None):
            problems.append(f"result has invalid level {res.get('level')!r}")
        locs = res.get("locations")
        if not isinstance(locs, list) or not locs:
            problems.append(f"result {res.get('ruleId')} has no locations")
            continue
        phys = (locs[0].get("physicalLocation") or {})
        if not (phys.get("artifactLocation") or {}).get("uri"):
            problems.append(f"result {res.get('ruleId')} has no artifact uri")
        if not (phys.get("region") or {}).get("startLine"):
            problems.append(f"result {res.get('ruleId')} has no startLine")
    # Rules referenced by results should be declared (soft signal in score).
    undeclared = {res.get("ruleId") for _, res in _iter_results(log)} - rule_ids - {None}
    if undeclared:
        problems.append(f"results reference undeclared rules: {sorted(undeclared)}")
    passed = not problems
    score = 1.0 if passed else max(0.0, 1.0 - len(problems) / max(total, 1))
    details = (f"SARIF 2.1.0 valid: {total} result(s), {len(rule_ids)} rule(s)."
               if passed else "; ".join(problems))
    return _result(passed, details, round(score, 3))


def sarif_locations_exist(workspace: Path, params: dict, *, run=None) -> dict:
    """Every result location points at a real file, line and (if given) snippet.

    params: path (SARIF), code_root (default '.') to resolve artifact URIs.
    A finding whose snippet no longer matches the source at that line fails,
    which catches stale or fabricated evidence; so does a location outside the
    code root, under .agentkit/, or in a file the agent wrote itself.
    """
    log, ref = _load_sarif(workspace, params)
    if log is None:
        return _result(False, ref, 0.0)
    try:
        root = jail_path(Path(workspace), params.get("code_root", "."))
    except PolicyViolation as exc:
        return _result(False, f"code_root refused: {exc}", 0.0)
    is_authored = _authored(workspace)
    total = ok = 0
    problems: list[str] = []
    for _, res in _iter_results(log):
        for loc in res.get("locations", []) or []:
            total += 1
            phys = loc.get("physicalLocation") or {}
            uri = str((phys.get("artifactLocation") or {}).get("uri", ""))
            region = phys.get("region") or {}
            start = region.get("startLine")
            try:
                target = jail_path(root, uri)
            except PolicyViolation:
                problems.append(f"{uri}: not a file inside the code root")
                continue
            rel = _rel(workspace, target)
            if rel.split("/", 1)[0] == INTERNAL_DIR:
                problems.append(f"{uri}: kit-internal file, not source")
                continue
            if not target.is_file():
                problems.append(f"{uri}: file not found")
                continue
            if is_authored(rel):
                problems.append(f"{uri}: written by the agent during the engagement, not customer source")
                continue
            try:
                lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError as exc:
                problems.append(f"{uri}: unreadable ({exc})")
                continue
            if not isinstance(start, int) or not (1 <= start <= len(lines)):
                problems.append(f"{uri}: startLine {start} out of range (1..{len(lines)})")
                continue
            snippet = (region.get("snippet") or {}).get("text")
            if snippet:
                end = region.get("endLine", start)
                window = _norm_ws(" ".join(lines[start - 1:max(start, end)]))
                if _norm_ws(snippet) not in window:
                    problems.append(f"{uri}:{start}: snippet does not match source")
                    continue
            ok += 1
    if total == 0:
        return _result(False, "no locations to verify", 0.0)
    passed = ok == total
    details = (f"all {total} location(s) resolve to real file:line with matching snippets"
               if passed else f"{ok}/{total} verified; " + "; ".join(problems[:8]))
    return _result(passed, details, round(ok / total, 3))


def findings_have_evidence(workspace: Path, params: dict, *, run=None) -> dict:
    """Each finding carries a CWE or OWASP class, an exploit scenario and a fix.

    A pentest deliverable's whole value is that no finding is a bare assertion,
    so this enforces the 'evidence + impact + remediation' shape per result.
    """
    log, ref = _load_sarif(workspace, params)
    if log is None:
        return _result(False, ref, 0.0)
    total = ok = 0
    problems: list[str] = []
    for _, res in _iter_results(log):
        total += 1
        props = res.get("properties") or {}
        rid = res.get("ruleId", "?")
        missing = []
        if not (props.get("cwe") or props.get("owasp")):
            missing.append("cwe/owasp")
        if not props.get("exploit"):
            missing.append("exploit")
        if not props.get("remediation"):
            missing.append("remediation")
        if missing:
            problems.append(f"{rid}: missing {', '.join(missing)}")
        else:
            ok += 1
    if total == 0:
        return _result(False, "no findings to check", 0.0)
    passed = ok == total
    details = (f"all {total} finding(s) carry class, exploit and remediation"
               if passed else f"{ok}/{total} complete; " + "; ".join(problems[:8]))
    return _result(passed, details, round(ok / total, 3))


def scope_respected(workspace: Path, params: dict, *, run=None) -> dict:
    """No SARIF location refers to a host outside the signed scope allow-list.

    Source-file URIs (no host) are always in scope; only network-style URIs with
    a host are checked. params: path (SARIF), scope_file.
    """
    log, ref = _load_sarif(workspace, params)
    if log is None:
        return _result(False, ref, 0.0)
    scope_rel = params.get("scope_file", "inputs/scope.json")
    try:
        scope_path = jail_path(Path(workspace), scope_rel)
    except PolicyViolation as exc:
        return _result(False, f"scope file refused: {exc}", 0.0)
    if not scope_path.exists():
        return _result(None, f"scope file not found: {scope_rel}; human must confirm", None)
    allow = list((json.loads(scope_path.read_text(encoding="utf-8")) or {}).get("hosts", []))
    total = ok = 0
    violations: list[str] = []
    for _, res in _iter_results(log):
        for loc in res.get("locations", []) or []:
            uri = ((loc.get("physicalLocation") or {}).get("artifactLocation") or {}).get("uri", "")
            if "://" not in uri:  # local source path, not a target host
                continue
            total += 1
            host = _host_of(uri)
            if _host_in_scope(host, allow):
                ok += 1
            else:
                violations.append(f"{host} (out of scope)")
    if total == 0:
        return _result(True, "no network-scoped locations; all findings are on source files", 1.0)
    passed = ok == total
    details = ("all targeted hosts are inside the signed scope"
               if passed else "out-of-scope targets: " + "; ".join(sorted(set(violations))))
    return _result(passed, details, round(ok / total, 3))


def secret_findings_reconcile(workspace: Path, params: dict, *, run=None) -> dict:
    """Re-run the secret scanner and confirm every claimed secret really exists.

    params: report (JSON list of {file, line, rule_id}), code_root (default
    'repo'). This never trusts the agent's list: it rescans the source and fails
    if any claimed finding has no matching real hit at that file:line, which
    catches fabricated or stale secret findings. Hits in files the agent wrote
    during the engagement do not count.
    """
    report_rel = params.get("report", "deliverables/m2-findings/secrets.json")
    try:
        report_path = jail_path(Path(workspace), report_rel)
    except PolicyViolation as exc:
        return _result(False, f"secrets report refused: {exc}", 0.0)
    if not report_path.exists():
        return _result(False, f"secrets report not found: {report_rel}", 0.0)
    try:
        claimed = json.loads(report_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return _result(False, f"secrets report is not valid JSON: {exc}", 0.0)
    if isinstance(claimed, dict):
        claimed = claimed.get("findings", [])
    if not isinstance(claimed, list):
        return _result(False, "secrets report must be a list of findings", 0.0)

    fresh = scan_secrets(Path(workspace), root=params.get("code_root", "repo"))["findings"]
    is_authored = _authored(workspace)
    # A tolerant match: same file and rule within a line or two (formatting drift).
    real = {(f["file"], f["rule_id"], f["line"]) for f in fresh if not is_authored(f["file"])}
    total = ok = 0
    bogus: list[str] = []
    for item in claimed:
        total += 1
        item = item if isinstance(item, dict) else {}
        f = item.get("file")
        rid = item.get("rule_id")
        line = item.get("line")
        if isinstance(f, str):
            f = f.replace("\\", "/")
        if (isinstance(line, int) and not isinstance(line, bool)
                and any((f, rid, line + d) in real for d in (-1, 0, 1))):
            ok += 1
        else:
            bogus.append(f"{f}:{line} [{rid}]")
    if total == 0:
        return _result(True, "no secret findings claimed; nothing to reconcile", 1.0)
    passed = ok == total
    details = (f"all {total} claimed secret finding(s) reproduce from a fresh scan"
               if passed else f"{ok}/{total} reproduce; unverifiable: " + "; ".join(bogus[:8]))
    return _result(passed, details, round(ok / total, 3))


CHECK_DEFS = {
    "sarif_valid": sarif_valid,
    "sarif_locations_exist": sarif_locations_exist,
    "findings_have_evidence": findings_have_evidence,
    "scope_respected": scope_respected,
    "secret_findings_reconcile": secret_findings_reconcile,
}
