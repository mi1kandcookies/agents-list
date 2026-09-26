"""
specialists/data_analyst/checks.py - acceptance checks for the data-analyst
specialist.

Every check reloads the customer's data from inputs/ and recomputes what it
verifies: it re-runs the saved SQL, re-profiles the tables and recomputes the
metrics. Files the agent wrote (profile.json, results/*.csv, figures.json,
reconciliation.csv) are treated as claims to test, never as the truth.

A check is `fn(workspace: Path, params: dict, *, run=None) -> {"passed",
"details", "score"}`; CHECK_DEFS maps the names agent.yaml uses to them.
"""
from __future__ import annotations

import csv
import json
import math
import re
from pathlib import Path
from typing import Any, Callable

import yaml

from agentkit.errors import ToolError
from specialists.data_analyst import tools

_MARKER = re.compile(r"\[F:([a-z0-9][a-z0-9_\-]{0,63})\]")


def _result(passed: bool, problems: list[str], ok: int, total: int, what: str) -> dict:
    score = round(ok / total, 4) if total else 0.0
    if passed:
        details = f"{ok}/{total} {what} verified"
    else:
        shown = problems[:10]
        more = f" (+{len(problems) - 10} more)" if len(problems) > 10 else ""
        details = "; ".join(shown) + more
    return {"passed": passed, "details": details, "score": score}


def _fail(details: str) -> dict:
    return {"passed": False, "details": details, "score": 0.0}


def _close(a: Any, b: Any, tol: float = 1e-6) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) \
            and not isinstance(a, bool) and not isinstance(b, bool):
        return math.isclose(float(a), float(b), rel_tol=tol, abs_tol=tol)
    return a == b


def _milestone_dir(workspace: Path, params: dict) -> Path:
    return tools.milestone_dir(workspace, str(params.get("milestone", "")))


# --- M1: profile ---------------------------------------------------------------

def profile_matches_source(workspace: Path, params: dict, *, run=None) -> dict:
    """profile.json covers every in-scope table and its counts match a fresh profile.

    params: path (default deliverables/m1-profile/profile.json), tables (default: all inputs)
    """
    path = tools.resolve_in(workspace, params.get("path", "deliverables/m1-profile/profile.json"))
    try:
        claimed = json.loads(path.read_text(encoding="utf-8"))["tables"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return _fail(f"cannot read profile: {exc}")
    session = tools.open_session(workspace)
    problems, ok, total = [], 0, 0
    try:
        tables = params.get("tables") or list(session.tables)
        if not tables:
            return _fail("no tables found under inputs/")
        for table in tables:
            total += 1
            if table not in session.tables:
                problems.append(f"{table}: not in inputs/")
                continue
            got = claimed.get(table) if isinstance(claimed, dict) else None
            if not isinstance(got, dict):
                problems.append(f"{table}: not profiled")
                continue
            truth = tools.profile_table(session, table)
            bad = [k for k in ("row_count", "duplicate_rows") if got.get(k) != truth[k]]
            got_cols = {c.get("name"): c for c in got.get("columns", []) if isinstance(c, dict)}
            for col in truth["columns"]:
                g = got_cols.get(col["name"])
                if g is None:
                    bad.append(f"column {col['name']} missing")
                    continue
                bad += [f"{col['name']}.{k}" for k in ("nulls", "distinct", "min", "max")
                        if not _close(g.get(k), col[k])]
            if bad:
                problems.append(f"{table}: " + ", ".join(bad[:6]) + " differ from the data")
            else:
                ok += 1
    finally:
        session.close()
    return _result(not problems, problems, ok, total, "table profiles")


# --- saved queries --------------------------------------------------------------

def queries_reexecute(workspace: Path, params: dict, *, run=None) -> dict:
    """Every saved queries/<name>.sql is read-only, runs, and reproduces results/<name>.csv.

    params: milestone, min_queries (default 1)
    """
    base = _milestone_dir(workspace, params)
    files = sorted((base / "queries").glob("*.sql"))
    min_queries = int(params.get("min_queries", 1))
    if len(files) < min_queries:
        return _fail(f"{len(files)} saved queries in {base.name}/queries, need at least {min_queries}")
    session = tools.open_session(workspace)
    problems, ok = [], 0
    try:
        for f in files:
            saved = base / "results" / f"{f.stem}.csv"
            try:
                result = tools.execute(session, f.read_text(encoding="utf-8"))
            except ToolError as exc:
                problems.append(f"{f.name}: {exc}")
                continue
            if not saved.is_file():
                problems.append(f"{f.name}: no results/{f.stem}.csv")
                continue
            claimed = saved.read_text(encoding="utf-8").replace("\r\n", "\n")
            if claimed != tools.result_csv(result):
                problems.append(f"{f.name}: re-run result differs from results/{f.stem}.csv")
                continue
            ok += 1
    finally:
        session.close()
    return _result(not problems, problems, ok, len(files), "queries")


# --- M3: figures in the report -------------------------------------------------------

def figures_match_queries(workspace: Path, params: dict, *, run=None) -> dict:
    """Every figure recomputes from its saved query and appears in the report as recorded.

    params: milestone, report (default <milestone dir>/report.md), min_figures (default 1),
            require_all_cited (default true)
    """
    base = _milestone_dir(workspace, params)
    report_path = tools.resolve_in(workspace, params.get("report") or tools._rel(workspace, base) + "/report.md")
    try:
        figures = tools.load_figures(base / "figures.json")
        report = report_path.read_text(encoding="utf-8")
    except (OSError, ValueError, ToolError) as exc:
        return _fail(f"cannot read figures or report: {exc}")
    min_figures = int(params.get("min_figures", 1))
    if len(figures) < min_figures:
        return _fail(f"{len(figures)} figures recorded, need at least {min_figures}")
    lines = report.splitlines()
    known = {str(f.get("id")) for f in figures if isinstance(f, dict)}
    problems = [f"[F:{m}] cited in the report but not in figures.json"
                for m in sorted(set(_MARKER.findall(report)) - known)]
    session = tools.open_session(workspace)
    ok = 0
    try:
        for fig in figures:
            fid = str(fig.get("id", "?")) if isinstance(fig, dict) else "?"
            try:
                sql = tools.load_saved_query(workspace, str(params.get("milestone", "")), str(fig.get("query")))
                value = tools.pick_value(tools.execute(session, sql), str(fig.get("column")),
                                         int(fig.get("row", 0)))
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ToolError(f"value {value!r} is not numeric")
                display = tools.format_value(value, fig.get("unit", ""), fig.get("decimals"))
            except (ToolError, AttributeError, TypeError, ValueError) as exc:
                problems.append(f"{fid}: {exc}")
                continue
            if not _close(fig.get("value"), value):
                problems.append(f"{fid}: recorded {fig.get('value')!r} but the query returns {value!r}")
                continue
            if fig.get("display") != display:
                problems.append(f"{fid}: display {fig.get('display')!r} should be {display!r}")
                continue
            marker = f"[F:{fid}]"
            cited = [ln for ln in lines if marker in ln]
            if not cited:
                if params.get("require_all_cited", True):
                    problems.append(f"{fid}: not cited in the report")
                    continue
            elif any(display not in ln for ln in cited):
                problems.append(f"{fid}: a line citing {marker} does not show {display}")
                continue
            ok += 1
    finally:
        session.close()
    return _result(not problems, problems, ok, len(figures), "figures")


# --- M2: metric definitions ------------------------------------------------------------

def metrics_valid(workspace: Path, params: dict, *, run=None) -> dict:
    """metrics.yaml has complete, uniquely named definitions whose queries run and return a number.

    params: milestone (default m2-metrics), min_metrics (default 1)
    """
    params = {"milestone": "m2-metrics", **params}
    base = _milestone_dir(workspace, params)
    try:
        metrics = tools.load_metrics(base / "metrics.yaml")
    except ToolError as exc:
        return _fail(str(exc))
    except yaml.YAMLError as exc:
        return _fail(f"metrics.yaml does not parse: {exc}")
    min_metrics = int(params.get("min_metrics", 1))
    if len(metrics) < min_metrics:
        return _fail(f"{len(metrics)} metrics defined, need at least {min_metrics}")
    problems, ok, seen = [], 0, set()
    session = tools.open_session(workspace)
    try:
        for i, m in enumerate(metrics):
            if not isinstance(m, dict):
                problems.append(f"metrics[{i}] is not a mapping")
                continue
            name = str(m.get("name", f"metrics[{i}]"))
            missing = [k for k in tools.METRIC_FIELDS if not str(m.get(k) or "").strip()]
            if missing:
                problems.append(f"{name}: missing {', '.join(missing)}")
                continue
            if name in seen:
                problems.append(f"{name}: defined twice")
                continue
            seen.add(name)
            try:
                tools.metric_value(workspace, session, base, m)
            except ToolError as exc:
                problems.append(f"{name}: {exc}")
                continue
            ok += 1
    finally:
        session.close()
    return _result(not problems, problems, ok, len(metrics), "metric definitions")


def metrics_reconcile(workspace: Path, params: dict, *, run=None) -> dict:
    """Recomputed metrics tie to the customer's reference totals, and reconciliation.csv is honest.

    params: milestone (default m2-metrics), reference (default inputs/reference_totals.csv),
            tolerance_pct (default 0.5), require_all_references (default true),
            reconciliation (default <milestone dir>/reconciliation.csv; "" to skip)
    """
    params = {"milestone": "m2-metrics", **params}
    base = _milestone_dir(workspace, params)
    try:
        rows = tools.evaluate_metrics(workspace, params["milestone"],
                                      params.get("reference", "inputs/reference_totals.csv"),
                                      float(params.get("tolerance_pct", 0.5)))
    except Exception as exc:
        return _fail(f"cannot evaluate metrics: {exc}")
    require_all = params.get("require_all_references", True)
    problems = []
    checked = [r for r in rows if r["status"] != "no_reference"]
    for r in checked:
        if r["status"] == "missing_metric":
            if require_all:
                problems.append(f"{r['metric']}: reference total has no metric definition")
        elif r["status"] != "match":
            problems.append(f"{r['metric']}: {r['status']} (computed {r['computed']}, "
                            f"reference {r['reference']}, diff {r['diff_pct']}%)")
    rec_param = params.get("reconciliation")
    rec_path = base / "reconciliation.csv" if rec_param is None else (
        tools.resolve_in(workspace, rec_param) if rec_param else None)
    if rec_path is not None:
        if not rec_path.is_file():
            problems.append(f"{rec_path.name} not found")
        else:
            truth = {r["metric"]: r for r in rows}
            with rec_path.open(newline="", encoding="utf-8") as fh:
                for claimed in csv.DictReader(fh):
                    name = claimed.get("metric", "")
                    real = truth.get(name)
                    if real is None:
                        problems.append(f"reconciliation lists unknown metric {name!r}")
                        continue
                    try:
                        value = float(claimed.get("computed") or "nan")
                    except ValueError:
                        value = float("nan")
                    if real["computed"] is not None and not _close(value, real["computed"]):
                        problems.append(f"reconciliation claims {name} = {claimed.get('computed')} "
                                        f"but the query returns {real['computed']}")
                    if (claimed.get("status") or "") != real["status"]:
                        problems.append(f"reconciliation claims {name} is {claimed.get('status')!r}, "
                                        f"actually {real['status']!r}")
    ok = sum(1 for r in checked if r["status"] == "match")
    if not checked:
        problems.append("no metric has a reference total to reconcile against")
    return _result(not problems, problems, ok, len(checked), "metrics reconciled")


CHECK_DEFS: dict[str, Callable[..., dict]] = {
    "profile_matches_source": profile_matches_source,
    "queries_reexecute": queries_reexecute,
    "figures_match_queries": figures_match_queries,
    "metrics_valid": metrics_valid,
    "metrics_reconcile": metrics_reconcile,
}
