"""
specialists/data_analyst/checks.py - acceptance checks for the data-analyst
specialist.

Every check reloads the customer's data from inputs/ and recomputes what it
verifies: it re-runs the saved SQL, re-profiles the tables and recomputes the
metrics. Files the agent wrote (profile.json, results/*.csv, figures.json,
reconciliation.csv) are treated as claims to test, never as the truth. A
saved query must read at least one input table, and a figure or metric value
must not be a literal typed into its SQL; the reference totals are never
queryable, so metrics cannot copy the numbers they are reconciled against.

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
# What may sit between a figure's display text and its marker: closing
# emphasis (**$4,436.00** [F:x]) and spaces, nothing else.
_GAP = r"[*_]{0,2}[ \t]*"
_MARKER_AFTER = re.compile(_GAP + r"\[F:[a-z0-9][a-z0-9_\-]{0,63}\]")
# A figure's display must not continue a number or a sign to its left:
# "-$4,500.00" or "119" do not show "$4,500.00" or "19".
_LEFT_EDGE = r"(?<![0-9A-Za-z$.,+\-])"
# Numbers a report states as facts: money, percentages, thousands-grouped
# numbers, decimals and long whole numbers. Each must be followed by an
# [F:id] marker. Short whole numbers (19 customers, 2026, 3 of 62 rows) and
# dates are left alone.
_STATED_NUMBER = re.compile(
    r"(?<![\d.,])[-+]?\$\s?\d[\d,]*(?:\.\d+)?"          # $4,436.00, -$12.50, US$95
    r"|(?<![\w.,\-])(?:[-+]?\d[\d,]*(?:\.\d+)?\s?%"    # 24.0%, 5 %; not INV-00012
    r"|\d{1,3}(?:,\d{3})+(?:\.\d+)?"                   # 1,204
    r"|\d+\.\d+"                                       # 37.5, 4436.00
    r"|\d{5,})")                                       # 1200000
_FENCE = re.compile(r"^\s{0,3}(```|~~~)")
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_HEADING_NUMBER = re.compile(r"^(?:\d+(?:\.\d+)*\.?\s+)")
_INLINE_CODE = re.compile(r"`[^`]*`")


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

    Masked columns (marked "masked" in profile.json, or holding e-mail
    addresses) are compared on nulls and distinct counts only, and must not
    carry any values.

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
            got_cols = {c.get("name"): c for c in got.get("columns", []) if isinstance(c, dict)}
            masked = {name for name, c in got_cols.items() if c.get("masked") is True}
            truth = tools.profile_table(session, table, masked)
            bad = [k for k in ("row_count", "duplicate_rows") if got.get(k) != truth[k]]
            for col in truth["columns"]:
                g = got_cols.get(col["name"])
                if g is None:
                    bad.append(f"column {col['name']} missing")
                    continue
                if col.get("masked"):
                    if any(g.get(k) not in (None, []) for k in tools.VALUE_FIELDS):
                        bad.append(f"{col['name']} holds personal data but shows values (must be masked)")
                    fields = ("nulls", "distinct")
                else:
                    fields = ("nulls", "distinct", "min", "max")
                bad += [f"{col['name']}.{k}" for k in fields if not _close(g.get(k), col[k])]
            if bad:
                problems.append(f"{table}: " + ", ".join(bad[:6]) + " differ from the data")
            else:
                ok += 1
    finally:
        session.close()
    return _result(not problems, problems, ok, total, "table profiles")


# --- saved queries --------------------------------------------------------------

def queries_reexecute(workspace: Path, params: dict, *, run=None) -> dict:
    """Every saved queries/<name>.sql is read-only, reads an input table, runs,
    and reproduces results/<name>.csv.

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
                tools.require_input_tables(result, f.name)
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

def _title_is(title: str, names: list[str]) -> bool:
    """A heading is exactly one of `names` ("Method", "4. Method:"), so a
    heading such as "Findings and method" exempts nothing."""
    text = " ".join(_HEADING_NUMBER.sub("", title.strip()).rstrip(":").lower().split())
    return any(text == " ".join(n.lower().split()) for n in names)


def unmarked_numbers(report: str, exempt_sections: list[str]) -> list[str]:
    """Stated numbers (see _STATED_NUMBER) in the report that are not followed
    by an [F:id] marker. Headings, code, and sections titled exactly as one of
    `exempt_sections` (with their subsections) are skipped."""
    found, fence, skip_level = [], None, None
    for line in report.splitlines():
        f = _FENCE.match(line)
        if f:
            fence = None if fence == f.group(1) else (fence or f.group(1))
            continue
        if fence:
            continue
        h = _HEADING.match(line)
        if h:
            level = len(h.group(1))
            if skip_level is not None and level <= skip_level:
                skip_level = None
            if skip_level is None and _title_is(h.group(2), exempt_sections):
                skip_level = level
            continue
        if skip_level is not None:
            continue
        text = _INLINE_CODE.sub(" code ", line)
        for m in _STATED_NUMBER.finditer(text):
            if not _MARKER_AFTER.match(text, m.end()):
                found.append(m.group(0).strip())
    return found


def _cites_exactly(report: str, fid: str, display: str) -> tuple[int, int]:
    """(times [F:fid] appears, times it directly follows its display text)."""
    marker = re.escape(f"[F:{fid}]")
    cited = len(re.findall(marker, report))
    shown = len(re.findall(_LEFT_EDGE + re.escape(display) + _GAP + marker, report))
    return cited, shown


def figures_match_queries(workspace: Path, params: dict, *, run=None) -> dict:
    """Every figure recomputes from its saved query and each [F:id] marker in the
    report directly follows the figure's display text; no money, percentage,
    grouped, decimal or long number in the report goes without a marker.

    params: milestone, report (default <milestone dir>/report.md), min_figures (default 1),
            require_all_cited (default true), exempt_sections (default [Method]: sections,
            titled exactly so, where unmarked numbers are allowed)
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
                value = tools.computed_value(session, sql, str(fig.get("column")), int(fig.get("row", 0)),
                                             f"query {fig.get('query')!r}")
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
            cited, shown = _cites_exactly(report, fid, display)
            if not cited:
                if params.get("require_all_cited", True):
                    problems.append(f"{fid}: not cited in the report")
                    continue
            elif shown != cited:
                problems.append(f"{fid}: [F:{fid}] must directly follow {display} "
                                f"({cited - shown} of {cited} citation(s) do not)")
                continue
            ok += 1
    finally:
        session.close()
    loose = unmarked_numbers(report, list(params.get("exempt_sections") or ["Method"]))
    if loose:
        shown_numbers = ", ".join(repr(n) for n in loose[:8]) + (" ..." if len(loose) > 8 else "")
        problems.append(f"{len(loose)} number(s) in the report have no [F:id] marker: {shown_numbers}; "
                        "record each with record_figure and cite it")
    return _result(not problems, problems, ok, len(figures), "figures")


# --- M2: metric definitions ------------------------------------------------------------

def metrics_valid(workspace: Path, params: dict, *, run=None) -> dict:
    """metrics.yaml has complete, uniquely named definitions whose saved
    queries read the input data and return a computed number.

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


def _number(text: Any) -> float | None:
    text = str(text if text is not None else "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return float("nan")


def _reconciliation_problems(path: Path, rows: list[dict]) -> list[str]:
    """How reconciliation.csv differs from the recomputed rows: one row per
    metric, with the same computed, reference, diff, tolerance and status."""
    truth = {r["metric"]: r for r in rows}
    problems, seen = [], set()
    with path.open(newline="", encoding="utf-8") as fh:
        for claimed in csv.DictReader(fh):
            name = claimed.get("metric", "")
            real = truth.get(name)
            if real is None:
                problems.append(f"reconciliation lists unknown metric {name!r}")
                continue
            if name in seen:
                problems.append(f"reconciliation lists {name} twice")
                continue
            seen.add(name)
            for column in ("computed", "reference", "diff_pct", "tolerance_pct"):
                got, want = _number(claimed.get(column)), real[column]
                same = got is None if want is None else (got is not None and _close(got, want))
                if not same:
                    problems.append(f"reconciliation claims {name} {column} = {claimed.get(column)!r}, "
                                    f"recomputed {want!r}")
            if (claimed.get("status") or "") != real["status"]:
                problems.append(f"reconciliation claims {name} is {claimed.get('status')!r}, "
                                f"actually {real['status']!r}")
    problems += [f"reconciliation has no row for {name}" for name in truth if name not in seen]
    return problems


def metrics_reconcile(workspace: Path, params: dict, *, run=None) -> dict:
    """Recomputed metrics tie to the customer's reference totals, and reconciliation.csv is honest.

    Without a reference file (it is optional at intake) nothing is
    reconciled: the check passes once reconciliation.csv matches the
    recomputation, and says so; set require_reference to fail instead.

    params: milestone (default m2-metrics), reference (default inputs/reference_totals.csv),
            tolerance_pct (default 0.5; whole-number results tie exactly unless the reference
            row sets tolerance_pct), require_all_references (default true),
            require_reference (default false),
            reconciliation (default <milestone dir>/reconciliation.csv; "" to skip)
    """
    params = {"milestone": "m2-metrics", **params}
    base = _milestone_dir(workspace, params)
    reference = params.get("reference") or tools.REFERENCE_FILE
    try:
        rows = tools.evaluate_metrics(workspace, params["milestone"], reference,
                                      float(params.get("tolerance_pct", 0.5)),
                                      require_reference=bool(params.get("require_reference", False)))
        _, has_reference = tools.reference_file(workspace, reference)
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
            problems += _reconciliation_problems(rec_path, rows)
    ok = sum(1 for r in checked if r["status"] == "match")
    if not checked:
        if has_reference:
            problems.append(f"{reference} lists no metrics (expected columns metric, value); ask the "
                            "customer for usable reference totals")
        elif not problems:
            return {"passed": True, "score": None,
                    "details": f"no reference totals supplied ({reference}); {len(rows)} metric(s) "
                               "recomputed, nothing reconciled"}
    return _result(not problems, problems, ok, len(checked), "metrics reconciled")


CHECK_DEFS: dict[str, Callable[..., dict]] = {
    "profile_matches_source": profile_matches_source,
    "queries_reexecute": queries_reexecute,
    "figures_match_queries": figures_match_queries,
    "metrics_valid": metrics_valid,
    "metrics_reconcile": metrics_reconcile,
}
