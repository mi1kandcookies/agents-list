"""
specialists/test_coverage/checks.py - acceptance checks for the test-coverage
specialist.

Each check is `fn(workspace: Path, params: dict, *, run=None)` returning
{"passed": bool | None, "details": str, "score": float | None} and is listed
in CHECK_DEFS. Checks never trust the agent's own numbers: they re-parse the
raw coverage reports, JUnit run files, patches and mutation reports and
recompute every figure, so a forged summary or a quietly widened diff fails.
"""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any, Callable

from agentkit.errors import PolicyViolation, ToolError
from specialists.test_coverage import tools as T


def _result(passed: bool | None, details: str, score: float | None = None) -> dict[str, Any]:
    return {"passed": passed, "details": details, "score": score}


def _need(params: dict, *keys: str) -> None:
    missing = [k for k in keys if params.get(k) in (None, "", [])]
    if missing:
        raise ToolError(f"missing check params: {', '.join(missing)}")


def _guard(fn: Callable) -> Callable:
    """Turn missing files, refused paths and bad input into a failed check
    instead of a crash."""
    def wrapper(workspace: Path, params: dict, *, run=None) -> dict[str, Any]:
        try:
            return fn(Path(workspace), dict(params or {}), run=run)
        except (ToolError, PolicyViolation) as exc:
            return _result(False, str(exc))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return _result(False, f"{type(exc).__name__}: {exc}")
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


def _junit_runs(workspace: Path, pattern: str) -> tuple[list[dict[str, str]], list[str]]:
    files = T._glob_files(workspace, pattern)
    names = [p.name for p in files if p.suffix.lower() == ".xml"]
    runs = [T.parse_junit_text(p.read_text(encoding="utf-8", errors="replace"), p.name)
            for p in files if p.suffix.lower() == ".xml"]
    return runs, names


# --- coverage ----------------------------------------------------------------

@_guard
def coverage_summary_matches(workspace: Path, params: dict, *, run=None) -> dict:
    """The agent's coverage summary JSON equals a recount of the raw report.

    params: report (raw coverage file), summary (agent's JSON), include (opt),
            tolerance_pp (default 0.01)
    """
    _need(params, "report", "summary")
    truth = T.summarize_coverage(T._read_text(workspace, params["report"]), "auto", params.get("include"))
    claimed = json.loads(T._read_text(workspace, params["summary"]))
    tol = float(params.get("tolerance_pp", 0.01))
    problems = []
    for key in ("line_pct", "branch_pct"):
        want, got = truth["totals"].get(key), (claimed.get("totals") or {}).get(key)
        if want is None and got is None:
            continue
        if want is None or got is None or abs(float(want) - float(got)) > tol:
            problems.append(f"totals.{key}: report says {want}, summary says {got}")
    claimed_files = claimed.get("files") or {}
    if set(claimed_files) != set(truth["files"]):
        extra = sorted(set(claimed_files) - set(truth["files"]))[:5]
        dropped = sorted(set(truth["files"]) - set(claimed_files))[:5]
        problems.append(f"file list differs (extra {extra}, missing {dropped})")
    for name, rec in truth["files"].items():
        got = claimed_files.get(name) or {}
        for key in ("lines_total", "lines_covered", "branches_total", "branches_covered"):
            if key in got and int(got[key]) != rec[key]:
                problems.append(f"{name}.{key}: report {rec[key]}, summary {got[key]}")
    if problems:
        return _result(False, "; ".join(problems[:10]))
    return _result(True, f"{len(truth['files'])} files, line {truth['totals']['line_pct']}%, "
                         f"branch {truth['totals']['branch_pct']}% recomputed from {params['report']}")


@_guard
def coverage_delta_min(workspace: Path, params: dict, *, run=None) -> dict:
    """Scoped coverage rose by at least min_delta_pp (and/or reached min_after_pct).

    params: before, after (raw reports), scope (globs/prefixes), metric (line|branch),
            min_delta_pp, min_after_pct (opt), max_denominator_drop_pct (default 2)
    The measured code base must not shrink: excluding files or deleting code
    to lift a percentage fails the check.
    """
    _need(params, "before", "after")
    metric = params.get("metric", "line")
    if metric not in ("line", "branch"):
        raise ToolError("metric must be line or branch")
    delta = T.compare_coverage(T.summarize_coverage(T._read_text(workspace, params["before"])),
                               T.summarize_coverage(T._read_text(workspace, params["after"])),
                               params.get("scope"))
    if not delta["files"]:
        return _result(False, "no files in scope")
    b, a = delta[f"{metric}_pct_before"], delta[f"{metric}_pct_after"]
    d = delta[f"{metric}_delta_pp"]
    if d is None:
        return _result(False, f"no {metric} data in scope")
    problems = []
    if params.get("min_delta_pp") is not None and d < float(params["min_delta_pp"]):
        problems.append(f"{metric} delta {d}pp < required {params['min_delta_pp']}pp")
    if params.get("min_after_pct") is not None and a < float(params["min_after_pct"]):
        problems.append(f"{metric} coverage {a}% < required {params['min_after_pct']}%")
    tb, ta = delta["lines_total_before"], delta["lines_total_after"]
    drop_pct = 100.0 * (tb - ta) / tb if tb else 0.0
    if drop_pct > float(params.get("max_denominator_drop_pct", 2)):
        problems.append(f"measured lines fell from {tb} to {ta} ({drop_pct:.1f}%)")
    summary = f"{metric} {b}% -> {a}% ({d:+}pp) over {len(delta['files'])} files"
    return _result(not problems, "; ".join(problems) or summary, a / 100.0 if a is not None else None)


# --- diffs -------------------------------------------------------------------

@_guard
def diff_test_paths_only(workspace: Path, params: dict, *, run=None) -> dict:
    """The patch changes test code only (production code untouched).

    params: patch, test_globs (opt), allow (opt, extra approved paths)
    """
    _need(params, "patch")
    rep = T.scope_report(T.parse_patch(T._read_text(workspace, params["patch"])),
                         params.get("test_globs"), params.get("allow"))
    if not rep["files_changed"]:
        return _result(False, "patch is empty")
    if rep["outside_test_paths"]:
        return _result(False, f"changes outside test paths: {rep['outside_test_paths'][:10]}")
    return _result(True, f"{rep['files_changed']} test files, +{rep['lines_added']}/-{rep['lines_removed']}")


@_guard
def patch_size_max(workspace: Path, params: dict, *, run=None) -> dict:
    """The patch stays reviewable: added + removed lines <= max_changed_lines."""
    _need(params, "patch", "max_changed_lines")
    rep = T.scope_report(T.parse_patch(T._read_text(workspace, params["patch"])))
    changed, limit = rep["lines_added"] + rep["lines_removed"], int(params["max_changed_lines"])
    return _result(changed <= limit, f"{changed} changed lines (limit {limit})")


@_guard
def no_assertion_free_tests(workspace: Path, params: dict, *, run=None) -> dict:
    """Every test the patch adds asserts something real.

    params: patch, repo_dir (default repo), helpers (opt, custom assertion helpers)
    """
    _need(params, "patch")
    out = T.find_assertion_free_tests(workspace, patch=params["patch"],
                                      repo_dir=params.get("repo_dir", "repo"),
                                      helpers=params.get("helpers"), test_globs=params.get("test_globs"))
    if out["assertion_free"]:
        items = [f"{f['file']}::{f['test']} ({f['reason']})" for f in out["assertion_free"][:10]]
        return _result(False, "assertion-free tests: " + ", ".join(items))
    if not out["files_checked"]:
        return _result(False, "patch adds no test files to check")
    return _result(True, f"{out['files_checked']} test files checked")


@_guard
def patch_secret_free(workspace: Path, params: dict, *, run=None) -> dict:
    """No secret-looking values in lines the patch adds (fixtures, golden files)."""
    _need(params, "patch")
    out = T.scan_patch_secrets(workspace, patch=params["patch"])
    if out["findings"]:
        where = [f"{h['file']}:{h['line']} {h['kind']}" for h in out["findings"][:10]]
        return _result(False, "secret-looking values: " + ", ".join(where))
    return _result(True, f"{out['added_lines_scanned']} added lines scanned")


# --- runs and flakes ---------------------------------------------------------

@_guard
def tests_stable(workspace: Path, params: dict, *, run=None) -> dict:
    """Every run in the JUnit set passed: no failures, no flakes, enough runs.

    params: runs (glob of JUnit files, one per run), min_runs (default 10),
            require_tests (opt list of test-id substrings that must appear in every run)
    """
    _need(params, "runs")
    runs, names = _junit_runs(workspace, params["runs"])
    min_runs = int(params.get("min_runs", 10))
    if len(runs) < min_runs:
        return _result(False, f"{len(runs)} runs found, {min_runs} required")
    census = T.census_from_runs(runs)
    problems = []
    if not census["tests"]:
        problems.append("no tests in the run files")
    if census["flaky"]:
        problems.append(f"flaky: {census['flaky'][:5]}")
    if census["broken"]:
        problems.append(f"failing: {census['broken'][:5]}")
    if census["missing_in_some_runs"]:
        problems.append(f"tests missing from some runs: {census['missing_in_some_runs'][:5]}")
    for needle in params.get("require_tests") or []:
        if not all(any(needle in t for t in r) for r in runs):
            problems.append(f"required test '{needle}' not in every run")
    detail = f"{census['tests']} tests x {len(runs)} runs all passed"
    return _result(not problems, "; ".join(problems) or detail)


@_guard
def flake_census_matches(workspace: Path, params: dict, *, run=None) -> dict:
    """The agent's flake census lists exactly the flaky and broken tests the runs show.

    params: census (agent's JSON with flaky/broken lists), runs (JUnit glob), min_runs (default 5)
    """
    _need(params, "census", "runs")
    runs, _ = _junit_runs(workspace, params["runs"])
    if len(runs) < int(params.get("min_runs", 5)):
        return _result(False, f"{len(runs)} runs found, {params.get('min_runs', 5)} required")
    truth = T.census_from_runs(runs)
    claimed = json.loads(T._read_text(workspace, params["census"]))
    problems = []
    for key in ("flaky", "broken"):
        want, got = set(truth[key]), set(claimed.get(key) or [])
        if want != got:
            problems.append(f"{key}: runs show {sorted(want)[:5]}, census says {sorted(got)[:5]}")
    if int(claimed.get("runs", len(runs))) != len(runs):
        problems.append(f"census claims {claimed.get('runs')} runs, {len(runs)} files present")
    return _result(not problems, "; ".join(problems) or
                   f"{len(truth['flaky'])} flaky, {len(truth['broken'])} broken over {len(runs)} runs")


# --- mutation and targets ----------------------------------------------------

@_guard
def mutation_score_min(workspace: Path, params: dict, *, run=None) -> dict:
    """Mutation score over the scope is at least min_score_pct.

    params: report (mutation-testing-elements JSON), scope (opt), min_score_pct,
            min_mutants (default 1)
    """
    _need(params, "report", "min_score_pct")
    data = json.loads(T._read_text(workspace, params["report"]))
    out = T.summarize_mutation(data, params.get("scope"))
    total = out["detected"] + out["undetected"]
    if total < int(params.get("min_mutants", 1)):
        return _result(False, f"only {total} scorable mutants in scope")
    score, floor = out["score_pct"], float(params["min_score_pct"])
    return _result(score >= floor, f"mutation score {score}% ({out['detected']}/{total}), floor {floor}%",
                   score / 100.0)


@_guard
def targets_ranking_matches(workspace: Path, params: dict, *, run=None) -> dict:
    """targets.csv ranks files by the documented risk formula over the real report.

    params: targets (agent CSV), coverage (raw report), churn (opt CSV), include (opt),
            min_rows (default 1)
    proposed_floor_pct is negotiable with the customer and not compared.
    """
    _need(params, "targets", "coverage")
    summary = T.summarize_coverage(T._read_text(workspace, params["coverage"]), "auto", params.get("include"))
    churn = T._churn_map(T._read_text(workspace, params["churn"])) if params.get("churn") else {}
    truth = {r["path"]: r for r in T.ranked_targets(summary, churn)}
    rows = list(csv.DictReader(io.StringIO(T._read_text(workspace, params["targets"]))))
    if len(rows) < int(params.get("min_rows", 1)):
        return _result(False, f"targets CSV has {len(rows)} rows")
    missing_cols = [c for c in ("rank", "path", "line_pct", "churn", "risk_score") if c not in rows[0]]
    if missing_cols:
        return _result(False, f"targets CSV lacks columns {missing_cols}")
    problems, listed, prev = [], set(), float("inf")
    for i, row in enumerate(rows):
        path = T._norm(row["path"])
        want = truth.get(path)
        if want is None:
            problems.append(f"{path}: not in the coverage report")
            continue
        listed.add(path)
        if int(row["rank"]) != i + 1 or want["risk_score"] > prev + 1e-9:
            problems.append(f"{path}: out of risk order at row {i + 1}")
        prev = min(prev, want["risk_score"])
        if abs(float(row["risk_score"]) - want["risk_score"]) > 1e-3:
            problems.append(f"{path}: risk {row['risk_score']} != recomputed {want['risk_score']}")
        if row.get("line_pct") not in ("", None) and want["line_pct"] is not None \
                and abs(float(row["line_pct"]) - want["line_pct"]) > 0.01:
            problems.append(f"{path}: line_pct {row['line_pct']} != report {want['line_pct']}")
        if int(row.get("churn") or 0) != want["churn"]:
            problems.append(f"{path}: churn {row.get('churn')} != {want['churn']}")
    # A top-N list must not skip a riskier file.
    skipped = [p for p, r in truth.items() if p not in listed and r["risk_score"] > prev + 1e-9]
    if skipped:
        problems.append(f"riskier files left off the list: {sorted(skipped)[:5]}")
    return _result(not problems, "; ".join(problems[:10]) or f"{len(rows)} targets verified")


CHECK_DEFS: dict[str, Callable[..., dict[str, Any]]] = {
    "coverage_summary_matches": coverage_summary_matches,
    "coverage_delta_min": coverage_delta_min,
    "diff_test_paths_only": diff_test_paths_only,
    "patch_size_max": patch_size_max,
    "no_assertion_free_tests": no_assertion_free_tests,
    "patch_secret_free": patch_secret_free,
    "tests_stable": tests_stable,
    "flake_census_matches": flake_census_matches,
    "mutation_score_min": mutation_score_min,
    "targets_ranking_matches": targets_ranking_matches,
}
