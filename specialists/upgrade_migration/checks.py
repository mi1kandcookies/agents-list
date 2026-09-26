"""
specialists/upgrade_migration/checks.py - acceptance checks for the
upgrade-migration specialist.

Every check recomputes its answer from source data: the repo as it is on
disk, and the evidence recorded under .agentkit/upgrade_migration/ (baseline
inventory, OSV answers and advisories, registry snapshots, the test run log
with the dependency versions each run tested, detector baselines, the
client's test command and upgrade policy, patch export records). The model
cannot write there directly, so a deliverable that disagrees with the
recomputation fails, whatever the model's report says.

Dependencies are compared as states: for each (ecosystem, name) the set of
exact versions pinned anywhere in the repo, "declared but not pinned", or
absent. An advisory only counts as resolved when the package is pinned at
versions the recorded OSV evidence clears, or its removal is logged and
backed by a green run; unpinning a vulnerable package proves nothing.

Signature: fn(workspace: Path, params: dict, *, run=None) ->
{"passed": bool | None, "details": str, "score": float | None}. Paths in
params are checked lexically against the workspace before any file is
opened (agentkit.policy.jail_path).
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from agentkit.errors import PolicyViolation
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
    except PolicyViolation:
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
    path = T.baseline_path(workspace)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8")).get("dependencies", [])


def _unscanned(deps: list[dict], queries: dict) -> list[str]:
    return [f"{d['name']}@{d['version']}" for d in deps
            if d.get("version") and d["ecosystem"] in SCANNABLE
            and T.dep_key(d["ecosystem"], d["name"], d["version"]) not in queries]


def _pkg(key: str) -> str:
    return key.split("|", 1)[1]


def _not_read(deps: list[dict]) -> str:
    unread = T.unread_manifests(deps)
    return f"; not read (other ecosystems or broken): {_short(unread)}" if unread else ""


def _cwd_rel(workspace: Path, cwd: str) -> str:
    return T._inside(workspace, cwd).relative_to(Path(workspace).resolve()).as_posix()


# --- milestone 1: inventory, findings, plan -------------------------------------

def inventory_matches_repo(workspace: Path, params: dict, *, run=None) -> dict:
    """The inventory deliverable lists exactly the dependencies in the repo
    (including the dependency files the tools could not read)."""
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
    deps = _repo_deps(workspace, params)
    actual = {(d["ecosystem"], d["name"], d.get("version")) for d in deps}
    missing, extra = actual - claimed_set, claimed_set - actual
    if missing or extra:
        return _result(False, f"missing from inventory: {_short(missing) or 'none'}; "
                              f"not in repo: {_short(extra) or 'none'}",
                       len(actual & claimed_set) / max(len(actual), 1))
    return _result(True, f"{len(actual)} dependencies match the repo{_not_read(deps)}", 1.0)


def _expected_findings(workspace: Path, params: dict) -> tuple[list[dict], list[str], list[dict]]:
    deps = _repo_deps(workspace, params)
    queries, vulns = T.load_osv_state(workspace)
    return T.findings_for(deps, queries, vulns), _unscanned(deps, queries), deps


def findings_match_osv(workspace: Path, params: dict, *, run=None) -> dict:
    """findings.csv equals the findings recomputed from the recorded OSV
    evidence: same (package, version, advisory) rows and same severities."""
    rel = params.get("findings", "deliverables/m1-assess/findings.csv")
    rows = _read_csv(workspace, rel)
    if rows is None:
        return _result(False, f"{rel} not found")
    expected, unscanned, deps = _expected_findings(workspace, params)
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
    return _result(True, f"{len(want)} findings match the OSV evidence{_not_read(deps)}", 1.0)


def plan_covers_findings(workspace: Path, params: dict, *, run=None) -> dict:
    """Every recomputed advisory (by id or alias) is addressed in the plan,
    and every dependency file the tools could not read is named in it."""
    rel = params.get("plan", "deliverables/m1-assess/upgrade-plan.md")
    text = _read_text(workspace, rel)
    if text is None:
        return _result(False, f"{rel} not found")
    expected, unscanned, deps = _expected_findings(workspace, params)
    if unscanned:
        return _result(False, f"dependencies never scanned against OSV: {_short(unscanned)}")
    ids = {f["vuln_id"]: [f["vuln_id"], *f["aliases"]] for f in expected}
    uncovered = [vid for vid, names in ids.items() if not any(n in text for n in names)]
    unnamed = [src for src in T.unread_manifests(deps) if src not in text]
    score = (len(ids) - len(uncovered)) / len(ids) if ids else 1.0
    problems = []
    if uncovered:
        problems.append(f"advisories not addressed in the plan: {_short(uncovered)}")
    if unnamed:
        problems.append(f"dependency files the tools could not read, not named in the plan: {_short(unnamed)}")
    if problems:
        return _result(False, "; ".join(problems), score)
    return _result(True, f"all {len(ids)} advisories addressed", score)


# --- milestones 2/3: tests, OSV delta, diff scope ----------------------------------

def _test_argv(workspace: Path, params: dict) -> list[str] | None:
    """params.argv, else the client's test_command (recorded from the brief
    by the specialist, or inputs/intake.json), else a guess from the repo."""
    if params.get("argv"):
        return list(params["argv"])
    recorded = T.recorded_test_command(workspace)
    if recorded:
        return recorded
    intake = _read_text(workspace, "inputs/intake.json")
    if intake:
        try:
            cmd = T.command_argv(json.loads(intake).get("test_command"))
        except (ValueError, AttributeError):
            cmd = None
        if cmd:
            return cmd
    repo = T._inside(workspace, params.get("cwd", "repo"))
    if (repo / "pytest.ini").exists() or (repo / "pyproject.toml").exists() or (repo / "tests").is_dir():
        return ["python", "-m", "pytest", "-q"]
    if (repo / "package.json").exists():
        return ["npm", "test", "--silent"]
    if (repo / "go.mod").exists():
        return ["go", "test", "./..."]
    return None


def _client_run(entry: dict, argv: list[str], cwd: str) -> bool:
    """A logged run of exactly the client's test command in the checked cwd."""
    return entry.get("argv") == argv and entry.get("cwd") == cwd


def _at_baseline(state: dict | None, base_state: dict) -> bool:
    """The run tested the baseline's exact pins. Packages the baseline left
    unpinned, and packages it did not have, are free: a lockfile the
    environment setup generated (npm install, uv sync) does not rule a run
    out."""
    return state is not None and all(state.get(k) == v for k, v in base_state.items() if v)


def _baseline_run(workspace: Path, runs: list[dict], argv: list[str], cwd: str,
                  base_state: dict) -> int | None:
    """Index of the baseline test run: the client's command, in cwd, at the
    baseline dependency pins; the one with most passing tests (earliest on a
    tie), so an early run on a half-set-up machine cannot lower it."""
    best = None
    for i, entry in enumerate(runs):
        if not (_client_run(entry, argv, cwd)
                and _at_baseline(T.load_run_state(workspace, entry), base_state)):
            continue
        if best is None or entry["counts"]["passed"] > runs[best]["counts"]["passed"]:
            best = i
    return best


def tests_pass(workspace: Path, params: dict, *, run=None) -> dict:
    """Re-run the client's suite now. Green, and (by default) no fewer passing
    and no more skipped tests than the baseline run, so deleting or skipping
    tests cannot turn it green."""
    if run is None:
        return _result(None, "pending: needs a command runner to re-execute the tests")
    argv = _test_argv(workspace, params)
    if not argv:
        return _result(False, "no test command: set test_command in intake or argv in params")
    cwd = params.get("cwd", "repo")
    cwd_rel = _cwd_rel(workspace, cwd)
    res = run(argv, cwd=cwd, timeout=params.get("timeout"))
    counts = T.parse_test_summary((res.stdout or "") + "\n" + (res.stderr or ""))
    total = counts["passed"] + counts["failed"] + counts["errors"]
    score = counts["passed"] / total if total else None
    if getattr(res, "timed_out", False):
        return _result(False, f"{' '.join(argv)} timed out", score)
    if res.exit_code != 0 or counts["failed"] or counts["errors"]:
        return _result(False, f"{' '.join(argv)} exit {res.exit_code}; {counts}", score)
    if params.get("compare_baseline", True):
        base_deps = _baseline(workspace)
        runs = T.load_test_runs(workspace)
        idx = None if base_deps is None else _baseline_run(workspace, runs, argv, cwd_rel,
                                                            T.dep_state(base_deps))
        if idx is None:
            return _result(False, f"no baseline run of `{' '.join(argv)}` in {cwd_rel}/ at the baseline "
                                  "dependency versions (run_tests it before changing anything)", score)
        base = runs[idx]
        if counts["passed"] < base["counts"]["passed"]:
            return _result(False, f"{counts['passed']} tests pass now, {base['counts']['passed']} "
                                  f"passed in {base['id']} (baseline)", score)
        if counts["skipped"] > base["counts"].get("skipped", 0):
            return _result(False, f"{counts['skipped']} tests skipped/xfailed/deselected now, "
                                  f"{base['counts'].get('skipped', 0)} in {base['id']} (baseline)", score)
    minimum = int(params.get("min_passed", 1))
    if counts["passed"] < minimum:
        return _result(False, f"only {counts['passed']} passing tests (need {minimum})", score)
    return _result(True, f"{' '.join(argv)}: {counts}", score)


def _log_steps(workspace: Path, rel: str) -> list | None:
    text = _read_text(workspace, rel)
    if text is None:
        return None
    try:
        steps = json.loads(text)["steps"]
    except (ValueError, KeyError, TypeError):
        return None
    return steps if isinstance(steps, list) else None


def _logged_removals(workspace: Path, params: dict) -> set[str]:
    """Packages ("ecosystem|name") whose removal an upgrade log in params.log
    (a path or a list of paths) records, backed by a green run of the
    client's command that no longer has the package."""
    logs = params.get("log") or []
    logs = [logs] if isinstance(logs, str) else list(logs)
    argv = _test_argv(workspace, params)
    cwd = _cwd_rel(workspace, params.get("cwd", "repo"))
    runs = {r["id"]: r for r in T.load_test_runs(workspace)}
    out = set()
    for rel in logs:
        for step in _log_steps(workspace, rel) or []:
            if not isinstance(step, dict) or str(step.get("to")) != T.REMOVED:
                continue
            try:
                key = f"{step['ecosystem']}|{T.normalize_name(step['ecosystem'], step['name'])}"
            except (KeyError, TypeError, AttributeError):
                continue
            entry = runs.get(step.get("test_run"))
            state = T.load_run_state(workspace, entry) if entry else None
            if entry and argv and _client_run(entry, argv, cwd) and T.is_green(entry) \
                    and state is not None and key not in state:
                out.add(key)
    return out


def _open_advisories(workspace: Path, base: list[dict], current: list[dict],
                     removals: set[str] = frozenset()) -> tuple[set, set, set[str], set[str]]:
    """(open at baseline, open now, unpinned, removed). Open now is
    recomputed for every pinned version, plus the baseline advisories of any
    baseline-vulnerable package that is no longer pinned (only a range is
    declared) or is gone without a logged removal: nothing proves those
    were fixed."""
    queries, vulns = T.load_osv_state(workspace)
    before = {(f["ecosystem"], f["name"], f["vuln_id"]) for f in T.findings_for(base, queries, vulns)}
    now = {(f["ecosystem"], f["name"], f["vuln_id"]) for f in T.findings_for(current, queries, vulns)}
    state = T.dep_state(current)
    unpinned, removed = set(), set()
    for eco, name, vid in before:
        key = f"{eco}|{name}"
        versions = state.get(key)
        if versions or (versions is None and key in removals):
            continue
        now.add((eco, name, vid))
        (unpinned if versions is not None else removed).add(name)
    return before, now, unpinned, removed


def osv_delta(workspace: Path, params: dict, *, run=None) -> dict:
    """Advisories open at baseline vs now, both recomputed: no new ones, at
    least `min_resolved` resolved, no vulnerable package unpinned or removed
    without a logged removal. Every current version must be scanned."""
    base = _baseline(workspace)
    if base is None:
        return _result(False, "no baseline inventory recorded")
    current = _repo_deps(workspace, params)
    queries, _ = T.load_osv_state(workspace)
    unscanned = _unscanned(current, queries) + _unscanned(base, queries)
    if unscanned:
        return _result(False, f"versions never scanned against OSV: {_short(unscanned)}")
    before, after, unpinned, removed = _open_advisories(workspace, base, current,
                                                        _logged_removals(workspace, params))
    new, resolved = after - before, before - after
    score = len(resolved) / len(before) if before else 1.0
    detail = f"baseline {len(before)} open, now {len(after)}; resolved {len(resolved)}"
    problems = []
    if new and not params.get("allow_new", False):
        problems.append(f"NEW advisories: {_short(v[2] for v in new)}")
    if unpinned:
        problems.append(f"vulnerable packages no longer pinned (their advisories stay open; pin the "
                        f"fixed version): {_short(unpinned)}")
    if removed:
        problems.append(f"vulnerable packages removed without a logged removal step backed by a green "
                        f"run (their advisories stay open): {_short(removed)}")
    minimum = int(params.get("min_resolved", 1))
    if len(resolved) < minimum:
        problems.append(f"need at least {minimum} resolved")
    if problems:
        return _result(False, f"{detail}; " + "; ".join(problems), score)
    return _result(True, f"{detail}" + (f": {_short(v[2] for v in resolved)}" if resolved else ""), score)


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
        problems.append(f"skip/xfail/deselect markers added: {_short(rep['skips_added'], 4)}")
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


def patch_generated(workspace: Path, params: dict, *, run=None) -> dict:
    """The patch deliverable is the one the specialist wrote from `git diff`
    against the milestone's base commit (not a file the model wrote), and it
    is unchanged since."""
    rel = params.get("patch", "")
    record = T.load_patch_records(workspace).get(rel)
    if not record:
        return _result(False, f"{rel} was not generated from git (no export record)")
    if record.get("error"):
        return _result(False, f"{rel} could not be generated from git: {record['error']}")
    try:
        data = T._inside(workspace, rel).read_bytes()
    except (PolicyViolation, OSError):
        return _result(False, f"{rel} not found")
    if hashlib.sha256(data).hexdigest() != record.get("sha256"):
        return _result(False, f"{rel} changed after it was generated from git")
    return _result(True, f"git diff against {str(record.get('base'))[:12]}")


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
        except PolicyViolation:
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


def _versions_text(versions: list[str] | None) -> str:
    if versions is None:
        return "absent"
    return ", ".join(versions) if versions else T.UNPINNED


def upgrade_log_verified(workspace: Path, params: dict, *, run=None) -> dict:
    """The upgrade log replays the baseline into the repo, and the test log
    proves each step: every version change of a baseline package is logged
    (unpinning and removal included), steps chain from the baseline versions
    to the repo's, and each step cites a green run of the client's test
    command, after the baseline run and after the previous step's run, with
    no fewer passing and no more skipped tests than the baseline run, whose
    recorded dependency versions show exactly this step applied and no
    later one. Consecutive steps that moved together cite the same run."""
    rel = params.get("log", "deliverables/m2-upgrade/upgrade-log.json")
    if _read_text(workspace, rel) is None:
        return _result(False, f"{rel} not found")
    base = _baseline(workspace)
    if base is None:
        return _result(False, "no baseline inventory recorded")
    steps = _log_steps(workspace, rel)
    if not steps:
        return _result(False, f"{rel} needs a non-empty 'steps' list")
    argv = _test_argv(workspace, params)
    if not argv:
        return _result(False, "no test command to verify the logged runs against")
    cwd = _cwd_rel(workspace, params.get("cwd", "repo"))
    before = T.dep_state(base)
    now = T.dep_state(_repo_deps(workspace, params))
    runs = T.load_test_runs(workspace)
    index = {r["id"]: i for i, r in enumerate(runs)}
    base_run = _baseline_run(workspace, runs, argv, cwd, before)
    if base_run is None:
        return _result(False, f"no baseline run of `{' '.join(argv)}` in {cwd}/ at the baseline dependency "
                              "versions, so no step can be shown to come after it")
    problems: list[str] = []
    parsed = []
    for n, step in enumerate(steps, 1):
        try:
            key = f"{step['ecosystem']}|{T.normalize_name(step['ecosystem'], step['name'])}"
            parsed.append((n, key, str(step["from"]), str(step["to"]), str(step.get("test_run"))))
        except (KeyError, TypeError, AttributeError):
            problems.append(f"step {n}: needs ecosystem, name, from, to, test_run")
    groups: list[tuple[Any, list]] = []          # consecutive steps citing one run
    for item in parsed:
        if groups and groups[-1][0] == item[4]:
            groups[-1][1].append(item)
        else:
            groups.append((item[4], [item]))
    logged = {item[1] for item in parsed}
    sim = {k: list(v) for k, v in before.items()}   # state the log says the repo is in
    last = base_run
    for run_id, items in groups:
        for n, key, frm, to in (i[:4] for i in items):
            cur = sim.get(key)
            if frm == T.UNPINNED and cur == []:
                pass
            elif cur is None or frm not in cur:
                problems.append(f"step {n}: {_pkg(key)} is not at {frm} before this step "
                                f"({_versions_text(cur)})")
                continue
            else:
                cur.remove(frm)
            if to == T.UNPINNED:
                problems.append(f"step {n}: {_pkg(key)} would end unpinned (pin an exact version)")
            elif to != T.REMOVED:
                cur.append(to)
            sim[key] = sorted(set(cur), key=lambda x: (T.version_key(x), x)) if cur or to != T.REMOVED \
                else None
        where = f"step {items[0][0]}" + (f"-{items[-1][0]}" if len(items) > 1 else "")
        if run_id not in index:
            problems.append(f"{where}: test run {run_id!r} not in the test log")
            continue
        i = index[run_id]
        entry = runs[i]
        if not T.is_green(entry):
            problems.append(f"{where}: {run_id} was not green")
        got, floor = entry["counts"], runs[base_run]["counts"]
        if got["passed"] < floor["passed"] or got.get("skipped", 0) > floor.get("skipped", 0):
            problems.append(f"{where}: {run_id} ran {got['passed']} passing / {got.get('skipped', 0)} skipped "
                            f"tests (baseline {runs[base_run]['id']}: {floor['passed']} / "
                            f"{floor.get('skipped', 0)})")
        if not _client_run(entry, argv, cwd):
            problems.append(f"{where}: {run_id} is not the client's test command "
                            f"(`{' '.join(argv)}` in {cwd}/)")
        if i <= last:
            problems.append(f"{where}: {run_id} is not after the "
                            + ("baseline run" if last == base_run else "previous step's run"))
        last = max(last, i)
        state = T.load_run_state(workspace, entry)
        if state is None:
            problems.append(f"{where}: {run_id} has no recorded dependency versions")
            continue
        off = [f"{_pkg(k)} ({_versions_text(state.get(k))}, log says {_versions_text(sim.get(k))})"
               for k in sorted(logged) if state.get(k) != sim.get(k)]
        if off:
            problems.append(f"{where}: {run_id} did not test the logged state: {_short(off, 4)}")
    for key in sorted(logged):
        if now.get(key) != sim.get(key):
            problems.append(f"{_pkg(key)}: log ends at {_versions_text(sim.get(key))}, "
                            f"repo has {_versions_text(now.get(key))}")
    unlogged = [_pkg(k) for k in before if now.get(k) != before[k] and k not in logged]
    if unlogged:
        problems.append(f"version changes not in the log (unpinning and removal included): {_short(unlogged)}")
    if problems:
        return _result(False, "; ".join(problems[:10]), 1 - min(len(problems), len(steps)) / len(steps))
    return _result(True, f"{len(steps)} steps in {len(groups)} test runs verified against baseline, "
                         "repo and test log", 1.0)


def release_age_ok(workspace: Path, params: dict, *, run=None) -> dict:
    """Every PyPI/npm version pinned now that was not pinned at baseline
    (each copy of a package counts) was at least the minimum release age
    old when its registry history was recorded, and is not yanked or
    deprecated. The minimum is params.min_age_days or the client's recorded
    policy, whichever is stricter."""
    base = _baseline(workspace)
    if base is None:
        return _result(False, "no baseline inventory recorded")
    min_age = max(int(params.get("min_age_days", T.DEFAULT_MIN_AGE_DAYS)), T.min_release_age(workspace))
    before, now = T.dep_state(base), T.dep_state(_repo_deps(workspace, params))
    added = sorted((key, v) for key, versions in now.items() for v in versions
                   if key.split("|", 1)[0] in ("PyPI", "npm") and v not in (before.get(key) or []))
    problems = []
    for key, version in added:
        eco, name = key.split("|", 1)
        path = T.registry_snapshot_path(workspace, eco, name)
        snap = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
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
        except (KeyError, ValueError, AttributeError):
            problems.append(f"{name} {version}: unreadable release date")
            continue
        if age < min_age:
            problems.append(f"{name} {version}: {age} days old (< {min_age})")
        if info.get("yanked"):
            problems.append(f"{name} {version}: yanked/deprecated")
    if problems:
        return _result(False, "; ".join(problems[:10]), 1 - len(problems) / max(len(added), 1))
    return _result(True, f"{len(added)} new versions meet the {min_age}-day release age", 1.0)


def new_dependencies_disclosed(workspace: Path, params: dict, *, run=None) -> dict:
    """Direct dependencies added since baseline are named in the report, so
    the customer can approve them (a new package is a supply-chain decision).
    Where a lockfile has no manifest to tell direct from transitive, every
    new package counts as direct."""
    rel = params.get("report", "")
    text = _read_text(workspace, rel)
    if text is None:
        return _result(False, f"{rel} not found")
    base = _baseline(workspace)
    if base is None:
        return _result(False, "no baseline inventory recorded")
    known = set(T.dep_state(base))
    current = _repo_deps(workspace, params)
    added = {(d["ecosystem"], d["name"]) for d in current
             if d["ecosystem"] != "unknown" and d.get("direct") and f"{d['ecosystem']}|{d['name']}" not in known}
    lower = text.lower()
    missing = [n for _, n in added if n.lower() not in lower]
    if missing:
        return _result(False, f"new direct dependencies not disclosed: {_short(missing)}")
    return _result(True, f"{len(added)} new direct dependencies, all disclosed")


def detectors_cleared(workspace: Path, params: dict, *, run=None) -> dict:
    """Old-API detectors: every detector recorded with a non-zero baseline is
    in the deliverable, either active (same regex and glob as recorded, now
    matching nothing) or superseded by an active one with a reason."""
    rel = params.get("detectors", "deliverables/m3-migrate/detectors.json")
    text = _read_text(workspace, rel)
    if text is None:
        return _result(False, f"{rel} not found")
    try:
        dets = json.loads(text)
        dets = dets.get("detectors", dets) if isinstance(dets, dict) else dets
        assert isinstance(dets, list) and all(isinstance(d, dict) and d.get("id") for d in dets)
        active = [d for d in dets if "superseded_by" not in d]
        superseded = [d for d in dets if "superseded_by" in d]
        assert active and all(d.get("regex") for d in active)
    except (ValueError, AssertionError, AttributeError):
        return _result(False, f"{rel} must be a non-empty list of {{id, regex, glob}} "
                              "(plus optional {id, superseded_by, reason})")
    base_path = Path(workspace) / T.STATE_DIR / "detector_baseline.json"
    baseline = json.loads(base_path.read_text(encoding="utf-8")) if base_path.is_file() else {}
    problems = []
    for d in active:
        b = baseline.get(d["id"])
        if b is None:
            problems.append(f"{d['id']}: never scanned before the change")
        elif b["regex"] != d["regex"] or b.get("glob", "*") != d.get("glob", "*"):
            problems.append(f"{d['id']}: regex/glob differs from the recorded baseline")
        elif b["count"] == 0:
            problems.append(f"{d['id']}: baseline found no usages, so it proves nothing")
    active_ids = {d["id"] for d in active}
    notes = []
    for d in superseded:
        if d["superseded_by"] not in active_ids:
            problems.append(f"{d['id']}: superseded_by {d['superseded_by']!r} is not an active detector")
        if len(str(d.get("reason") or "").strip()) < 10:
            problems.append(f"{d['id']}: superseded without a reason")
        if d["id"] not in baseline:
            problems.append(f"{d['id']}: superseded but never scanned")
    required = {k for k, b in baseline.items() if isinstance(b, dict) and b.get("count")}
    dropped = required - {d["id"] for d in dets}
    if dropped:
        problems.append(f"recorded detectors missing from {rel}: {_short(dropped)}")
    repo = T._inside(workspace, params.get("repo", "repo"))
    try:
        counts = T.count_pattern_matches(repo, active)
    except re.error as exc:
        return _result(False, "; ".join([*problems, f"bad regex: {exc}"][:10]), 0.0)
    for k, v in counts.items():
        if v["count"]:
            problems.append(f"{k}: {v['count']} usages left ({_short(v['sites'], 3)})")
    old = [dict(baseline[d["id"]], id=d["id"]) for d in superseded if d["id"] in baseline]
    for k, v in T.count_pattern_matches(repo, old).items():
        notes.append(f"{k} superseded ({v['count']} matches of its recorded regex left)")
    cleared = sum(1 for d in active if not counts[d["id"]]["count"])
    tail = f"; {'; '.join(notes)}" if notes else ""
    if problems:
        return _result(False, "; ".join(problems[:10]) + tail, cleared / len(active))
    return _result(True, f"{len(active)} detectors at zero{tail}", 1.0)


def residual_risks_registered(workspace: Path, params: dict, *, run=None) -> dict:
    """Every advisory still open in the repo (a vulnerable package unpinned or
    removed without a logged removal still counts) has a register row with a
    justification and a pending status; nothing is self-approved; no row
    names an advisory that is not open."""
    rel = params.get("register", "deliverables/m3-migrate/residual-risk.csv")
    rows = _read_csv(workspace, rel)
    if rows is None:
        return _result(False, f"{rel} not found")
    base = _baseline(workspace)
    if base is None:
        return _result(False, "no baseline inventory recorded")
    deps = _repo_deps(workspace, params)
    queries, _ = T.load_osv_state(workspace)
    unscanned = _unscanned(deps, queries) + _unscanned(base, queries)
    if unscanned:
        return _result(False, f"versions never scanned against OSV: {_short(unscanned)}")
    _, now, _, _ = _open_advisories(workspace, base, deps, _logged_removals(workspace, params))
    open_ids = {vid for _, _, vid in now}
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
    "patch_generated": patch_generated,
    "patch_matches_repo": patch_matches_repo,
    "upgrade_log_verified": upgrade_log_verified,
    "release_age_ok": release_age_ok,
    "new_dependencies_disclosed": new_dependencies_disclosed,
    "detectors_cleared": detectors_cleared,
    "residual_risks_registered": residual_risks_registered,
}
