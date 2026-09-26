"""
specialists/test_coverage/tools.py - deterministic domain tools for the
test-coverage specialist.

Every tool is a plain function

    fn(workspace: Path, *, fetch=None, run=None, **args) -> dict | str

listed in TOOL_DEFS; agent.py wraps them as kit tools. The model decides what
to test, these functions do the counting: coverage reports (Cobertura, LCOV,
JaCoCo, Go cover profiles), JUnit run results, unified diffs, mutation
reports (the open mutation-testing-elements JSON schema), risk ranking and a
secret scan of added lines. Subprocess work goes through the injected `run`
(the kit's shell-policy-checked runner); nothing here opens a socket or
spawns a process itself.

Paths in arguments are workspace-relative; outputs are written only under
the workspace and never under inputs/.
"""
from __future__ import annotations

import ast
import csv
import io
import json
import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable, Iterable

from agentkit.errors import ToolError

# Paths that count as "test code" when no globs are given. `**/` matches any
# number of directories (including none); `*` stays inside one path segment.
DEFAULT_TEST_GLOBS = [
    "tests/**", "test/**", "**/tests/**", "**/test/**", "**/__tests__/**",
    "**/test_*.py", "**/*_test.py", "**/conftest.py", "**/*_test.go",
    "**/*.test.js", "**/*.test.ts", "**/*.test.jsx", "**/*.test.tsx",
    "**/*.spec.js", "**/*.spec.ts", "src/test/**", "**/*Test.java", "**/*Tests.java",
    "**/*Test.kt", "**/*Tests.cs",
]

MAX_REPORT_BYTES = 50 * 1024 * 1024


# --- helpers -----------------------------------------------------------------

def _ws_path(workspace: Path, rel: str, *, write: bool = False) -> Path:
    """Resolve a workspace-relative path, refusing escapes (and inputs/ writes)."""
    if not rel or not isinstance(rel, str):
        raise ToolError("path is required")
    root = Path(workspace).resolve()
    target = (root / rel).resolve()
    if target != root and root not in target.parents:
        raise ToolError(f"path escapes the workspace: {rel}")
    if write:
        inputs = root / "inputs"
        if target == inputs or inputs in target.parents:
            raise ToolError("inputs/ is read-only")
        if (root / ".agentkit") in target.parents:
            raise ToolError(".agentkit/ is internal to the kit")
    return target


def _read_text(workspace: Path, rel: str) -> str:
    path = _ws_path(workspace, rel)
    if not path.is_file():
        raise ToolError(f"file not found: {rel}")
    if path.stat().st_size > MAX_REPORT_BYTES:
        raise ToolError(f"file too large: {rel}")
    return path.read_text(encoding="utf-8", errors="replace")


def _write_json(workspace: Path, rel: str, data: Any) -> str:
    path = _ws_path(workspace, rel, write=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return rel


def _parse_xml(text: str, label: str) -> ET.Element:
    # Coverage and JUnit reports never need entity declarations; refusing
    # them closes off entity-expansion tricks in hostile reports.
    if "<!ENTITY" in text:
        raise ToolError(f"{label}: XML entity declarations are not allowed")
    try:
        return ET.fromstring(text)
    except ET.ParseError as exc:
        raise ToolError(f"{label}: invalid XML ({exc})") from exc


def _norm(path: str) -> str:
    path = path.replace("\\", "/").strip()
    while path.startswith("./"):
        path = path[2:]
    return path


def glob_regex(pattern: str) -> re.Pattern[str]:
    """Compile a path glob: `**/` = any directories, `*` = within one segment."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pattern[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(out + r"\Z")


def path_matches(path: str, patterns: Iterable[str]) -> bool:
    """True if `path` matches a glob, or equals / sits under a plain prefix."""
    path = _norm(path)
    for pat in patterns:
        pat = _norm(pat)
        if not pat:
            continue
        if any(ch in pat for ch in "*?"):
            if glob_regex(pat).match(path):
                return True
        elif path == pat.rstrip("/") or path.startswith(pat.rstrip("/") + "/"):
            return True
    return False


def _pct(covered: int, total: int) -> float | None:
    return round(100.0 * covered / total, 2) if total else None


def _get(obj: Any, name: str, default: Any = None) -> Any:
    """Read a field from the kit's CommandResult or a plain dict."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


# --- coverage reports --------------------------------------------------------

def _blank() -> dict[str, Any]:
    return {"lines_total": 0, "lines_covered": 0, "branches_total": 0,
            "branches_covered": 0, "missing_lines": []}


def _parse_cobertura(root: ET.Element) -> dict[str, dict]:
    files: dict[str, dict] = {}
    seen: dict[str, dict[int, tuple[bool, int, int]]] = {}
    for cls in root.iter("class"):
        name = _norm(cls.get("filename") or "")
        if not name:
            continue
        lines = seen.setdefault(name, {})
        for line in cls.iter("line"):
            try:
                num, hits = int(line.get("number", "0")), int(line.get("hits", "0"))
            except ValueError:
                continue
            btotal = bcov = 0
            m = re.search(r"\((\d+)/(\d+)\)", line.get("condition-coverage") or "")
            if line.get("branch") == "true" and m:
                bcov, btotal = int(m.group(1)), int(m.group(2))
            prev = lines.get(num)
            if prev:  # the same line listed twice (inner classes): merge
                hits_any = prev[0] or hits > 0
                lines[num] = (hits_any, max(prev[1], btotal), max(prev[2], bcov))
            else:
                lines[num] = (hits > 0, btotal, bcov)
    for name, lines in seen.items():
        rec = _blank()
        for num in sorted(lines):
            hit, btotal, bcov = lines[num]
            rec["lines_total"] += 1
            rec["lines_covered"] += 1 if hit else 0
            rec["branches_total"] += btotal
            rec["branches_covered"] += bcov
            if not hit:
                rec["missing_lines"].append(num)
        files[name] = rec
    return files


def _parse_jacoco(root: ET.Element) -> dict[str, dict]:
    files: dict[str, dict] = {}
    for pkg in root.iter("package"):
        prefix = _norm(pkg.get("name") or "")
        for src in pkg.findall("sourcefile"):
            name = f"{prefix}/{src.get('name')}" if prefix else _norm(src.get("name") or "")
            rec = _blank()
            for line in src.findall("line"):
                mi, ci = int(line.get("mi", 0)), int(line.get("ci", 0))
                mb, cb = int(line.get("mb", 0)), int(line.get("cb", 0))
                if mi + ci == 0:
                    continue
                rec["lines_total"] += 1
                if ci > 0:
                    rec["lines_covered"] += 1
                else:
                    rec["missing_lines"].append(int(line.get("nr", 0)))
                rec["branches_total"] += mb + cb
                rec["branches_covered"] += cb
            files[name] = rec
    return files


def _parse_lcov(text: str) -> dict[str, dict]:
    files: dict[str, dict] = {}
    current: str | None = None
    lines: dict[int, int] = {}
    branches: dict[tuple[str, str, str], int] = {}
    summary: dict[str, int] = {}

    def flush() -> None:
        if current is None:
            return
        rec = files.setdefault(current, _blank())
        if lines:
            rec["lines_total"] = len(lines)
            rec["lines_covered"] = sum(1 for h in lines.values() if h > 0)
            rec["missing_lines"] = sorted(n for n, h in lines.items() if h == 0)
        else:  # summary-only record
            rec["lines_total"], rec["lines_covered"] = summary.get("LF", 0), summary.get("LH", 0)
        if branches:
            rec["branches_total"] = len(branches)
            rec["branches_covered"] = sum(1 for t in branches.values() if t > 0)
        else:
            rec["branches_total"], rec["branches_covered"] = summary.get("BRF", 0), summary.get("BRH", 0)

    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("SF:"):
            current, lines, branches, summary = _norm(line[3:]), {}, {}, {}
        elif line.startswith("DA:") and current:
            parts = line[3:].split(",")
            try:
                num, hits = int(parts[0]), int(float(parts[1]))
            except (ValueError, IndexError):
                continue
            lines[num] = max(lines.get(num, 0), hits)
        elif line.startswith("BRDA:") and current:
            parts = line[5:].split(",")
            if len(parts) == 4:
                taken = 0 if parts[3] in ("-", "") else int(float(parts[3]))
                key = (parts[0], parts[1], parts[2])
                branches[key] = max(branches.get(key, 0), taken)
        elif line[:3] in ("LF:", "LH:") or line[:4] in ("BRF:", "BRH:"):
            key, _, val = line.partition(":")
            if val.strip().isdigit():
                summary[key] = int(val)
        elif line == "end_of_record":
            flush()
            current = None
    flush()
    return files


def _parse_go_profile(text: str) -> dict[str, dict]:
    # file:startLine.startCol,endLine.endCol numStatements hitCount
    blocks: dict[tuple[str, str], tuple[int, int]] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("mode:"):
            continue
        m = re.match(r"^(.+):(\d+\.\d+,\d+\.\d+) (\d+) (\d+)$", line)
        if not m:
            continue
        key = (_norm(m.group(1)), m.group(2))
        stmts, count = int(m.group(3)), int(m.group(4))
        prev = blocks.get(key)
        blocks[key] = (stmts, max(count, prev[1] if prev else 0))
    files: dict[str, dict] = {}
    for (name, span), (stmts, count) in sorted(blocks.items()):
        rec = files.setdefault(name, _blank())
        rec["lines_total"] += stmts  # Go reports statements, not lines
        if count > 0:
            rec["lines_covered"] += stmts
        else:
            rec["missing_lines"].append(int(span.split(".")[0]))
    return files


def detect_format(text: str) -> str:
    head = text.lstrip()[:4000]
    if head.startswith("mode:"):
        return "go"
    if "SF:" in head and ("end_of_record" in text or "DA:" in head):
        return "lcov"
    if "<report" in head and ("<sourcefile" in text or "JACOCO" in head.upper()):
        return "jacoco"
    if "<coverage" in head:
        return "cobertura"
    raise ToolError("unrecognized coverage format (expected Cobertura, JaCoCo, LCOV or Go)")


def summarize_coverage(text: str, fmt: str = "auto",
                       include: Iterable[str] | None = None) -> dict[str, Any]:
    """Parse a coverage report into per-file counts plus totals."""
    fmt = detect_format(text) if fmt in ("auto", "", None) else fmt
    if fmt == "cobertura":
        files = _parse_cobertura(_parse_xml(text, "coverage report"))
    elif fmt == "jacoco":
        files = _parse_jacoco(_parse_xml(text, "coverage report"))
    elif fmt == "lcov":
        files = _parse_lcov(text)
    elif fmt == "go":
        files = _parse_go_profile(text)
    else:
        raise ToolError(f"unknown coverage format: {fmt}")
    include = [p for p in (include or []) if p]
    if include:
        files = {k: v for k, v in files.items() if path_matches(k, include)}
    totals = _blank()
    del totals["missing_lines"]
    for rec in files.values():
        for key in totals:
            totals[key] += rec[key]
        rec["line_pct"] = _pct(rec["lines_covered"], rec["lines_total"])
        rec["branch_pct"] = _pct(rec["branches_covered"], rec["branches_total"])
    totals["line_pct"] = _pct(totals["lines_covered"], totals["lines_total"])
    totals["branch_pct"] = _pct(totals["branches_covered"], totals["branches_total"])
    totals["files"] = len(files)
    return {"format": fmt, "files": dict(sorted(files.items())), "totals": totals}


def parse_coverage(workspace: Path, *, fetch=None, run=None, path: str, format: str = "auto",
                   include: list[str] | None = None, out: str | None = None) -> dict:
    """Summarize a coverage report; optionally write the summary JSON to `out`."""
    summary = summarize_coverage(_read_text(workspace, path), format, include)
    summary["source"] = path
    if out:
        _write_json(workspace, out, summary)
        summary["written"] = out
    return summary


def compare_coverage(before: dict, after: dict, scope: Iterable[str] | None = None) -> dict[str, Any]:
    """Per-file and scoped deltas (percentage points) between two summaries."""
    scope = [s for s in (scope or []) if s]
    names = sorted(set(before["files"]) | set(after["files"]))
    if scope:
        names = [n for n in names if path_matches(n, scope)]
    agg = {"before": _blank(), "after": _blank()}
    per_file = []
    for name in names:
        b, a = before["files"].get(name, _blank()), after["files"].get(name, _blank())
        for side, rec in (("before", b), ("after", a)):
            for key in ("lines_total", "lines_covered", "branches_total", "branches_covered"):
                agg[side][key] += rec[key]
        per_file.append({
            "path": name,
            "line_pct_before": _pct(b["lines_covered"], b["lines_total"]),
            "line_pct_after": _pct(a["lines_covered"], a["lines_total"]),
            "branch_pct_before": _pct(b["branches_covered"], b["branches_total"]),
            "branch_pct_after": _pct(a["branches_covered"], a["branches_total"]),
        })

    def pct(side: str, kind: str) -> float | None:
        return _pct(agg[side][f"{kind}_covered"], agg[side][f"{kind}_total"])

    out: dict[str, Any] = {"scope": scope, "files": per_file}
    for kind in ("lines", "branches"):
        label = "line" if kind == "lines" else "branch"
        b, a = pct("before", kind), pct("after", kind)
        out[f"{label}_pct_before"], out[f"{label}_pct_after"] = b, a
        out[f"{label}_delta_pp"] = round(a - b, 2) if a is not None and b is not None else None
    # A shrinking denominator (deleted code, narrowed report) is a red flag.
    out["lines_total_before"] = agg["before"]["lines_total"]
    out["lines_total_after"] = agg["after"]["lines_total"]
    return out


def coverage_delta(workspace: Path, *, fetch=None, run=None, before: str, after: str,
                   scope: list[str] | None = None, out: str | None = None) -> dict:
    """Compare two coverage reports (any supported format) over a scope."""
    result = compare_coverage(summarize_coverage(_read_text(workspace, before)),
                              summarize_coverage(_read_text(workspace, after)), scope)
    result.update(before=before, after=after)
    if out:
        _write_json(workspace, out, result)
    return result


# --- test results (JUnit XML) ------------------------------------------------

def parse_junit_text(text: str, label: str = "junit") -> dict[str, str]:
    """{test_id: passed|failed|error|skipped} for one JUnit XML report."""
    root = _parse_xml(text, label)
    results: dict[str, str] = {}
    for case in root.iter("testcase"):
        cls, name = case.get("classname") or "", case.get("name") or ""
        test_id = f"{cls}::{name}" if cls else name
        if case.find("failure") is not None:
            outcome = "failed"
        elif case.find("error") is not None:
            outcome = "error"
        elif case.find("skipped") is not None:
            outcome = "skipped"
        else:
            outcome = "passed"
        # A test reported twice in one run: the worse outcome wins.
        order = ["passed", "skipped", "failed", "error"]
        prev = results.get(test_id)
        results[test_id] = outcome if prev is None or order.index(outcome) > order.index(prev) else prev
    return results


def census_from_runs(runs: list[dict[str, str]]) -> dict[str, Any]:
    """Classify tests over repeated runs: stable pass, flaky, broken, skipped."""
    ids = sorted({t for run in runs for t in run})
    flaky, broken, skipped, missing = [], [], [], []
    for t in ids:
        outcomes = [run.get(t) for run in runs]
        seen = {o for o in outcomes if o}
        bad = seen & {"failed", "error"}
        if None in outcomes:
            missing.append(t)
        if bad and "passed" in seen:
            flaky.append(t)
        elif bad:
            broken.append(t)
        elif seen == {"skipped"}:
            skipped.append(t)
    runs_failed = sum(1 for run in runs if any(o in ("failed", "error") for o in run.values()))
    return {"runs": len(runs), "tests": len(ids), "flaky": flaky, "broken": broken,
            "skipped": skipped, "missing_in_some_runs": missing, "runs_with_failures": runs_failed,
            "all_green": bool(runs) and bool(ids) and runs_failed == 0}


def _glob_files(workspace: Path, pattern: str) -> list[Path]:
    root = Path(workspace).resolve()
    _ws_path(workspace, pattern.split("*")[0] or ".")  # refuse ../ in the fixed part
    return sorted(p for p in root.glob(pattern) if p.is_file() and root in p.resolve().parents)


def parse_test_results(workspace: Path, *, fetch=None, run=None, paths: str,
                       out: str | None = None) -> dict:
    """Parse one or more JUnit XML files (a glob) into a flake census."""
    files = _glob_files(workspace, paths)
    if not files:
        raise ToolError(f"no JUnit files match {paths}")
    runs = [parse_junit_text(p.read_text(encoding="utf-8", errors="replace"), p.name) for p in files]
    census = census_from_runs(runs)
    census["files"] = [p.relative_to(Path(workspace).resolve()).as_posix() for p in files]
    if out:
        _write_json(workspace, out, census)
    return census


def run_test_matrix(workspace: Path, *, fetch=None, run: Callable | None = None,
                    argv: list[str], runs: int = 5, runs_dir: str, cwd: str = "repo",
                    base_seed: int = 1000, timeout: int | None = None) -> dict:
    """Run the suite `runs` times, each writing JUnit XML, and build a census.

    argv placeholders: {junit} -> absolute path of this run's JUnit file,
    {seed} -> base_seed + run index (for order-randomizing plugins), {run}.
    """
    if run is None:
        raise ToolError("run_test_matrix needs the kit's command runner")
    if not argv or not isinstance(argv, list):
        raise ToolError("argv must be a non-empty list")
    if not 1 <= int(runs) <= 30:
        raise ToolError("runs must be between 1 and 30")
    out_dir = _ws_path(workspace, runs_dir, write=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    work = _ws_path(workspace, cwd)
    log, results = [], []
    for i in range(1, int(runs) + 1):
        junit = out_dir / f"run-{i:02d}.xml"
        subs = {"{junit}": str(junit), "{seed}": str(base_seed + i), "{run}": str(i)}
        args = [_sub_all(str(a), subs) for a in argv]
        res = run(args, cwd=str(work), timeout=timeout)
        entry = {"run": i, "seed": base_seed + i, "exit_code": _get(res, "exit_code"),
                 "timed_out": bool(_get(res, "timed_out", False)), "junit": junit.name}
        if junit.is_file():
            results.append(parse_junit_text(junit.read_text(encoding="utf-8", errors="replace"), junit.name))
        else:
            entry["error"] = "no JUnit file written"
        log.append(entry)
    census = census_from_runs(results)
    census["log"] = log
    _write_json(workspace, f"{runs_dir.rstrip('/')}/matrix.json", census)
    return census


def _sub_all(arg: str, subs: dict[str, str]) -> str:
    for key, val in subs.items():
        arg = arg.replace(key, val)
    return arg


# --- diffs -------------------------------------------------------------------

def parse_patch(text: str) -> list[dict[str, Any]]:
    """Files in a unified (git) diff with added/removed counts and added lines."""
    files: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    in_hunk = False
    for line in text.splitlines():
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.+?) b/(.+)$", line)
            cur = {"old": m.group(1) if m else "", "new": m.group(2) if m else "",
                   "added": 0, "removed": 0, "added_lines": [], "binary": False}
            files.append(cur)
            in_hunk = False
        elif cur is None and line.startswith("--- "):
            # plain diff without "diff --git" headers
            cur = {"old": "", "new": "", "added": 0, "removed": 0, "added_lines": [], "binary": False}
            files.append(cur)
            cur["old"] = _strip_prefix(line[4:])
        elif cur is not None and line.startswith("--- ") and not in_hunk:
            cur["old"] = _strip_prefix(line[4:])
        elif cur is not None and line.startswith("+++ ") and not in_hunk:
            cur["new"] = _strip_prefix(line[4:])
        elif cur is not None and line.startswith("rename from "):
            cur["old"] = line[len("rename from "):]
        elif cur is not None and line.startswith("rename to "):
            cur["new"] = line[len("rename to "):]
        elif cur is not None and (line.startswith("Binary files") or line == "GIT binary patch"):
            cur["binary"] = True
        elif line.startswith("@@"):
            in_hunk = True
        elif cur is not None and in_hunk:
            if line.startswith("+"):
                cur["added"] += 1
                cur["added_lines"].append(line[1:])
            elif line.startswith("-"):
                cur["removed"] += 1
            elif line.startswith("diff ") or line.startswith("--- "):
                in_hunk = False
    for f in files:
        f["old"], f["new"] = _norm(f["old"]), _norm(f["new"])
        f["path"] = f["new"] if f["new"] != "/dev/null" else f["old"]
        f["status"] = ("added" if f["old"] == "/dev/null" else
                       "deleted" if f["new"] == "/dev/null" else
                       "renamed" if f["old"] != f["new"] else "modified")
    return files


def _strip_prefix(p: str) -> str:
    p = p.split("\t")[0].strip()
    if p.startswith(("a/", "b/")):
        return p[2:]
    return p


def scope_report(files: list[dict[str, Any]], test_globs: list[str] | None = None,
                 allow: list[str] | None = None) -> dict[str, Any]:
    globs = list(test_globs or DEFAULT_TEST_GLOBS)
    allow = list(allow or [])
    outside = []
    for f in files:
        # A rename out of a production path also changes production code.
        touched = {f["path"]} | ({f["old"]} if f["old"] not in ("", "/dev/null") else set())
        for p in touched:
            if not (path_matches(p, globs) or path_matches(p, allow)):
                outside.append(p)
    return {
        "files": [{"path": f["path"], "status": f["status"], "added": f["added"],
                   "removed": f["removed"], "binary": f["binary"]} for f in files],
        "files_changed": len(files),
        "lines_added": sum(f["added"] for f in files),
        "lines_removed": sum(f["removed"] for f in files),
        "outside_test_paths": sorted(set(outside)),
        "test_only": bool(files) and not outside,
    }


def diff_scope(workspace: Path, *, fetch=None, run=None, patch: str,
               test_globs: list[str] | None = None, allow: list[str] | None = None) -> dict:
    """Which files a patch touches, its size, and anything outside test paths."""
    return scope_report(parse_patch(_read_text(workspace, patch)), test_globs, allow)


def export_patch(workspace: Path, *, fetch=None, run: Callable | None = None, out: str,
                 base: str = "HEAD", cwd: str = "repo") -> dict:
    """Write `git diff <base>` (including new files) of repo/ to `out`."""
    if run is None:
        raise ToolError("export_patch needs the kit's command runner")
    if not re.fullmatch(r"[A-Za-z0-9._/~^-]{1,100}", base) or base.startswith("-"):
        raise ToolError("invalid base revision")
    work = str(_ws_path(workspace, cwd))
    # Mark untracked files intent-to-add so they appear in the diff.
    run(["git", "add", "-N", "."], cwd=work)
    res = run(["git", "diff", "--no-color", "--no-ext-diff", base], cwd=work)
    if _get(res, "exit_code") != 0:
        raise ToolError(f"git diff failed: {(_get(res, 'stderr') or '')[:500]}")
    text = _get(res, "stdout") or ""
    target = _ws_path(workspace, out, write=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    report = scope_report(parse_patch(text))
    report["written"] = out
    return report


# --- assertion-free tests ----------------------------------------------------

_ASSERT_CALL = re.compile(r"^(assert|verify|expect|check_that|fail)", re.I)


def _py_has_assertion(fn: ast.AST, helpers: set[str]) -> tuple[bool, str]:
    trivial = False
    for node in ast.walk(fn):
        if isinstance(node, ast.Assert):
            if isinstance(node.test, ast.Constant) and bool(node.test.value):
                trivial = True
                continue
            if (isinstance(node.test, ast.Compare) and len(node.test.comparators) == 1
                    and ast.dump(node.test.left) == ast.dump(node.test.comparators[0])):
                trivial = True  # assert x == x
                continue
            return True, ""
        if isinstance(node, ast.Raise):
            return True, ""  # explicit failure path (e.g. raise AssertionError)
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in helpers or _ASSERT_CALL.match(name or ""):
                return True, ""
            if name in ("raises", "warns", "deprecated_call"):
                return True, ""  # pytest.raises / warns as a call or context manager
    return False, "only trivially-true asserts" if trivial else "no assertion"


def _python_assertion_free(source: str, names: set[str] | None, helpers: set[str]) -> list[dict]:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [{"test": "<module>", "reason": f"syntax error: {exc.msg}"}]
    found = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            if names is not None and node.name not in names:
                continue
            ok, reason = _py_has_assertion(node, helpers)
            if not ok:
                found.append({"test": node.name, "reason": reason, "line": node.lineno})
    return found


# Block starts for other ecosystems, and what counts as an assertion there.
_BLOCKS = {
    "js": (re.compile(r"^\s*(?:it|test)(?:\.\w+)?\(\s*['\"`](.+?)['\"`]", re.M),
           re.compile(r"\bexpect\s*\(|\bassert\w*\s*[.(]|\.should\b|toThrow")),
    "java": (re.compile(r"@Test\b[^{]*?\b(?:void\s+)?(\w+)\s*\([^)]*\)[^{]*\{", re.S),
             re.compile(r"\bassert\w*\s*\(|\bverify\s*\(|\bfail\s*\(")),
    "go": (re.compile(r"^func\s+(Test\w*)\s*\(\s*\w+\s+\*testing\.T\s*\)", re.M),
           re.compile(r"\bt\.(?:Error|Errorf|Fatal|Fatalf|Fail|FailNow)\b|\b(?:assert|require)\.\w+\(")),
}
_EXT = {".js": "js", ".jsx": "js", ".ts": "js", ".tsx": "js", ".mjs": "js",
        ".java": "java", ".kt": "java", ".go": "go"}


def _generic_assertion_free(source: str, lang: str, names: set[str] | None) -> list[dict]:
    start_re, assert_re = _BLOCKS[lang]
    starts = list(start_re.finditer(source))
    found = []
    for i, m in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(source)
        name = m.group(1)
        if names is not None and name not in names:
            continue
        body = source[m.end():end]
        if not assert_re.search(body):
            found.append({"test": name, "reason": "no assertion",
                          "line": source.count("\n", 0, m.start()) + 1})
    return found


def assertion_free_in_source(path: str, source: str, names: set[str] | None = None,
                             helpers: Iterable[str] = ()) -> list[dict]:
    ext = Path(path).suffix.lower()
    if ext == ".py":
        found = _python_assertion_free(source, names, set(helpers))
    elif ext in _EXT:
        found = _generic_assertion_free(source, _EXT[ext], names)
    else:
        return []
    for item in found:
        item["file"] = path
    return found


def _added_test_names(added_lines: list[str]) -> set[str]:
    names = set()
    for line in added_lines:
        for pat in (r"^\s*(?:async\s+)?def\s+(test\w*)", r"^func\s+(Test\w*)",
                    r"^\s*(?:it|test)(?:\.\w+)?\(\s*['\"`](.+?)['\"`]",
                    r"^\s*(?:public\s+|private\s+|protected\s+)?(?:\w+\s+)?void\s+(\w+)\s*\("):
            m = re.match(pat, line)
            if m:
                names.add(m.group(1))
    return names


def find_assertion_free_tests(workspace: Path, *, fetch=None, run=None,
                              patch: str | None = None, paths: list[str] | None = None,
                              repo_dir: str = "repo", helpers: list[str] | None = None,
                              test_globs: list[str] | None = None) -> dict:
    """Tests with no real assertion: in files (`paths`) or tests a patch adds."""
    helpers = list(helpers or [])
    found: list[dict] = []
    checked = 0
    if patch:
        globs = list(test_globs or DEFAULT_TEST_GLOBS)
        for f in parse_patch(_read_text(workspace, patch)):
            if f["status"] == "deleted" or not path_matches(f["path"], globs):
                continue
            names = None if f["status"] == "added" else _added_test_names(f["added_lines"])
            if names is not None and not names:
                continue
            on_disk = _ws_path(workspace, f"{repo_dir}/{f['path']}")
            if on_disk.is_file():
                source = on_disk.read_text(encoding="utf-8", errors="replace")
            elif f["status"] == "added":
                source = "\n".join(f["added_lines"]) + "\n"
            else:
                found.append({"file": f["path"], "test": "<file>", "reason": "file missing in repo/"})
                continue
            checked += 1
            found += assertion_free_in_source(f["path"], source, names, helpers)
    for rel in paths or []:
        checked += 1
        found += assertion_free_in_source(rel, _read_text(workspace, rel), None, helpers)
    return {"files_checked": checked, "assertion_free": found, "clean": not found}


# --- mutation reports --------------------------------------------------------

DETECTED = {"Killed", "Timeout"}
UNDETECTED = {"Survived", "NoCoverage"}


def summarize_mutation(data: dict, scope: Iterable[str] | None = None) -> dict[str, Any]:
    """Mutation score from a mutation-testing-elements style report.

    score = detected / (detected + undetected); compile/runtime errors and
    ignored mutants are excluded from both sides.
    """
    if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
        raise ToolError("mutation report must be a JSON object with a 'files' map")
    scope = [s for s in (scope or []) if s]
    files, survivors = {}, []
    tot_d = tot_u = 0
    for name, entry in sorted(data["files"].items()):
        path = _norm(name)
        if scope and not path_matches(path, scope):
            continue
        d = u = 0
        for mut in entry.get("mutants", []) if isinstance(entry, dict) else []:
            status = mut.get("status")
            if status in DETECTED:
                d += 1
            elif status in UNDETECTED:
                u += 1
                loc = (mut.get("location") or {}).get("start") or {}
                survivors.append({"file": path, "id": mut.get("id"), "mutator": mut.get("mutatorName"),
                                  "line": loc.get("line"), "status": status})
        files[path] = {"detected": d, "undetected": u, "score_pct": _pct(d, d + u)}
        tot_d, tot_u = tot_d + d, tot_u + u
    return {"files": files, "detected": tot_d, "undetected": tot_u,
            "score_pct": _pct(tot_d, tot_d + tot_u), "survivors": survivors}


def parse_mutation_report(workspace: Path, *, fetch=None, run=None, path: str,
                          scope: list[str] | None = None, out: str | None = None) -> dict:
    """Score a mutation report and list surviving mutants (the next test targets)."""
    try:
        data = json.loads(_read_text(workspace, path))
    except json.JSONDecodeError as exc:
        raise ToolError(f"mutation report is not JSON: {exc}") from exc
    result = summarize_mutation(data, scope)
    if out:
        _write_json(workspace, out, result)
    return result


# --- risk ranking ------------------------------------------------------------

def risk_score(line_pct: float | None, lines_total: int, churn: int) -> float:
    """Uncovered share x log churn x log size: big, busy, untested files first."""
    uncovered = 1.0 - (line_pct or 0.0) / 100.0
    return round(uncovered * math.log2(2 + max(churn, 0)) * math.log2(2 + max(lines_total, 0)), 4)


def proposed_floor(line_pct: float | None) -> int:
    """Default per-module target: +20pp, rounded up to 5, capped at 90."""
    return int(min(90, math.ceil(((line_pct or 0.0) + 20) / 5.0) * 5))


def _churn_map(text: str) -> dict[str, int]:
    rows = csv.DictReader(io.StringIO(text))
    if not rows.fieldnames or not {"path", "commits"} <= set(rows.fieldnames):
        raise ToolError("churn CSV needs columns path,commits")
    out: dict[str, int] = {}
    for row in rows:
        try:
            out[_norm(row["path"])] = int(row["commits"])
        except (TypeError, ValueError):
            continue
    return out


def ranked_targets(summary: dict, churn: dict[str, int], top_n: int | None = None) -> list[dict]:
    rows = []
    for path, rec in summary["files"].items():
        if not rec["lines_total"]:
            continue
        c = churn.get(path, 0)
        rows.append({"path": path, "lines_total": rec["lines_total"], "line_pct": rec["line_pct"],
                     "branch_pct": rec["branch_pct"], "churn": c,
                     "risk_score": risk_score(rec["line_pct"], rec["lines_total"], c),
                     "proposed_floor_pct": proposed_floor(rec["line_pct"])})
    rows.sort(key=lambda r: (-r["risk_score"], r["path"]))
    for i, row in enumerate(rows, 1):
        row["rank"] = i
    return rows[:top_n] if top_n else rows


TARGET_COLUMNS = ["rank", "path", "lines_total", "line_pct", "branch_pct", "churn",
                  "risk_score", "proposed_floor_pct"]


def rank_targets(workspace: Path, *, fetch=None, run=None, coverage: str, churn: str | None = None,
                 include: list[str] | None = None, top_n: int | None = None,
                 out: str | None = None) -> dict:
    """Risk-rank files (churn x size x uncovered share); optionally write targets CSV."""
    summary = summarize_coverage(_read_text(workspace, coverage), "auto", include)
    churn_map = _churn_map(_read_text(workspace, churn)) if churn else {}
    rows = ranked_targets(summary, churn_map, top_n)
    if out:
        target = _ws_path(workspace, out, write=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=TARGET_COLUMNS, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: "" if row[k] is None else row[k] for k in TARGET_COLUMNS})
        target.write_text(buf.getvalue(), encoding="utf-8")
    return {"targets": rows, "written": out}


def git_churn(workspace: Path, *, fetch=None, run: Callable | None = None, out: str,
              since_days: int = 180, cwd: str = "repo") -> dict:
    """Commits per file over the last N days, from `git log`, as path,commits CSV."""
    if run is None:
        raise ToolError("git_churn needs the kit's command runner")
    days = int(since_days)
    if not 1 <= days <= 3650:
        raise ToolError("since_days must be between 1 and 3650")
    res = run(["git", "log", f"--since={days}.days", "--name-only", "--pretty=format:", "--no-renames"],
              cwd=str(_ws_path(workspace, cwd)))
    if _get(res, "exit_code") != 0:
        raise ToolError(f"git log failed: {(_get(res, 'stderr') or '')[:500]}")
    counts: dict[str, int] = {}
    for line in (_get(res, "stdout") or "").splitlines():
        line = _norm(line)
        if line:
            counts[line] = counts.get(line, 0) + 1
    target = _ws_path(workspace, out, write=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    target.write_text("path,commits\n" + "".join(f"{p},{c}\n" for p, c in rows), encoding="utf-8")
    return {"files": len(rows), "written": out, "top": rows[:10]}


# --- secrets in added lines --------------------------------------------------

SECRET_PATTERNS = [
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY-----")),
    ("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("stripe_live_key", re.compile(r"\b[sr]k_live_[A-Za-z0-9]{16,}\b")),
    ("assigned_secret", re.compile(
        r"(?i)\b(?:api[_-]?key|secret|passw(?:or)?d|token|auth)\w*\s*[:=]\s*['\"]([^'\"\s]{16,})['\"]")),
]
# Values that are obviously placeholders in test code.
_PLACEHOLDER = re.compile(r"(?i)test|dummy|fake|example|placeholder|changeme|x{6,}|redacted|sample|not-a-")


def scan_lines(lines: Iterable[tuple[str, int, str]]) -> list[dict]:
    hits = []
    for path, num, text in lines:
        for kind, pat in SECRET_PATTERNS:
            m = pat.search(text)
            if not m:
                continue
            if kind == "assigned_secret" and _PLACEHOLDER.search(m.group(1)):
                continue
            hits.append({"file": path, "line": num, "kind": kind})
            break  # one finding per line; the most specific pattern is listed first
    return hits


def scan_patch_secrets(workspace: Path, *, fetch=None, run=None, patch: str) -> dict:
    """Secret-looking strings in lines a patch adds (values are never echoed)."""
    lines = []
    for f in parse_patch(_read_text(workspace, patch)):
        lines += [(f["path"], i + 1, t) for i, t in enumerate(f["added_lines"])]
    hits = scan_lines(lines)
    return {"added_lines_scanned": len(lines), "findings": hits, "clean": not hits}


# --- registry ----------------------------------------------------------------

_PATH = {"type": "string", "description": "Workspace-relative path."}
_GLOBS = {"type": "array", "items": {"type": "string"}}

TOOL_DEFS: list[dict[str, Any]] = [
    {"name": "parse_coverage", "risk": "write", "function": parse_coverage,
     "description": "Summarize a coverage report (Cobertura XML, JaCoCo XML, LCOV or Go cover "
                    "profile, auto-detected) into per-file line/branch counts, percentages and "
                    "missing lines. `include` limits to path prefixes or globs; `out` writes the "
                    "summary JSON.",
     "input_schema": {"type": "object", "required": ["path"], "properties": {
         "path": _PATH, "format": {"type": "string", "enum": ["auto", "cobertura", "jacoco", "lcov", "go"]},
         "include": _GLOBS, "out": _PATH}}},
    {"name": "coverage_delta", "risk": "write", "function": coverage_delta,
     "description": "Compare a before and an after coverage report over a scope of paths; returns "
                    "line and branch percentage-point deltas, per-file numbers and the measured "
                    "line totals (a shrinking total means code or report scope changed).",
     "input_schema": {"type": "object", "required": ["before", "after"], "properties": {
         "before": _PATH, "after": _PATH, "scope": _GLOBS, "out": _PATH}}},
    {"name": "run_test_matrix", "risk": "exec", "function": run_test_matrix,
     "description": "Run the test command N times (1-30) in repo/, each run writing JUnit XML to "
                    "runs_dir/run-NN.xml, then classify every test as stable, flaky or broken. "
                    "argv placeholders: {junit}, {seed} (vary order per run), {run}.",
     "input_schema": {"type": "object", "required": ["argv", "runs_dir"], "properties": {
         "argv": _GLOBS, "runs": {"type": "integer", "minimum": 1, "maximum": 30},
         "runs_dir": _PATH, "cwd": _PATH, "base_seed": {"type": "integer"},
         "timeout": {"type": "integer"}}}},
    {"name": "parse_test_results", "risk": "write", "function": parse_test_results,
     "description": "Parse JUnit XML files matching a glob (one file per run) into a census: "
                    "flaky tests (passed and failed across runs), broken, skipped, missing.",
     "input_schema": {"type": "object", "required": ["paths"], "properties": {
         "paths": {"type": "string", "description": "Workspace-relative glob."}, "out": _PATH}}},
    {"name": "diff_scope", "risk": "read", "function": diff_scope,
     "description": "List the files a unified diff touches with added/removed line counts and "
                    "flag every path outside the test-path globs (renames count both sides).",
     "input_schema": {"type": "object", "required": ["patch"], "properties": {
         "patch": _PATH, "test_globs": _GLOBS, "allow": _GLOBS}}},
    {"name": "export_patch", "risk": "exec", "function": export_patch,
     "description": "Write the diff of repo/ against a base revision (new files included) to a "
                    "deliverable path, and report its scope.",
     "input_schema": {"type": "object", "required": ["out"], "properties": {
         "out": _PATH, "base": {"type": "string"}, "cwd": _PATH}}},
    {"name": "find_assertion_free_tests", "risk": "read", "function": find_assertion_free_tests,
     "description": "Find tests that assert nothing (or only assert constants): every test a "
                    "patch adds, or every test in the given files. Python is parsed; JS/TS, "
                    "Java/Kotlin and Go use block heuristics.",
     "input_schema": {"type": "object", "properties": {
         "patch": _PATH, "paths": _GLOBS, "repo_dir": _PATH, "helpers": _GLOBS,
         "test_globs": _GLOBS}}},
    {"name": "parse_mutation_report", "risk": "write", "function": parse_mutation_report,
     "description": "Score a mutation-testing-elements JSON report (detected / (detected + "
                    "survived + no-coverage)) per file and overall, and list surviving mutants.",
     "input_schema": {"type": "object", "required": ["path"], "properties": {
         "path": _PATH, "scope": _GLOBS, "out": _PATH}}},
    {"name": "rank_targets", "risk": "write", "function": rank_targets,
     "description": "Risk-rank source files from a coverage report and an optional path,commits "
                    "churn CSV (score = uncovered share x log2(2+churn) x log2(2+lines)); "
                    "`out` writes the targets CSV with a proposed coverage floor per file.",
     "input_schema": {"type": "object", "required": ["coverage"], "properties": {
         "coverage": _PATH, "churn": _PATH, "include": _GLOBS,
         "top_n": {"type": "integer", "minimum": 1}, "out": _PATH}}},
    {"name": "git_churn", "risk": "exec", "function": git_churn,
     "description": "Count commits per file in repo/ over the last N days and write path,commits CSV.",
     "input_schema": {"type": "object", "required": ["out"], "properties": {
         "out": _PATH, "since_days": {"type": "integer", "minimum": 1, "maximum": 3650},
         "cwd": _PATH}}},
    {"name": "scan_patch_secrets", "risk": "read", "function": scan_patch_secrets,
     "description": "Scan the lines a patch adds for secret-looking values (private keys, cloud "
                    "and SaaS tokens, long assigned secrets); reports file and line, never the value.",
     "input_schema": {"type": "object", "required": ["patch"], "properties": {"patch": _PATH}}},
]
