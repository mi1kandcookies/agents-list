"""
specialists/upgrade_migration/checks.py - acceptance checks for the
upgrade-migration specialist.

Every check recomputes its answer from source data: the repo as it is on
disk, and the evidence the tools recorded under .agentkit/upgrade_migration/
(baseline inventory, OSV answers and advisories, registry snapshots, the test
run log, detector baselines). The model cannot write there directly, so a
deliverable that disagrees with the recomputation fails, whatever the
model's report says.

Signature: fn(workspace: Path, params: dict, *, run=None) ->
{"passed": bool | None, "details": str, "score": float | None}.
"""
from __future__ import annotations

import csv
import json
import shlex
from datetime import datetime
from pathlib import Path
from typing import Any

from specialists.upgrade_migration import tools as T

SCANNABLE = ("PyPI", "npm", "Go")
OPEN_STATUSES = {"no_fix_available", "deferred", "accepted_pending_approval",
                 "not_affected_pending_approval"}


def _result(passed: bool | None, details: str, score: float | None = None) -> dict:
    return {"passed": passed, "details": details, "score": score}


def _short(items, n: int = 8) -> str:
    items = sorted(str(i) for i in items)
    return ", ".join(items[:n]) + (f" (+{len(items) - n} more)" if len(items) > n else "")


def _read_text(workspace: Path, rel: str) -> str | None:
    try:
        path = T._inside(workspace, rel)
    except ValueError:
        return None
    return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else None


def _read_csv(workspace: Path, rel: str) -> list[dict] | None:
    text = _read_text(workspace, rel)
    if text is None:
        return None
    return list(csv.DictReader(text.splitlines()))


def _repo_deps(workspace: Path, params: dict) -> list[dict]:
    return T.collect_dependencies(T._inside(workspace, params.get("repo", "repo")))


def _baseline(workspace: Path) -> list[dict] | None:
    path = Path(workspace) / T.STATE_DIR / "baseline.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8")).get("dependencies", [])


def _unscanned(deps: list[dict], queries: dict) -> list[str]:
    return [f"{d['name']}@{d['version']}" for d in deps
            if d.get("version") and d["ecosystem"] in SCANNABLE
            and T.dep_key(d["ecosystem"], d["name"], d["version"]) not in queries]


def _versions(deps: list[dict]) -> dict[tuple, str]:
    return {(d["ecosystem"], d["name"]): d["version"] for d in deps if d.get("version")}


# --- milestone 1: inventory, findings, plan -------------------------------------

def inventory_matches_repo(workspace: Path, params: dict, *, run=None) -> dict:
    """The inventory deliverable lists exactly the dependencies in the repo."""
    rel = params.get("inventory", "deliverables/m1-assess/inventory.json")
    text = _read_text(workspace, rel)
    if text is None:
        return _result(False, f"{rel} not found")
    try:
        claimed = json.loads(text).get("dependencies", [])
        claimed_set = {(d["ecosystem"], T.normalize_name(d["ecosystem"], d["name"]), d.get("version"))
                       for d in claimed}
    except (ValueError, AttributeError, KeyError, TypeError) as exc:
        return _result(False, f"{rel} is not a valid inventory: {exc}")
    actual = {(d["ecosystem"], d["name"], d.get("version")) for d in _repo_deps(workspace, params)}
    missing, extra = actual - claimed_set, claimed_set - actual
    if missing or extra:
        return _result(False, f"missing from inventory: {_short(missing) or 'none'}; "
                              f"not in repo: {_short(extra) or 'none'}",
                       len(actual & claimed_set) / max(len(actual), 1))
    return _result(True, f"{len(actual)} dependencies match the repo", 1.0)


def _expected_findings(workspace: Path, params: dict) -> tuple[list[dict], list[str]]:
    deps = _repo_deps(workspace, params)
    queries, vulns = T.load_osv_state(workspace)
    return T.findings_for(deps, queries, vulns), _unscanned(deps, queries)


def findings_match_osv(workspace: Path, params: dict, *, run=None) -> dict:
    """findings.csv equals the findings recomputed from the recorded OSV
    evidence: same (package, version, advisory) rows and same severities."""
    rel = params.get("findings", "deliverables/m1-assess/findings.csv")
    rows = _read_csv(workspace, rel)
    if rows is None:
        return _result(False, f"{rel} not found")
    expected, unscanned = _expected_findings(workspace, params)
    if unscanned:
        return _result(False, f"dependencies never scanned against OSV: {_short(unscanned)}")
    want = {(f["ecosystem"], f["name"], f["version"], f["vuln_id"]): f["severity"] for f in expected}
    try:
        got = {(r["ecosystem"], T.normalize_name(r["ecosystem"], r["name"]), r["version"], r["vuln_id"]):
               (r.get("severity") or "").upper() for r in rows}
    except KeyError as exc:
        return _result(False, f"{rel} lacks column {exc}")
    missing = set(want) - set(got)
    forged = set(got) - set(want)
    wrong_sev = [f"{k[3]} ({got[k]} != {want[k]})" for k in set(want) & set(got) if got[k] != want[k]]
    if missing or forged or wrong_sev:
        return _result(False, f"missing: {_short(k[3] for k in missing) or 'none'}; "
                              f"not supported by OSV evidence: {_short(k[3] for k in forged) or 'none'}; "
                              f"severity mismatch: {_short(wrong_sev) or 'none'}",
                       len(set(want) & set(got)) / max(len(want), 1))
    return _result(True, f"{len(want)} findings match the OSV evidence", 1.0)


def plan_covers_findings(workspace: Path, params: dict, *, run=None) -> dict:
    """Every recomputed advisory (by id or alias) is addressed in the plan."""
    rel = params.get("plan", "deliverables/m1-assess/upgrade-plan.md")
    text = _read_text(workspace, rel)
    if text is None:
        return _result(False, f"{rel} not found")
    expected, unscanned = _expected_findings(workspace, params)
    if unscanned:
        return _result(False, f"dependencies never scanned against OSV: {_short(unscanned)}")
    ids = {f["vuln_id"]: [f["vuln_id"], *f["aliases"]] for f in expected}
    uncovered = [vid for vid, names in ids.items() if not any(n in text for n in names)]
    score = (len(ids) - len(uncovered)) / len(ids) if ids else 1.0
    if uncovered:
        return _result(False, f"advisories not addressed in the plan: {_short(uncovered)}", score)
    return _result(True, f"all {len(ids)} advisories addressed", score)


# --- milestones 2/3: tests, OSV delta, diff scope ----------------------------------

def _test_argv(workspace: Path, params: dict) -> list[str] | None:
    if params.get("argv"):
        return list(params["argv"])
    intake = _read_text(workspace, "inputs/intake.json")
    if intake:
        try:
            cmd = json.loads(intake).get("test_command")
        except (ValueError, AttributeError):
            cmd = None
        if cmd:
            return list(cmd) if isinstance(cmd, list) else shlex.split(cmd)
    repo = T._inside(workspace, params.get("cwd", "repo"))
    if (repo / "pytest.ini").exists() or (repo / "pyproject.toml").exists() or (repo / "tests").is_dir():
        return ["python", "-m", "pytest", "-q"]
    if (repo / "package.json").exists():
        return ["npm", "test", "--silent"]
    if (repo / "go.mod").exists():
        return ["go", "test", "./..."]
    return None


def tests_pass(workspace: Path, params: dict, *, run=None) -> dict:
    """Re-run the suite now. Green, and (by default) at least as many passing
    tests as the first logged run, so deleting tests cannot turn it green."""
    if run is None:
        return _result(None, "pending: needs a command runner to re-execute the tests")
    argv = _test_argv(workspace, params)
    if not argv:
        return _result(False, "no test command: set test_command in intake or argv in params")
    cwd = str(T._inside(workspace, params.get("cwd", "repo")))
    res = run(argv, cwd=cwd, timeout=params.get("timeout"))
    counts = T.parse_test_summary((res.stdout or "") + "\n" + (res.stderr or ""))
    total = counts["passed"] + counts["failed"] + counts["errors"]
    score = counts["passed"] / total if total else None
    if getattr(res, "timed_out", False):
        return _result(False, f"{' '.join(argv)} timed out", score)
    if res.exit_code != 0 or counts["failed"] or counts["errors"]:
        return _result(False, f"{' '.join(argv)} exit {res.exit_code}; {counts}", score)
    runs = T.load_test_runs(workspace)
    if params.get("compare_baseline", True) and runs:
        base = next((r for r in runs if r.get("label") == "baseline"), runs[0])
        if counts["passed"] < base["counts"]["passed"]:
            return _result(False, f"{counts['passed']} tests pass now, {base['counts']['passed']} "
                                  f"passed in {base['id']} ({base.get('label') or 'first run'})", score)
    minimum = int(params.get("min_passed", 1))
    if counts["passed"] < minimum:
        return _result(False, f"only {counts['passed']} passing tests (need {minimum})", score)
    return _result(True, f"{' '.join(argv)}: {counts}", score)


def _open_findings(workspace: Path, deps: list[dict]) -> set[tuple]:
    queries, vulns = T.load_osv_state(workspace)
    return {(f["ecosystem"], f["name"], f["vuln_id"]) for f in T.findings_for(deps, queries, vulns)}


def osv_delta(workspace: Path, params: dict, *, run=None) -> dict:
    """Advisories open at baseline vs now, both recomputed: no new ones, and
    at least `min_resolved` resolved. Every current version must be scanned."""
    base = _baseline(workspace)
    if base is None:
        return _result(False, "no baseline inventory recorded (run inventory_dependencies first)")
    current = _repo_deps(workspace, params)
    queries, _ = T.load_osv_state(workspace)
    unscanned = _unscanned(current, queries) + _unscanned(base, queries)
    if unscanned:
        return _result(False, f"versions never scanned against OSV: {_short(unscanned)}")
    before, after = _open_findings(workspace, base), _open_findings(workspace, current)
    new, resolved = after - before, before - after
    score = len(resolved) / len(before) if before else 1.0
    detail = f"baseline {len(before)} open, now {len(after)}; resolved {len(resolved)}"
    if new and not params.get("allow_new", False):
        return _result(False, f"{detail}; NEW advisories: {_short(v[2] for v in new)}", score)
    minimum = int(params.get("min_resolved", 1))
    if len(resolved) < minimum:
        return _result(False, f"{detail}; need at least {minimum} resolved", score)
    return _result(True, f"{detail}: {_short(v[2] for v in resolved)}", score)


def diff_scope(workspace: Path, params: dict, *, run=None) -> dict:
    """The patch stays in scope and keeps the test suite intact."""
    rel = params.get("patch", "")
    text = _read_text(workspace, rel)
    if text is None:
        return _result(False, f"{rel} not found")
    rep = T.audit_patch_text(text, allow=params.get("allow"), forbid=params.get("forbid"))
    if not rep["files"]:
        return _result(False, f"{rel} changes no files")
    problems = []
    if rep["skips_added"]:
        problems.append(f"skip/xfail markers added: {_short(rep['skips_added'], 4)}")
    if rep["test_files_deleted"]:
        problems.append(f"test files deleted: {_short(rep['test_files_deleted'])}")
    if rep["net_tests"] < 0:
        problems.append(f"{-rep['net_tests']} test definitions removed")
    if rep["outside_allow"]:
        problems.append(f"outside allowed paths: {_short(rep['outside_allow'])}")
    if rep["forbidden"]:
        problems.append(f"forbidden paths: {_short(rep['forbidden'])}")
    limit = params.get("max_changed_lines")
    if limit is not None and rep["changed_lines"] > int(limit):
        problems.append(f"{rep['changed_lines']} changed lines (limit {limit}, lockfiles excluded)")
    summary = f"{len(rep['files'])} files, {rep['changed_lines']} lines (+{rep['lockfile_lines']} lockfile)"
    return _result(not problems, f"{summary}; " + "; ".join(problems) if problems else summary)


def _is_subsequence(needles: list[str], haystack: list[str]) -> bool:
    it = iter(haystack)
    return all(any(line == n for line in it) for n in needles)


def patch_matches_repo(workspace: Path, params: dict, *, run=None) -> dict:
    """The submitted patch describes the repo on disk: deleted files are
    gone and every added line is present, in order, in the current file."""
    rel = params.get("patch", "")
    text = _read_text(workspace, rel)
    if text is None:
        return _result(False, f"{rel} not found")
    files = T.parse_unified_diff(text)
    if not files:
        return _result(False, f"{rel} changes no files")
    repo = T._inside(workspace, params.get("repo", "repo"))
    bad = []
    for f in files:
        try:
            target = T._inside(repo, f["path"])
        except ValueError:
            bad.append(f"{f['path']} (escapes repo)")
            continue
        if f["status"] == "deleted":
            if target.exists():
                bad.append(f"{f['path']} (patch deletes it, repo still has it)")
            continue
        if not target.is_file():
            bad.append(f"{f['path']} (missing)")
            continue
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        if not _is_subsequence(f["added"], lines):
            bad.append(f"{f['path']} (added lines not in the file)")
    ok = len(files) - len(bad)
    if bad:
        return _result(False, f"patch does not match repo: {_short(bad, 5)}", ok / len(files))
    return _result(True, f"{len(files)} files match the repo", 1.0)


def upgrade_log_verified(workspace: Path, params: dict, *, run=None) -> dict:
    """Each logged step chains baseline -> current version, every version
    change in the repo is logged, and each step cites a green test run from
    the tool-owned log, in order."""
    rel = params.get("log", "deliverables/m2-upgrade/upgrade-log.json")
    text = _read_text(workspace, rel)
    if text is None:
        return _result(False, f"{rel} not found")
    base = _baseline(workspace)
    if base is None:
        return _result(False, "no baseline inventory recorded")
    try:
        steps = json.loads(text)["steps"]
        assert isinstance(steps, list) and steps
    except (ValueError, KeyError, TypeError, AssertionError):
        return _result(False, f"{rel} needs a non-empty 'steps' list")
    before, now = _versions(base), _versions(_repo_deps(workspace, params))
    runs = {r["id"]: (i, r) for i, r in enumerate(T.load_test_runs(workspace))}
    problems, by_pkg, last_run = [], {}, -1
    for n, step in enumerate(steps, 1):
        try:
            key = (step["ecosystem"], T.normalize_name(step["ecosystem"], step["name"]))
            frm, to, run_id = str(step["from"]), str(step["to"]), step.get("test_run")
        except (KeyError, TypeError):
            problems.append(f"step {n}: needs ecosystem, name, from, to, test_run")
            continue
        by_pkg.setdefault(key, []).append((frm, to))
        rec = runs.get(run_id)
        if rec is None:
            problems.append(f"step {n}: test run {run_id!r} not in the test log")
        else:
            idx, entry = rec
            if entry["exit_code"] != 0 or entry["counts"]["failed"] or entry.get("timed_out"):
                problems.append(f"step {n}: {run_id} was not green")
            if idx <= last_run:
                problems.append(f"step {n}: {run_id} is not after the previous step's run")
            last_run = max(last_run, idx)
    for key, chain in by_pkg.items():
        if before.get(key) != chain[0][0]:
            problems.append(f"{key[1]}: first step from {chain[0][0]}, baseline has {before.get(key)}")
        if now.get(key) != chain[-1][1]:
            problems.append(f"{key[1]}: last step to {chain[-1][1]}, repo has {now.get(key)}")
        for (_, a), (b, _) in zip(chain, chain[1:]):
            if a != b:
                problems.append(f"{key[1]}: steps do not chain ({a} -> {b})")
    changed = {k for k in set(before) & set(now) if before[k] != now[k]}
    unlogged = changed - set(by_pkg)
    if unlogged:
        problems.append(f"version changes not in the log: {_short(k[1] for k in unlogged)}")
    if problems:
        return _result(False, "; ".join(problems[:10]), 1 - min(len(problems), len(steps)) / len(steps))
    return _result(True, f"{len(steps)} steps verified against baseline, repo and test log", 1.0)


def release_age_ok(workspace: Path, params: dict, *, run=None) -> dict:
    """Every upgraded PyPI/npm version was at least `min_age_days` old when
    its registry history was recorded, and is not yanked/deprecated."""
    base = _baseline(workspace)
    if base is None:
        return _result(False, "no baseline inventory recorded")
    min_age = int(params.get("min_age_days", 7))
    before, now = _versions(base), _versions(_repo_deps(workspace, params))
    upgraded = {k: v for k, v in now.items() if before.get(k) != v and k[0] in ("PyPI", "npm")}
    problems = []
    for (eco, name), version in sorted(upgraded.items()):
        snap = None
        for cand in (name, name.replace("-", "_")):
            path = Path(workspace) / T.STATE_DIR / "registry" / f"{eco}__{T._safe_id(cand)}.json"
            if path.is_file():
                snap = json.loads(path.read_text(encoding="utf-8"))
                break
        if snap is None:
            reg_dir = Path(workspace) / T.STATE_DIR / "registry"
            for path in sorted(reg_dir.glob(f"{eco}__*.json")) if reg_dir.is_dir() else []:
                if T.normalize_name(eco, path.stem.split("__", 1)[1]) == name:
                    snap = json.loads(path.read_text(encoding="utf-8"))
        if snap is None:
            problems.append(f"{name} {version}: no registry history recorded")
            continue
        info = next((v for v in snap.get("versions", []) if v["version"] == version), None)
        if info is None:
            problems.append(f"{name} {version}: not in the registry history")
            continue
        try:
            released = datetime.fromisoformat(info["released"].replace("Z", "+00:00"))
            seen = datetime.fromisoformat(snap["retrieved_at"].replace("Z", "+00:00"))
            if released.tzinfo is None:
                released = released.replace(tzinfo=seen.tzinfo)
            age = (seen - released).days
        except (KeyError, ValueError):
            problems.append(f"{name} {version}: unreadable release date")
            continue
        if age < min_age:
            problems.append(f"{name} {version}: {age} days old (< {min_age})")
        if info.get("yanked"):
            problems.append(f"{name} {version}: yanked/deprecated")
    if problems:
        return _result(False, "; ".join(problems[:10]), 1 - len(problems) / max(len(upgraded), 1))
    return _result(True, f"{len(upgraded)} upgraded versions meet the {min_age}-day release age", 1.0)


def new_dependencies_disclosed(workspace: Path, params: dict, *, run=None) -> dict:
    """Direct dependencies added since baseline are named in the report, so
    the customer can approve them (a new package is a supply-chain decision)."""
    rel = params.get("report", "")
    text = _read_text(workspace, rel)
    if text is None:
        return _result(False, f"{rel} not found")
    base = _baseline(workspace)
    if base is None:
        return _result(False, "no baseline inventory recorded")
    known = {(d["ecosystem"], d["name"]) for d in base}
    added = {(d["ecosystem"], d["name"]) for d in _repo_deps(workspace, params)
             if d.get("direct") and (d["ecosystem"], d["name"]) not in known}
    lower = text.lower()
    missing = [n for _, n in added if n.lower() not in lower]
    if missing:
        return _result(False, f"new direct dependencies not disclosed: {_short(missing)}")
    return _result(True, f"{len(added)} new direct dependencies, all disclosed")


def detectors_cleared(workspace: Path, params: dict, *, run=None) -> dict:
    """Old-API detectors: each one was recorded with a non-zero baseline
    (same regex and glob) before the change and now matches nothing."""
    rel = params.get("detectors", "deliverables/m3-migrate/detectors.json")
    text = _read_text(workspace, rel)
    if text is None:
        return _result(False, f"{rel} not found")
    try:
        dets = json.loads(text)
        dets = dets.get("detectors", dets) if isinstance(dets, dict) else dets
        assert isinstance(dets, list) and dets and all(d.get("id") and d.get("regex") for d in dets)
    except (ValueError, AssertionError, AttributeError):
        return _result(False, f"{rel} must be a non-empty list of {{id, regex, glob}}")
    base_path = Path(workspace) / T.STATE_DIR / "detector_baseline.json"
    baseline = json.loads(base_path.read_text(encoding="utf-8")) if base_path.is_file() else {}
    problems = []
    for d in dets:
        b = baseline.get(d["id"])
        if b is None:
            problems.append(f"{d['id']}: never scanned before the change")
        elif b["regex"] != d["regex"] or b.get("glob", "*") != d.get("glob", "*"):
            problems.append(f"{d['id']}: regex/glob differs from the recorded baseline")
        elif b["count"] == 0:
            problems.append(f"{d['id']}: baseline found no usages, so it proves nothing")
    counts = T.count_pattern_matches(T._inside(workspace, params.get("repo", "repo")), dets)
    remaining = {k: v for k, v in counts.items() if v["count"]}
    for k, v in remaining.items():
        problems.append(f"{k}: {v['count']} usages left ({_short(v['sites'], 3)})")
    cleared = sum(1 for d in dets if not counts[d["id"]]["count"])
    if problems:
        return _result(False, "; ".join(problems[:10]), cleared / len(dets))
    return _result(True, f"{len(dets)} detectors at zero", 1.0)


def residual_risks_registered(workspace: Path, params: dict, *, run=None) -> dict:
    """Every advisory still open in the repo has a register row with a
    justification and a pending status; nothing is self-approved; no row
    names an advisory that is not open."""
    rel = params.get("register", "deliverables/m3-migrate/residual-risk.csv")
    rows = _read_csv(workspace, rel)
    if rows is None:
        return _result(False, f"{rel} not found")
    deps = _repo_deps(workspace, params)
    queries, vulns = T.load_osv_state(workspace)
    unscanned = _unscanned(deps, queries)
    if unscanned:
        return _result(False, f"versions never scanned against OSV: {_short(unscanned)}")
    open_ids = {f["vuln_id"] for f in T.findings_for(deps, queries, vulns)}
    problems, listed = [], set()
    for n, row in enumerate(rows, 2):
        vid = (row.get("vuln_id") or "").strip()
        status = (row.get("status") or "").strip()
        listed.add(vid)
        if vid not in open_ids:
            problems.append(f"line {n}: {vid or '(blank)'} is not open in the repo")
        if status not in OPEN_STATUSES:
            problems.append(f"line {n}: status {status!r} (only the customer approves; use one of "
                            f"{', '.join(sorted(OPEN_STATUSES))})")
        if len((row.get("justification") or "").strip()) < 20:
            problems.append(f"line {n}: justification missing or too short")
    missing = open_ids - listed
    if missing:
        problems.append(f"open advisories not registered: {_short(missing)}")
    score = (len(open_ids) - len(missing)) / len(open_ids) if open_ids else 1.0
    if problems:
        return _result(False, "; ".join(problems[:10]), score)
    return _result(True, f"{len(open_ids)} open advisories registered", 1.0)


CHECK_DEFS: dict[str, Any] = {
    "inventory_matches_repo": inventory_matches_repo,
    "findings_match_osv": findings_match_osv,
    "plan_covers_findings": plan_covers_findings,
    "tests_pass": tests_pass,
    "osv_delta": osv_delta,
    "diff_scope": diff_scope,
    "patch_matches_repo": patch_matches_repo,
    "upgrade_log_verified": upgrade_log_verified,
    "release_age_ok": release_age_ok,
    "new_dependencies_disclosed": new_dependencies_disclosed,
    "detectors_cleared": detectors_cleared,
    "residual_risks_registered": residual_risks_registered,
}
