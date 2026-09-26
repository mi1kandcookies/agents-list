"""
specialists/appsec_review/checks.py - acceptance checks for the application
security review specialist.

Checks are deterministic verifiers the platform (and the buyer) can re-run:
`fn(workspace: Path, params: dict, *, run=None) -> {"passed", "details", "score"}`
where passed is True/False, or None when a person still has to decide. Every
check recomputes from the source of truth (the SARIF file, the actual source
tree, the signed scope, the OSV answers in the claim ledger) rather than
trusting whatever the agent claimed, so a forged finding, a moved line, an
invented advisory, a score that does not follow from its vector, an
unreported secret or an out-of-scope target fails here. CHECK_DEFS at the
bottom is the name -> function map the manifest references.

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
from typing import Any, Callable, Iterable

from agentkit.errors import PolicyViolation, ToolError
from agentkit.ledger import Ledger
from agentkit.policy import INTERNAL_DIR, jail_path
from specialists.appsec_review import tools as T

MIN_SNIPPET_CHARS = 10     # non-space characters a source snippet must carry
MIN_PROBE_CHARS = 8        # shorter secret values are too likely to match by chance


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


def _paths(params: dict, default: str | None = None) -> list[str]:
    if params.get("paths"):
        return [str(p) for p in params["paths"]]
    if params.get("path"):
        return [str(params["path"])]
    if default:
        return [default]
    raise ValueError("no 'path' or 'paths' given")


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
        log = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return None, f"SARIF file is not valid JSON: {exc}"
    if not isinstance(log, dict):
        return None, "SARIF log must be a JSON object"
    return log, rel


def _iter_results(log: dict):
    for run in log.get("runs", []) or []:
        for res in run.get("results", []) or []:
            yield run, res


def _norm_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _fresh_secrets(workspace: Path, code_root: str) -> list[dict]:
    """A fresh scan of code_root, without hits in files the agent wrote."""
    is_authored = _authored(workspace)
    hits, _, _ = T.find_secrets(Path(workspace), code_root)
    return [h for h in hits if not is_authored(h["file"])]


def _hit_near(hits: list[dict], file: str, rule: Any, line: Any) -> dict | None:
    if not isinstance(line, int) or isinstance(line, bool):
        return None
    rule = str(rule or "").upper()
    return next((h for h in hits if h["file"] == file and h["rule_id"] == rule
                 and abs(h["line"] - line) <= 1), None)


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
    """Every result location points at a real file and line in customer source.

    params: path (SARIF), code_root (default '.') to resolve artifact URIs.
    A code finding needs a snippet of at least MIN_SNIPPET_CHARS non-space
    characters that matches the source at startLine..endLine, which catches
    stale or fabricated evidence. A SECRET.* finding carries no snippet (that
    would be the secret); it must match a hit of the same rule within a line
    in a fresh secret scan instead. A location outside the code root, under
    .agentkit/, or in a file the agent wrote itself fails. A log with no
    results passes: it reports no findings (findings_have_evidence then
    requires the register to say so).
    """
    log, ref = _load_sarif(workspace, params)
    if log is None:
        return _result(False, ref, 0.0)
    code_root = str(params.get("code_root", "."))
    try:
        root = jail_path(Path(workspace), code_root)
    except PolicyViolation as exc:
        return _result(False, f"code_root refused: {exc}", 0.0)
    is_authored = _authored(workspace)
    secrets: list[dict] | None = None
    results = total = by_snippet = by_scan = 0
    problems: list[str] = []
    for _, res in _iter_results(log):
        results += 1
        rule = str(res.get("ruleId") or "")
        if not res.get("locations"):
            total += 1
            problems.append(f"{rule or '?'}: result has no location")
            continue
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
            if not isinstance(start, int) or isinstance(start, bool) or not (1 <= start <= len(lines)):
                problems.append(f"{uri}: startLine {start} out of range (1..{len(lines)})")
                continue
            if rule.upper().startswith("SECRET."):
                if secrets is None:
                    try:
                        secrets = _fresh_secrets(workspace, code_root)
                    except ToolError:
                        secrets = []
                if _hit_near(secrets, rel, rule, start) is None:
                    problems.append(f"{uri}:{start}: no {rule} hit near that line in a fresh secret scan")
                    continue
                by_scan += 1
                continue
            snippet = str((region.get("snippet") or {}).get("text") or "")
            if len(re.sub(r"\s", "", snippet)) < MIN_SNIPPET_CHARS:
                problems.append(f"{uri}:{start}: needs a source snippet of at least "
                                f"{MIN_SNIPPET_CHARS} non-space characters")
                continue
            end = region.get("endLine", start)
            end = end if isinstance(end, int) and not isinstance(end, bool) and end >= start else start
            window = _norm_ws(" ".join(lines[start - 1:end]))
            if _norm_ws(snippet) not in window:
                problems.append(f"{uri}:{start}: snippet does not match source")
                continue
            by_snippet += 1
    if results == 0:
        return _result(True, "the SARIF log reports no findings; no locations to verify", 1.0)
    ok = by_snippet + by_scan
    passed = ok == total
    details = (f"all {total} location(s) verified: {by_snippet} by a matching source snippet, "
               f"{by_scan} by a fresh secret scan"
               if passed else f"{ok}/{total} verified; " + "; ".join(problems[:8]))
    return _result(passed, details, round(ok / total, 3))


_NO_FINDINGS = re.compile(r"(?i)\bno\s+(?:[a-z-]+\s+){0,2}findings\b")


def findings_have_evidence(workspace: Path, params: dict, *, run=None) -> dict:
    """Each finding carries a CWE or OWASP class, a CVSS vector, an exploit
    scenario and a fix.

    A security deliverable's whole value is that no finding is a bare
    assertion, so this enforces 'evidence + impact + remediation' per result.
    params: path (SARIF), summary (optional findings register). An empty log
    passes only when the summary says plainly that there are no findings, so
    a clean result is stated for the human to confirm, never implied.
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
        if not props.get("cvss"):
            missing.append("cvss vector")
        if not props.get("exploit"):
            missing.append("exploit")
        if not props.get("remediation"):
            missing.append("remediation")
        if missing:
            problems.append(f"{rid}: missing {', '.join(missing)}")
        else:
            ok += 1
    if total == 0:
        summary = params.get("summary")
        if not summary:
            return _result(True, "no findings reported", 1.0)
        try:
            text = jail_path(Path(workspace), summary).read_text(encoding="utf-8", errors="replace")
        except (PolicyViolation, OSError) as exc:
            return _result(False, f"no findings reported and the register is unreadable: {exc}", 0.0)
        if _NO_FINDINGS.search(text):
            return _result(True, f"no findings reported; {summary} states so for the human to confirm", 1.0)
        return _result(False, f"the SARIF log is empty but {summary} does not state that there are "
                              "no findings (e.g. a 'No findings' section)", 0.0)
    passed = ok == total
    details = (f"all {total} finding(s) carry class, CVSS vector, exploit and remediation"
               if passed else f"{ok}/{total} complete; " + "; ".join(problems[:8]))
    return _result(passed, details, round(ok / total, 3))


def _same_score(value: Any, score: float) -> bool:
    try:
        return abs(float(value) - score) < 1e-9
    except (TypeError, ValueError):
        return False


def cvss_consistent(workspace: Path, params: dict, *, run=None) -> dict:
    """Every result's CVSS vector re-scores to the numbers it carries.

    Recomputes each result's properties.cvss with the calculator and fails
    when the result's level, its security-severity, or its rule's
    security-severity (the highest score among the rule's results) disagree
    with the vector. A result without a vector fails too. params: path (SARIF).
    """
    log, ref = _load_sarif(workspace, params)
    if log is None:
        return _result(False, ref, 0.0)
    total = ok = 0
    problems: list[str] = []
    for run_ in log.get("runs", []) or []:
        driver = ((run_.get("tool") or {}).get("driver") or {})
        rules = {r.get("id"): r for r in driver.get("rules", []) or [] if isinstance(r, dict)}
        best: dict[str, float] = {}
        for res in run_.get("results", []) or []:
            total += 1
            rid = str(res.get("ruleId", "?"))
            props = res.get("properties") or {}
            vector = props.get("cvss")
            if not vector:
                problems.append(f"{rid}: no CVSS vector")
                continue
            try:
                score = T.cvss_base_score(vector=str(vector))["base_score"]
            except ToolError as exc:
                problems.append(f"{rid}: {exc}")
                continue
            best[rid] = max(score, best.get(rid, 0.0))
            wrong = []
            level, want = res.get("level", "warning"), T.sarif_level(score)
            if level != want:
                wrong.append(f"level {level!r} should be {want!r}")
            if "security-severity" in props and not _same_score(props["security-severity"], score):
                wrong.append(f"security-severity {props['security-severity']!r} should be {score:.1f}")
            if wrong:
                problems.append(f"{rid}: vector scores {score}: " + ", ".join(wrong))
            else:
                ok += 1
        for rid, score in best.items():
            sev = (((rules.get(rid) or {}).get("properties")) or {}).get("security-severity")
            if not _same_score(sev, score):
                problems.append(f"rule {rid}: security-severity {sev!r} should be {score:.1f}")
    if total == 0:
        return _result(True, "no findings to score", 1.0)
    passed = not problems
    details = (f"all {total} finding(s) carry the score and level their CVSS vector gives"
               if passed else "; ".join(problems[:8]))
    return _result(passed, details, round(ok / total, 3))


# --- scope ---------------------------------------------------------------------

_URL_HOST = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://(?:[^\s/@\"'<>]*@)?(\[[0-9a-f:.]+\]|[a-z0-9.-]+)")
_BARE_HOST = re.compile(r"(?i)(?<![\w.@/-])((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z](?:[a-z0-9-]*[a-z0-9])?)"
                        r"(?![\w-])")
_MD_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_MD_FENCE = re.compile(r"^\s{0,3}(```|~~~)")
_SCOPE_HEADING = re.compile(r"(?i)^(?:\d+(?:\.\d+)*[.)]?\s+)?(?:(?:in|out[- ]of)[- ]scope"
                            r"|scope(?:\s+(?:statement|confirmation|and\s+authori[sz]ation))?)$")


def _hosts_in(text: str) -> set[tuple[str, bool]]:
    """(host, from a URL) pairs. A bare name ('staging.example.com') may as
    well be a file name ('config.py'), so only URL hosts are ever listed as
    third-party references; bare names count only when they are customer hosts."""
    hosts = {(m.lower().strip("[]").rstrip("."), True) for m in _URL_HOST.findall(text)}
    return hosts | {(m.lower(), False) for m in _BARE_HOST.findall(text)}


def _markdown_lines(text: str) -> Iterable[tuple[bool, str]]:
    """(under a Scope heading, line). Deeper headings stay inside the section;
    headings in fenced code are not headings."""
    level, fence = None, None
    for line in text.splitlines():
        m = _MD_FENCE.match(line)
        if m:
            fence = None if fence == m.group(1) else (fence or m.group(1))
        elif fence is None:
            h = _MD_HEADING.match(line)
            if h:
                depth = len(h.group(1))
                if _SCOPE_HEADING.match(h.group(2).strip()):
                    level = depth
                elif level is not None and depth <= level:
                    level = None
        yield level is not None, line


def _families(scope: dict) -> set[str]:
    """The customer's own domains: the parent domain of every host rule."""
    out = set()
    for rule in scope["allow"] + scope["deny"]:
        if "/" in rule:
            continue
        labels = (rule[2:] if rule.startswith("*.") else rule).split(".")
        out.add(".".join(labels[-2:]))
    return out


def _servers(node: Any) -> Iterable[str]:
    """URLs listed under any 'servers' key of a JSON document."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "servers" and isinstance(v, list):
                for s in v:
                    url = s.get("url") if isinstance(s, dict) else s
                    if isinstance(url, str) and url.strip():
                        yield url
            else:
                yield from _servers(v)
    elif isinstance(node, list):
        for v in node:
            yield from _servers(v)


def scope_respected(workspace: Path, params: dict, *, run=None) -> dict:
    """No deliverable points at a host outside the signed scope.

    params: paths (or path) of deliverables, scope_file. Recomputed from the
    scope file with the same rules as check_scope (out-of-scope entries win):
    - SARIF location URIs with a host and every JSON `servers` URL are review
      targets and must be in scope;
    - a host the scope file lists as out of scope may be named only under a
      Scope heading of a markdown deliverable (restating the signed scope);
    - any other host in the customer's own domains (the parent domain of a
      scope entry, e.g. a sibling environment) must be in scope;
    - third-party hosts (documentation, advisories) are not targets by
      themselves: they are listed in the details for the human reviewer.
    """
    scope_rel = params.get("scope_file", "inputs/scope.json")
    try:
        jail_path(Path(workspace), scope_rel)
    except PolicyViolation as exc:
        return _result(False, f"scope file refused: {exc}", 0.0)
    try:
        scope = T.load_scope(Path(workspace), scope_rel)
    except ToolError as exc:
        if "not found" in str(exc):
            return _result(None, f"scope file not found: {scope_rel}; human must confirm", None)
        return _result(False, str(exc), 0.0)
    try:
        paths = _paths(params, "deliverables/m2-findings/findings.sarif")
    except ValueError as exc:
        return _result(False, str(exc), 0.0)
    families = _families(scope)
    violations: dict[str, None] = {}
    others: set[str] = set()
    absent: list[str] = []
    checked = 0

    def grader(rel: str, host: str, *, target: bool = False, url: bool = True,
              in_scope_section: bool = False) -> None:
        nonlocal checked
        ok, why = T.scope_verdict(host, scope)
        denied = why == "listed out of scope"
        customer = any(host == f or host.endswith("." + f) for f in families)
        if not (target or url or denied or customer or ok):
            return                                  # a bare name that is not a host we know of
        checked += 1
        if ok:
            return
        if target:
            violations[f"{rel}: {host} ({why})"] = None
        elif denied and not in_scope_section:
            violations[f"{rel}: {host} (listed out of scope)"] = None
        elif not denied and customer:
            where = ("named in the Scope section but not in the signed scope file" if in_scope_section
                     else "a customer host outside the signed scope")
            violations[f"{rel}: {host} ({where})"] = None
        elif not denied:
            others.add(host)

    def grader_text(rel: str, text: str, in_scope_section: bool = False) -> None:
        for host, url in _hosts_in(text):
            grader(rel, host, url=url, in_scope_section=in_scope_section)

    for rel in paths:
        try:
            path = jail_path(Path(workspace), rel)
        except PolicyViolation as exc:
            return _result(False, f"deliverable path refused: {exc}", 0.0)
        if not path.is_file():
            absent.append(rel)
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        suffix = path.suffix.lower()
        if suffix in (".sarif", ".json"):
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = None
            if suffix == ".sarif" and isinstance(data, dict):
                for _, res in _iter_results(data):
                    for loc in res.get("locations", []) or []:
                        uri = str(((loc.get("physicalLocation") or {}).get("artifactLocation") or {})
                                  .get("uri", ""))
                        if "://" in uri:
                            grader(rel, T.host_of(uri), target=True)
                    grader_text(rel, json.dumps(res))
                continue
            for url in _servers(data):
                grader(rel, T.host_of(url), target=True)
            grader_text(rel, text)
            continue
        if suffix in (".md", ".markdown"):
            for in_section, line in _markdown_lines(text):
                grader_text(rel, line, in_section)
        else:
            grader_text(rel, text)
    notes = []
    if absent:
        notes.append(f"not present: {', '.join(absent)}")
    if others:
        notes.append("third-party hosts mentioned, not treated as targets (human to confirm): "
                     + ", ".join(sorted(others)[:15]))
    if violations:
        return _result(False, "out-of-scope targets: " + "; ".join(list(violations)[:10])
                       + ("; " + "; ".join(notes) if notes else ""), 0.0)
    details = f"{checked} host reference(s) checked; every customer host and target is inside the signed scope"
    return _result(True, details + ("; " + "; ".join(notes) if notes else ""), 1.0)


# --- secrets -------------------------------------------------------------------

def secret_findings_reconcile(workspace: Path, params: dict, *, run=None) -> dict:
    """Re-run the secret scanner and reconcile it with the secrets report, both ways.

    params: report (JSON: a list of reported hits {file, line, rule_id}, or
    {"findings": [...], "dismissed": [{file, line, rule_id, reason}]}),
    code_root (default 'repo'). Never trusts the agent's list: it rescans the
    source and fails when a reported hit has no real hit at that file:line
    (fabricated or stale), when a real hit is neither reported nor dismissed
    with a reason (a missed secret), or when a dismissal matches nothing.
    Dismissals are listed in the details for the human reviewer. Hits in files
    the agent wrote during the engagement do not count. (SECRET.* results in
    the SARIF log are checked against the same scan by sarif_locations_exist.)
    """
    report_rel = params.get("report", "deliverables/m2-findings/secrets.json")
    try:
        report_path = jail_path(Path(workspace), report_rel)
    except PolicyViolation as exc:
        return _result(False, f"secrets report refused: {exc}", 0.0)
    if not report_path.exists():
        return _result(False, f"secrets report not found: {report_rel}", 0.0)
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return _result(False, f"secrets report is not valid JSON: {exc}", 0.0)
    dismissed: Any = []
    claimed: Any = report
    if isinstance(report, dict):
        claimed, dismissed = report.get("findings", []) or [], report.get("dismissed", []) or []
    if not isinstance(claimed, list) or not isinstance(dismissed, list):
        return _result(False, "secrets report must be a list of findings (or findings + dismissed)", 0.0)

    code_root = str(params.get("code_root", "repo"))
    try:
        fresh = _fresh_secrets(workspace, code_root)
    except ToolError as exc:
        return _result(False, f"code_root refused: {exc}", 0.0)
    root = code_root.replace("\\", "/").strip("/")

    covered: set[int] = set()

    def match(item: Any) -> int | None:
        """The fresh hit an item stands for: same file and rule within a
        line, preferring the same column and a hit no other item took (two
        secrets on one line are two hits)."""
        if not isinstance(item, dict) or not isinstance(item.get("file"), str):
            return None
        line, rule = item.get("line"), str(item.get("rule_id") or "").upper()
        if not isinstance(line, int) or isinstance(line, bool):
            return None
        f = item["file"].replace("\\", "/").removeprefix("./")
        names = {f} | ({f"{root}/{f}"} if root not in ("", ".") and not f.startswith(root + "/") else set())
        found = [i for i, h in enumerate(fresh)
                 if h["file"] in names and h["rule_id"] == rule and abs(h["line"] - line) <= 1]
        if not found:
            return None
        found.sort(key=lambda i: (i in covered, fresh[i]["column"] != item.get("column"),
                                  abs(fresh[i]["line"] - line)))
        return found[0]

    def label(item: Any) -> str:
        item = item if isinstance(item, dict) else {}
        return f"{item.get('file')}:{item.get('line')} [{item.get('rule_id')}]"

    bogus, bad_dismissals, dismissals = [], [], []
    for item in claimed:
        i = match(item)
        if i is None:
            bogus.append(label(item))
        else:
            covered.add(i)
    for item in dismissed:
        i = match(item)
        reason = str(item.get("reason") or "").strip() if isinstance(item, dict) else ""
        if i is None or not reason:
            bad_dismissals.append(label(item) + (" (no reason)" if i is not None else ""))
        else:
            covered.add(i)
            dismissals.append(f"{label(item)} - {reason[:120]}")
    missing = [f"{h['file']}:{h['line']} [{h['rule_id']}]" for i, h in enumerate(fresh) if i not in covered]
    good = len(claimed) - len(bogus) + len(dismissals)
    denominator = len(claimed) + len(dismissed) + len(missing)
    score = round(good / denominator, 3) if denominator else 1.0
    if bogus or bad_dismissals or missing:
        parts = []
        if bogus:
            parts.append("unverifiable (no matching hit in a fresh scan): " + "; ".join(bogus[:8]))
        if missing:
            parts.append("found by a fresh scan but neither reported nor dismissed with a reason: "
                         + "; ".join(missing[:8]))
        if bad_dismissals:
            parts.append("dismissals that match no hit or give no reason: " + "; ".join(bad_dismissals[:8]))
        return _result(False, " | ".join(parts), score)
    if not fresh:
        return _result(True, f"a fresh scan of {code_root}/ finds no secrets and none are claimed", 1.0)
    details = (f"{len(claimed)} reported and {len(dismissals)} dismissed hit(s) reconcile with the "
               f"{len(fresh)} hit(s) of a fresh scan of {code_root}/")
    if dismissals:
        details += "; dismissed by the agent, for the human reviewer to confirm: " + "; ".join(dismissals[:10])
    return _result(True, details, 1.0)


# Lowercase words joined by '-', '_' or '.': a resource or host name that a
# keyword-named setting points at, not a literal secret worth hiding.
_NAME_LIKE = re.compile(r"^[a-z]+(?:[-_.][a-z]+)+$")


def no_secret_values(workspace: Path, params: dict, *, run=None) -> dict:
    """No raw secret value from the customer's files appears in a deliverable.

    params: roots (default ['repo', 'inputs']) to scan for secrets, path
    (default 'deliverables') to search. Re-runs the scanner, takes each hit's
    raw value (for a private key, its first body line) and fails if any of
    them appears in any text file under path. Generic assignments whose value
    is only lowercase words joined by '-', '_' or '.' (a resource or host
    name) are not treated as secret values. The details name the rule and
    location, never the value.
    """
    roots = params.get("roots") or ["repo", "inputs"]
    probes: dict[str, dict] = {}
    for root in roots:
        try:
            hits, _, _ = T.find_secrets(Path(workspace), str(root))
        except ToolError as exc:
            return _result(False, f"scan root refused: {exc}", 0.0)
        for h in hits:
            if len(h["probe"]) < MIN_PROBE_CHARS:
                continue
            if h["rule_id"] == "SECRET.GENERIC_ASSIGNMENT" and _NAME_LIKE.match(h["probe"]):
                continue        # e.g. secret_name: db-credentials, token_issuer: auth.example.com
            probes.setdefault(h["probe"], h)
    target_rel = str(params.get("path", "deliverables"))
    try:
        target = jail_path(Path(workspace), target_rel)
    except PolicyViolation as exc:
        return _result(False, f"deliverables path refused: {exc}", 0.0)
    if not target.exists():
        return _result(True, f"nothing under {target_rel} to check", 1.0)
    files, _ = T.text_files(target, Path(workspace))
    leaks: list[str] = []
    for f in files:
        text = f.read_text(encoding="utf-8", errors="replace")
        for probe, h in probes.items():
            if probe in text:
                leaks.append(f"{_rel(workspace, f)} contains the {h['rule_id']} value from "
                             f"{h['file']}:{h['line']} ({T.redact_secret(probe)})")
    if leaks:
        return _result(False, "raw secret values in deliverables: " + "; ".join(leaks[:8]), 0.0)
    return _result(True, f"{len(probes)} secret value(s) checked against {len(files)} deliverable file(s); "
                         "none appears", 1.0)


# --- dependencies ----------------------------------------------------------------

ADVISORY_ID = re.compile(r"(?i)\b(CVE-\d{4}-\d{4,}|GHSA(?:-[0-9a-z]{4}){3}|PYSEC-\d{4}-\d+|OSV-\d{4}-\d+"
                         r"|RUSTSEC-\d{4}-\d{4}|GO-\d{4}-\d{4,}|GSD-\d{4}-\d+|MAL-\d{4}-\d+)\b")


def advisories_reconcile(workspace: Path, params: dict, *, run=None) -> dict:
    """Every advisory id a deliverable names (CVE, GHSA, PYSEC, OSV, ...) is
    in an OSV answer (a ledger source of kind 'tool') or a fetched page (kind
    'web') the kit recorded, with its snapshot intact. params: paths (or path).
    An invented or mistyped CVE fails; customer files and the agent's own
    files never count as a source.
    """
    try:
        paths = _paths(params)
    except ValueError as exc:
        return _result(False, str(exc), 0.0)
    known: set[str] = set()
    try:
        ledger = Ledger(Path(workspace))
        for src in ledger.sources:
            if src.kind in ("tool", "web") and ledger.snapshot_problem(src.id) is None:
                known |= {m.upper() for m in ADVISORY_ID.findall(ledger.snapshot(src.id))}
    except (OSError, ValueError, TypeError):
        known = set()
    cited: dict[str, list[str]] = {}
    absent = []
    for rel in paths:
        try:
            path = jail_path(Path(workspace), rel)
        except PolicyViolation as exc:
            return _result(False, f"deliverable path refused: {exc}", 0.0)
        if not path.is_file():
            absent.append(rel)
            continue
        for m in ADVISORY_ID.findall(path.read_text(encoding="utf-8", errors="replace")):
            cited.setdefault(m.upper(), []).append(rel)
    note = f"; not present: {', '.join(absent)}" if absent else ""
    if not cited:
        return _result(True, "no advisory ids cited" + note, 1.0)
    unknown = sorted(set(cited) - known)
    if unknown:
        where = "; ".join(f"{i} ({', '.join(sorted(set(cited[i])))})" for i in unknown[:10])
        return _result(False, "advisory ids not found in any recorded OSV answer or fetched advisory: "
                              + where + note, round(1 - len(unknown) / len(cited), 3))
    return _result(True, f"all {len(cited)} advisory id(s) appear in recorded OSV answers or advisories" + note,
                   1.0)


# --- attack surface ------------------------------------------------------------

_OPERATION = re.compile(r"(?i)^\s*(GET|PUT|POST|DELETE|PATCH|OPTIONS|HEAD|TRACE)\s+(/\S*)\s*$")


def _surface_entries(data: Any) -> tuple[set[str], set[str]]:
    """('METHOD /path' listed, the ones marked unauthenticated) from an
    attack-surface document: objects with method + path (auth_required or
    authenticated false marks them), and 'METHOD /path' strings (under a key
    containing 'unauth' they count as unauthenticated)."""
    listed: set[str] = set()
    unauth: set[str] = set()

    def walk(node: Any, flagged: bool) -> None:
        if isinstance(node, dict):
            method, route = node.get("method"), node.get("path")
            if isinstance(method, str) and isinstance(route, str):
                sig = f"{method.upper()} {route}"
                listed.add(sig)
                if flagged or node.get("auth_required") is False or node.get("authenticated") is False:
                    unauth.add(sig)
            for k, v in node.items():
                walk(v, flagged or "unauth" in str(k).lower())
        elif isinstance(node, list):
            for v in node:
                walk(v, flagged)
        elif isinstance(node, str):
            m = _OPERATION.match(node)
            if m:
                sig = f"{m.group(1).upper()} {m.group(2)}"
                listed.add(sig)
                if flagged:
                    unauth.add(sig)

    walk(data, False)
    return listed, unauth


def _find_specs(workspace: Path) -> list[str]:
    """Workspace paths of the OpenAPI/Swagger documents under inputs/."""
    base = Path(workspace) / "inputs"
    if not base.is_dir():
        return []
    found = []
    files, _ = T.text_files(base, Path(workspace))
    for f in files:
        if f.suffix.lower() not in (".json", ".yaml", ".yml"):
            continue
        try:
            doc = T.load_spec(f)
        except ToolError:
            continue
        if "openapi" in doc or "swagger" in doc:
            found.append(_rel(workspace, f))
    return found


def attack_surface_covers_spec(workspace: Path, params: dict, *, run=None) -> dict:
    """The attack-surface map lists every operation of the customer's API
    spec, and flags every unauthenticated one.

    params: path (attack-surface JSON), spec (optional; default every
    OpenAPI/Swagger document under inputs/). Re-runs parse_openapi on the spec
    and compares. With no spec there is nothing to recompute: the check passes
    and says the inventory is for the human to review.
    """
    rel = params.get("path", "deliverables/m1-scope-threat-model/attack-surface.json")
    try:
        path = jail_path(Path(workspace), rel)
    except PolicyViolation as exc:
        return _result(False, f"attack-surface path refused: {exc}", 0.0)
    if not path.is_file():
        return _result(False, f"attack-surface map not found: {rel}", 0.0)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return _result(False, f"attack-surface map is not valid JSON: {exc}", 0.0)
    specs = [str(params["spec"])] if params.get("spec") else _find_specs(Path(workspace))
    if not specs:
        return _result(True, "no OpenAPI/Swagger spec under inputs/; the endpoint inventory "
                             "comes from source and is for the human to review", 1.0)
    expected: set[str] = set()
    expected_unauth: set[str] = set()
    for spec in specs:
        try:
            out = T.parse_openapi(Path(workspace), spec_path=spec)
        except ToolError as exc:
            return _result(False, f"cannot re-parse {spec}: {exc}", 0.0)
        expected |= {f"{e['method']} {e['path']}" for e in out["endpoints"]}
        expected_unauth |= set(out["unauthenticated"])
    listed, unauth = _surface_entries(data)
    missing = sorted(expected - listed)
    unflagged = sorted(expected_unauth - unauth)
    if missing or unflagged:
        parts = []
        if missing:
            parts.append("operations missing: " + ", ".join(missing[:15]))
        if unflagged:
            parts.append("unauthenticated but not flagged: " + ", ".join(unflagged[:15]))
        score = round((len(expected) - len(missing)) / max(1, len(expected)), 3)
        return _result(False, "; ".join(parts), score)
    return _result(True, f"all {len(expected)} operation(s) of {', '.join(specs)} listed, "
                         f"{len(expected_unauth)} unauthenticated one(s) flagged", 1.0)


CHECK_DEFS = {
    "sarif_valid": sarif_valid,
    "sarif_locations_exist": sarif_locations_exist,
    "findings_have_evidence": findings_have_evidence,
    "cvss_consistent": cvss_consistent,
    "scope_respected": scope_respected,
    "secret_findings_reconcile": secret_findings_reconcile,
    "no_secret_values": no_secret_values,
    "advisories_reconcile": advisories_reconcile,
    "attack_surface_covers_spec": attack_surface_covers_spec,
}
