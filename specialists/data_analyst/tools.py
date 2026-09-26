"""
specialists/data_analyst/tools.py - deterministic domain tools for the
data-analyst specialist.

The customer's data arrives as files under inputs/: CSV exports and/or SQLite
databases. Every call loads them into a fresh in-memory SQLite session, then
locks that session down (PRAGMA query_only plus an authorizer that only
permits reads), so a query can never change the customer's data or the
agent's own deliverables. The tools:

    list_tables        schema inventory (tables, columns, types, row counts)
    profile_tables     per-column data-quality profile -> profile.json
    run_query          guarded SELECT, returns a preview and a result hash
    save_query         guarded SELECT saved as queries/<name>.sql + results/<name>.csv
    record_figure      pull one number out of a saved query into figures.json
    reconcile_metrics  metrics.yaml values vs the customer's reference totals

Tools are plain `fn(workspace: Path, *, fetch=None, run=None, **args)`
functions that the kit wraps from TOOL_DEFS. None of them needs the network
or a subprocess. Tools that write files, or read a path the model chose,
also take the kit's `resolve_path`: under the harness every such path goes
through the PolicyGate (inputs/ read-only, .agentkit/ refused) and written
files are marked agent-authored. Called directly (tests, checks) they fall
back to resolve_in, the same lexical-then-resolved jail. checks.py reuses
the loaders here so acceptance recomputes every number from inputs/ rather
than trusting what the agent wrote.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from agentkit.errors import PolicyViolation, ToolError
from agentkit.policy import INTERNAL_DIR, jail_path

INPUTS_DIR = "inputs"
DELIVERABLES_DIR = "deliverables"
CSV_SUFFIXES = {".csv"}
SQLITE_SUFFIXES = {".sqlite", ".sqlite3", ".db"}
PREVIEW_ROWS = 50
MAX_PREVIEW_ROWS = 500

_SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,63}$")
_IDENT_JUNK = re.compile(r"[^0-9a-zA-Z_]+")
_INT = re.compile(r"^[+-]?(0|[1-9][0-9]*)$")
_LEADING_ZERO = re.compile(r"^[+-]?0[0-9]")   # zip codes, account numbers
_FLOAT = re.compile(r"^[+-]?([0-9]+\.[0-9]*|\.[0-9]+|[0-9]+)([eE][+-]?[0-9]+)?$")

# Statement keywords that change data or the connection. The authorizer below
# is the real guard; this text scan exists to give the model a clear error.
# `replace(` is SQLite's string function, so REPLACE only counts as a keyword
# when it is not followed by an opening parenthesis.
_WRITE_KEYWORDS = re.compile(
    r"\b(insert|update|delete|create|drop|alter|attach|detach|pragma|vacuum|reindex|"
    r"analyze|begin|commit|rollback|savepoint|release|load_extension)\b|\breplace\b(?!\s*\()",
    re.IGNORECASE,
)
_READ_ACTIONS = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    getattr(sqlite3, "SQLITE_RECURSIVE", 33),
}


# --- loading the customer's data --------------------------------------------

@dataclass
class Session:
    """A read-only SQLite session over everything in inputs/."""
    conn: sqlite3.Connection
    tables: dict[str, list[tuple[str, str]]] = field(default_factory=dict)  # name -> [(col, type)]
    sources: dict[str, str] = field(default_factory=dict)                   # table -> inputs/ file
    warnings: list[str] = field(default_factory=list)

    def close(self) -> None:
        self.conn.close()


def _ident(name: str) -> str:
    """Quote an SQL identifier."""
    return '"' + name.replace('"', '""') + '"'


def _clean_name(raw: str, fallback: str) -> str:
    name = _IDENT_JUNK.sub("_", raw.strip()).strip("_").lower() or fallback
    return "t_" + name if name[0].isdigit() else name


def _infer_type(values: list[str]) -> str:
    """INTEGER, REAL or TEXT for a CSV column. IDs with leading zeros stay TEXT."""
    seen = [v.strip() for v in values if v.strip() != ""]
    if not seen or any(_LEADING_ZERO.match(v) for v in seen):
        return "TEXT"
    if all(_INT.match(v) for v in seen):
        return "INTEGER"
    if all(_FLOAT.match(v) for v in seen):
        return "REAL"
    return "TEXT"


def _convert(value: str, col_type: str) -> Any:
    value = value.strip()
    if value == "":
        return None
    if col_type == "INTEGER":
        return int(value)
    if col_type == "REAL":
        return float(value)
    return value


def _load_csv(conn: sqlite3.Connection, path: Path, table: str, session: Session) -> None:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)
        header = next(reader, None)
        if not header:
            session.warnings.append(f"{path.name}: empty file, skipped")
            return
        rows = list(reader)
    columns: list[str] = []
    for i, raw in enumerate(header):
        name = _clean_name(raw, f"col_{i + 1}")
        while name in columns:
            name += "_dup"
        columns.append(name)
    width = len(columns)
    ragged = sum(1 for r in rows if len(r) != width)
    if ragged:
        session.warnings.append(f"{path.name}: {ragged} row(s) with a wrong field count were padded/truncated")
    rows = [(r + [""] * width)[:width] for r in rows]
    types = [_infer_type([r[i] for r in rows]) for i in range(width)]
    cols_sql = ", ".join(f"{_ident(c)} {t}" for c, t in zip(columns, types))
    conn.execute(f"CREATE TABLE {_ident(table)} ({cols_sql})")
    marks = ", ".join("?" * width)
    conn.executemany(f"INSERT INTO {_ident(table)} VALUES ({marks})",
                     [[_convert(v, t) for v, t in zip(r, types)] for r in rows])


def _load_sqlite(conn: sqlite3.Connection, path: Path, session: Session) -> list[str]:
    """Copy every table of a customer SQLite file into the session (opened read-only)."""
    src = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    loaded = []
    try:
        names = [r[0] for r in src.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        for raw in names:
            table = _clean_name(raw, "table")
            if table in session.tables or table in loaded:
                table = _clean_name(f"{path.stem}_{raw}", "table")
            info = src.execute(f"PRAGMA table_info({_ident(raw)})").fetchall()
            cols_sql = ", ".join(f"{_ident(c[1])} {c[2] or ''}".strip() for c in info)
            conn.execute(f"CREATE TABLE {_ident(table)} ({cols_sql})")
            rows = src.execute(f"SELECT * FROM {_ident(raw)}").fetchall()
            if rows:
                marks = ", ".join("?" * len(info))
                conn.executemany(f"INSERT INTO {_ident(table)} VALUES ({marks})", rows)
            loaded.append(table)
    finally:
        src.close()
    return loaded


def _authorizer(action: int, *_args: Any) -> int:
    return sqlite3.SQLITE_OK if action in _READ_ACTIONS else sqlite3.SQLITE_DENY


def input_files(workspace: Path) -> list[Path]:
    root = Path(workspace) / INPUTS_DIR
    if not root.is_dir():
        return []
    return sorted(p for p in root.rglob("*")
                  if p.is_file() and p.suffix.lower() in CSV_SUFFIXES | SQLITE_SUFFIXES)


def open_session(workspace: Path) -> Session:
    """Load inputs/ into memory and return a session that can only read."""
    workspace = Path(workspace)
    conn = sqlite3.connect(":memory:")
    session = Session(conn=conn)
    for path in input_files(workspace):
        rel = path.relative_to(workspace).as_posix()
        if path.suffix.lower() in CSV_SUFFIXES:
            table = _clean_name(path.stem, "table")
            if table in session.tables:
                table = _clean_name(f"{path.parent.name}_{path.stem}", "table")
            _load_csv(conn, path, table, session)
            new_tables = [table] if _table_exists(conn, table) else []
        else:
            new_tables = _load_sqlite(conn, path, session)
        for table in new_tables:
            info = conn.execute(f"PRAGMA table_info({_ident(table)})").fetchall()
            session.tables[table] = [(c[1], c[2] or "") for c in info]
            session.sources[table] = rel
    conn.commit()
    # Lock the session: from here on only reads are authorized.
    conn.execute("PRAGMA query_only = ON")
    conn.set_authorizer(_authorizer)
    return session


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (table,)).fetchone() is not None


# --- guarded queries -----------------------------------------------------------

def _strip_sql(sql: str) -> str:
    """SQL with comments removed and string literals blanked, for keyword scans."""
    out, i, n = [], 0, len(sql)
    while i < n:
        ch = sql[i]
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j
        elif sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            i = n if j < 0 else j + 2
            out.append(" ")
        elif ch in ("'", '"', "`", "["):
            close = "]" if ch == "[" else ch
            j = i + 1
            while j < n:
                if sql[j] == close:
                    if close != "]" and j + 1 < n and sql[j + 1] == close:
                        j += 2
                        continue
                    break
                j += 1
            # Quoted identifiers keep a placeholder name; strings become ''.
            out.append("''" if ch == "'" else "_q")
            i = j + 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def check_sql(sql: str) -> str:
    """Validate that `sql` is one read-only statement; return it without a trailing ';'.

    Raises ToolError with a message the model can act on.
    """
    if not isinstance(sql, str) or not sql.strip():
        raise ToolError("empty SQL")
    bare = _strip_sql(sql)
    statements = [s for s in bare.split(";") if s.strip()]
    if len(statements) != 1:
        raise ToolError("exactly one SQL statement is allowed per query")
    first = statements[0].strip().split(None, 1)[0].lower()
    if first not in ("select", "with", "values"):
        raise ToolError(f"only SELECT/WITH queries are allowed, got {first.upper()}")
    bad = _WRITE_KEYWORDS.search(statements[0])
    if bad:
        raise ToolError(f"read-only session: {bad.group(0).upper()} is not allowed")
    return sql.strip().rstrip(";").strip()


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[tuple]

    @property
    def sha256(self) -> str:
        return result_digest(self.columns, self.rows)


def execute(session: Session, sql: str) -> QueryResult:
    """Run one guarded read-only query in `session`."""
    text = check_sql(sql)
    try:
        cur = session.conn.execute(text)
        rows = cur.fetchall()
    except sqlite3.DatabaseError as exc:
        raise ToolError(f"SQL error: {exc}") from exc
    columns = [d[0] for d in (cur.description or [])]
    return QueryResult(columns=columns, rows=rows)


def cell(value: Any) -> str:
    """Canonical text form of one result value (used in CSVs and hashes)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(int(value))
    if isinstance(value, float):
        return repr(round(value, 6))
    if isinstance(value, bytes):
        return value.hex()
    return str(value)


def result_digest(columns: list[str], rows: list[tuple]) -> str:
    payload = json.dumps({"columns": list(columns), "rows": [[cell(v) for v in r] for r in rows]},
                         separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def result_csv(result: QueryResult) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(result.columns)
    for row in result.rows:
        writer.writerow([cell(v) for v in row])
    return buf.getvalue()


def _jsonable(value: Any) -> Any:
    return value.hex() if isinstance(value, bytes) else value


# --- workspace paths --------------------------------------------------------------

def _safe_name(value: str, what: str) -> str:
    if not isinstance(value, str) or not _SAFE_NAME.match(value):
        raise ToolError(f"{what} must match {_SAFE_NAME.pattern} (lowercase, digits, _ or -)")
    return value


def resolve_in(workspace: Path, rel: str) -> Path:
    """Resolve a workspace-relative path with the kit's jail (checked lexically
    before the filesystem is touched, then symlinks resolved); refuse anything
    that escapes the workspace and the kit's internal .agentkit/ folder."""
    root = Path(workspace).resolve()
    try:
        target = jail_path(root, rel)
    except PolicyViolation as exc:
        raise ToolError(f"path escapes the workspace: {rel} ({exc})") from None
    parts = target.relative_to(root).parts
    if parts and parts[0].lower() == INTERNAL_DIR:
        raise ToolError(f"path is internal to the kit: {rel}")
    return target


# resolve_path(rel, *, write=False) -> Path, as the kit hands it to domain tools.
Resolver = Callable[..., Path]


def resolver(workspace: Path, resolve_path: Resolver | None = None) -> Resolver:
    """The kit's resolve_path when a tool runs under the harness, else resolve_in."""
    if resolve_path is not None:
        return resolve_path

    def local(rel: str, *, write: bool = False) -> Path:
        return resolve_in(workspace, rel)

    return local


def milestone_rel(milestone: str) -> str:
    """deliverables/<milestone>, workspace-relative, for a safe milestone id."""
    return f"{DELIVERABLES_DIR}/{_safe_name(milestone, 'milestone')}"


def milestone_dir(workspace: Path, milestone: str) -> Path:
    return Path(workspace) / milestone_rel(milestone)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _rel(workspace: Path, path: Path) -> str:
    return path.resolve().relative_to(Path(workspace).resolve()).as_posix()


# --- tools ----------------------------------------------------------------------------

def list_tables(workspace: Path, *, fetch=None, run=None) -> dict:
    """Inventory every table loaded from inputs/: source file, columns, types, row count."""
    session = open_session(workspace)
    try:
        tables = []
        for name, cols in session.tables.items():
            count = session.conn.execute(f"SELECT COUNT(*) FROM {_ident(name)}").fetchone()[0]
            tables.append({"table": name, "source": session.sources[name], "rows": count,
                           "columns": [{"name": c, "type": t} for c, t in cols]})
        return {"tables": tables, "warnings": session.warnings}
    finally:
        session.close()


def profile_table(session: Session, table: str) -> dict:
    """Data-quality profile of one table (row count, per-column nulls/distinct/min/max/top)."""
    if table not in session.tables:
        raise ToolError(f"unknown table {table!r}; known: {', '.join(session.tables) or 'none'}")
    t = _ident(table)
    conn = session.conn
    row_count = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    duplicate_rows = row_count - conn.execute(f"SELECT COUNT(*) FROM (SELECT DISTINCT * FROM {t})").fetchone()[0]
    columns = []
    first_column = session.tables[table][0][0] if session.tables[table] else ""
    for name, col_type in session.tables[table]:
        c = _ident(name)
        nulls, distinct, lo, hi = conn.execute(
            f"SELECT SUM({c} IS NULL), COUNT(DISTINCT {c}), MIN({c}), MAX({c}) FROM {t}").fetchone()
        nulls = nulls or 0
        top = conn.execute(
            f"SELECT {c}, COUNT(*) AS n FROM {t} WHERE {c} IS NOT NULL "
            f"GROUP BY {c} ORDER BY n DESC, {c} LIMIT 5").fetchall()
        non_null = row_count - nulls
        entry = {
            "name": name, "type": col_type, "nulls": nulls,
            "null_rate": round(nulls / row_count, 4) if row_count else 0.0,
            "distinct": distinct, "min": _jsonable(lo), "max": _jsonable(hi),
            "top_values": [{"value": _jsonable(v), "count": n} for v, n in top],
        }
        # A leading id column is treated as the key: report duplicates so M1
        # surfaces broken keys.
        if name == first_column and (name == "id" or name.endswith("_id")):
            entry["duplicate_keys"] = non_null - distinct
        columns.append(entry)
    return {"table": table, "source": session.sources[table], "row_count": row_count,
            "duplicate_rows": duplicate_rows, "columns": columns}


def profile_tables(workspace: Path, *, fetch=None, run=None, resolve_path: Resolver | None = None,
                   tables: list[str] | None = None, milestone: str = "m1-profile") -> dict:
    """Profile tables (default: all) and merge them into deliverables/<milestone>/profile.json."""
    out = resolver(workspace, resolve_path)(f"{milestone_rel(milestone)}/profile.json", write=True)
    session = open_session(workspace)
    try:
        wanted = list(tables) if tables else list(session.tables)
        profiles = [profile_table(session, name) for name in wanted]
        inputs = {rel: _sha256_file(Path(workspace) / rel) for rel in sorted(set(session.sources.values()))}
    finally:
        session.close()
    doc = {"inputs": {}, "tables": {}}
    if out.exists():
        try:
            doc = json.loads(out.read_text(encoding="utf-8"))
        except ValueError:
            pass
    doc["inputs"] = inputs
    doc.setdefault("tables", {})
    for p in profiles:
        doc["tables"][p["table"]] = p
    _write(out, json.dumps(doc, indent=2, sort_keys=True) + "\n")
    return {"path": _rel(workspace, out), "profiled": wanted,
            "summary": [{"table": p["table"], "rows": p["row_count"], "duplicate_rows": p["duplicate_rows"],
                         "columns_with_nulls": [c["name"] for c in p["columns"] if c["nulls"]]}
                        for p in profiles]}


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_query(workspace: Path, *, fetch=None, run=None, sql: str, limit: int = PREVIEW_ROWS) -> dict:
    """Run one read-only query and return a preview plus the full result hash."""
    limit = max(1, min(int(limit), MAX_PREVIEW_ROWS))
    session = open_session(workspace)
    try:
        result = execute(session, sql)
    finally:
        session.close()
    return {"columns": result.columns, "rows": [[_jsonable(v) for v in r] for r in result.rows[:limit]],
            "row_count": len(result.rows), "truncated": len(result.rows) > limit,
            "sha256": result.sha256}


def save_query(workspace: Path, *, fetch=None, run=None, resolve_path: Resolver | None = None,
               name: str, sql: str, milestone: str, description: str = "") -> dict:
    """Execute a read-only query and save it with its result so reviewers can re-run it."""
    _safe_name(name, "name")
    path = resolver(workspace, resolve_path)
    base = milestone_rel(milestone)
    query_path = path(f"{base}/queries/{name}.sql", write=True)
    result_path = path(f"{base}/results/{name}.csv", write=True)
    session = open_session(workspace)
    try:
        result = execute(session, sql)
    finally:
        session.close()
    header = "".join(f"-- {line}\n" for line in description.strip().splitlines()) if description.strip() else ""
    _write(query_path, header + check_sql(sql) + ";\n")
    _write(result_path, result_csv(result))
    return {"query_path": _rel(workspace, query_path), "result_path": _rel(workspace, result_path),
            "columns": result.columns, "row_count": len(result.rows), "sha256": result.sha256,
            "preview": [[_jsonable(v) for v in r] for r in result.rows[:10]]}


def load_saved_query(workspace: Path, milestone: str, name: str) -> str:
    path = milestone_dir(workspace, milestone) / "queries" / f"{_safe_name(name, 'query')}.sql"
    if not path.is_file():
        raise ToolError(f"no saved query {name!r} in {milestone}; save it with save_query first")
    return path.read_text(encoding="utf-8")


def pick_value(result: QueryResult, column: str, row: int = 0) -> Any:
    if column not in result.columns:
        raise ToolError(f"column {column!r} not in result columns {result.columns}")
    if not 0 <= int(row) < len(result.rows):
        raise ToolError(f"row {row} out of range; the query returned {len(result.rows)} row(s)")
    return result.rows[int(row)][result.columns.index(column)]


def format_value(value: float, unit: str = "", decimals: int | None = None) -> str:
    """How a figure must appear in the report text (e.g. $12,480.00, 4.2%, 1,204)."""
    if unit == "usd":
        d = 2 if decimals is None else int(decimals)
        text = f"${abs(value):,.{d}f}"
        return "-" + text if value < 0 else text
    if unit == "pct":
        return f"{value:,.{1 if decimals is None else int(decimals)}f}%"
    if unit == "count":
        return f"{int(round(value)):,}"
    if decimals is None and isinstance(value, int):
        return f"{value:,}"
    return f"{value:,.{2 if decimals is None else int(decimals)}f}"


FIGURE_UNITS = ("", "usd", "pct", "count")


def record_figure(workspace: Path, *, fetch=None, run=None, resolve_path: Resolver | None = None,
                  milestone: str, figure_id: str, query: str, column: str, row: int = 0,
                  label: str = "", unit: str = "", decimals: int | None = None) -> dict:
    """Re-run a saved query, take one value from it and record it in figures.json.

    The report must cite it as [F:<figure_id>] on the same line as its display text.
    """
    _safe_name(figure_id, "figure_id")
    if unit not in FIGURE_UNITS:
        raise ToolError(f"unit must be one of {FIGURE_UNITS}")
    sql = load_saved_query(workspace, milestone, query)
    session = open_session(workspace)
    try:
        result = execute(session, sql)
    finally:
        session.close()
    value = pick_value(result, column, row)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolError(f"figure values must be numeric; got {value!r}")
    figure = {"id": figure_id, "label": label, "query": query, "column": column, "row": int(row),
              "value": value, "unit": unit, "decimals": decimals,
              "display": format_value(value, unit, decimals)}
    path = resolver(workspace, resolve_path)(f"{milestone_rel(milestone)}/figures.json", write=True)
    figures = load_figures(path) if path.exists() else []
    figures = [f for f in figures if f.get("id") != figure_id] + [figure]
    _write(path, json.dumps({"figures": figures}, indent=2) + "\n")
    return {"figure": figure, "path": _rel(workspace, path),
            "cite_as": f"{figure['display']} [F:{figure_id}]"}


def load_figures(path: Path) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    figures = data.get("figures") if isinstance(data, dict) else None
    if not isinstance(figures, list):
        raise ToolError("figures.json must be an object with a 'figures' list")
    return figures


# --- metric definitions and reconciliation -------------------------------------------

METRIC_FIELDS = ("name", "description", "grain", "query", "value_column", "owner")


def load_metrics(path: Path) -> list[dict]:
    if not Path(path).is_file():
        raise ToolError(f"metrics file not found: {Path(path).name}")
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    metrics = data.get("metrics") if isinstance(data, dict) else None
    if not isinstance(metrics, list):
        raise ToolError("metrics file must be a mapping with a 'metrics' list")
    return metrics


def load_reference(path: Path) -> dict[str, dict]:
    """Customer reference totals: CSV with columns metric, value[, tolerance_pct, note]."""
    with Path(path).open(newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    ref = {}
    for r in rows:
        name = (r.get("metric") or "").strip()
        if not name:
            continue
        tol = (r.get("tolerance_pct") or "").strip()
        ref[name] = {"value": float(r["value"]), "tolerance_pct": float(tol) if tol else None,
                     "note": (r.get("note") or "").strip()}
    return ref


def metric_value(workspace: Path, session: Session, base: Path, metric: dict,
                 resolve_path: Resolver | None = None) -> float:
    """Execute a metric's query file (relative to the milestone folder) and return its value."""
    rel = _rel(workspace, base) + "/" + str(metric.get("query", ""))
    query_path = resolver(workspace, resolve_path)(rel)
    if not query_path.is_file():
        raise ToolError(f"metric {metric.get('name')!r}: query file {metric.get('query')!r} not found")
    result = execute(session, query_path.read_text(encoding="utf-8"))
    value = pick_value(result, str(metric.get("value_column", "")), 0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolError(f"metric {metric.get('name')!r}: value is not numeric ({value!r})")
    return float(value)


def evaluate_metrics(workspace: Path, milestone: str, reference_path: str,
                     tolerance_pct: float = 0.5, *, resolve_path: Resolver | None = None) -> list[dict]:
    """Recompute every metric and compare it with the reference totals."""
    base = milestone_dir(workspace, milestone)
    metrics = load_metrics(base / "metrics.yaml")
    reference = load_reference(resolver(workspace, resolve_path)(reference_path))
    rows = []
    session = open_session(workspace)
    try:
        names = set()
        for m in metrics:
            name = str(m.get("name", ""))
            names.add(name)
            try:
                computed = metric_value(workspace, session, base, m, resolve_path)
                error = ""
            except ToolError as exc:
                computed, error = None, str(exc)
            ref = reference.get(name)
            tol = tolerance_pct
            if ref and ref["tolerance_pct"] is not None:
                tol = ref["tolerance_pct"]
            row = {"metric": name, "computed": computed, "reference": ref["value"] if ref else None,
                   "diff_pct": None, "tolerance_pct": tol if ref else None, "status": "no_reference"}
            if error:
                row["status"] = "error: " + error
            elif ref:
                diff = _diff_pct(computed, ref["value"])
                row["diff_pct"] = round(diff, 4)
                row["status"] = "match" if diff <= tol else "mismatch"
            rows.append(row)
        for name, ref in reference.items():
            if name not in names:
                rows.append({"metric": name, "computed": None, "reference": ref["value"], "diff_pct": None,
                             "tolerance_pct": tolerance_pct if ref["tolerance_pct"] is None
                             else ref["tolerance_pct"], "status": "missing_metric"})
    finally:
        session.close()
    return rows


def _diff_pct(computed: float, reference: float) -> float:
    if reference == 0:
        return 0.0 if computed == 0 else float("inf")
    return abs(computed - reference) / abs(reference) * 100.0


RECONCILIATION_COLUMNS = ("metric", "computed", "reference", "diff_pct", "tolerance_pct", "status")


def reconcile_metrics(workspace: Path, *, fetch=None, run=None, resolve_path: Resolver | None = None,
                      milestone: str = "m2-metrics",
                      reference_path: str = "inputs/reference_totals.csv",
                      tolerance_pct: float = 0.5) -> dict:
    """Recompute metrics.yaml and write reconciliation.csv against the reference totals."""
    out = resolver(workspace, resolve_path)(f"{milestone_rel(milestone)}/reconciliation.csv", write=True)
    rows = evaluate_metrics(workspace, milestone, reference_path, tolerance_pct, resolve_path=resolve_path)
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(RECONCILIATION_COLUMNS)
    for r in rows:
        writer.writerow(["" if r[k] is None else (cell(r[k]) if k == "computed" else r[k])
                         for k in RECONCILIATION_COLUMNS])
    _write(out, buf.getvalue())
    return {"path": _rel(workspace, out), "rows": rows,
            "all_match": all(r["status"] in ("match", "no_reference") for r in rows)}


# --- tool table ------------------------------------------------------------------------

_MILESTONE = {"type": "string", "description": "Milestone id, e.g. m3-analysis."}

TOOL_DEFS = [
    {"name": "list_tables", "risk": "read", "function": list_tables,
     "description": "List every table loaded from inputs/ (CSV files and SQLite databases) with its "
                    "source file, columns, inferred types and row count.",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "profile_tables", "risk": "write", "function": profile_tables,
     "description": "Compute a data-quality profile (row count, duplicate rows, per-column nulls, "
                    "distinct values, min/max, most common values) and merge it into "
                    "deliverables/<milestone>/profile.json.",
     "input_schema": {"type": "object", "properties": {
         "tables": {"type": "array", "items": {"type": "string"},
                    "description": "Tables to profile; omit for all."},
         "milestone": _MILESTONE}, "additionalProperties": False}},
    {"name": "run_query", "risk": "read", "function": run_query,
     "description": "Run one read-only SQLite SELECT/WITH query over the inputs and return the "
                    "columns, up to `limit` rows and a hash of the full result. Writes are refused.",
     "input_schema": {"type": "object", "properties": {
         "sql": {"type": "string"},
         "limit": {"type": "integer", "minimum": 1, "maximum": MAX_PREVIEW_ROWS}},
         "required": ["sql"], "additionalProperties": False}},
    {"name": "save_query", "risk": "write", "function": save_query,
     "description": "Execute a read-only query and save it as deliverables/<milestone>/queries/<name>.sql "
                    "with its full result in results/<name>.csv, so a reviewer can re-run it.",
     "input_schema": {"type": "object", "properties": {
         "name": {"type": "string", "description": "lowercase name, e.g. revenue_by_month"},
         "sql": {"type": "string"},
         "milestone": _MILESTONE,
         "description": {"type": "string", "description": "What the query answers; saved as a comment."}},
         "required": ["name", "sql", "milestone"], "additionalProperties": False}},
    {"name": "record_figure", "risk": "write", "function": record_figure,
     "description": "Re-run a saved query, take the value at (row, column) and record it in "
                    "deliverables/<milestone>/figures.json. Returns the exact display text; the report "
                    "must show it followed by the marker [F:<figure_id>].",
     "input_schema": {"type": "object", "properties": {
         "milestone": _MILESTONE,
         "figure_id": {"type": "string"},
         "query": {"type": "string", "description": "Name of a query saved with save_query."},
         "column": {"type": "string"},
         "row": {"type": "integer", "minimum": 0},
         "label": {"type": "string"},
         "unit": {"type": "string", "enum": list(FIGURE_UNITS)},
         "decimals": {"type": "integer", "minimum": 0, "maximum": 6}},
         "required": ["milestone", "figure_id", "query", "column"], "additionalProperties": False}},
    {"name": "reconcile_metrics", "risk": "write", "function": reconcile_metrics,
     "description": "Execute every metric in deliverables/<milestone>/metrics.yaml and compare it with "
                    "the customer's reference totals; writes reconciliation.csv.",
     "input_schema": {"type": "object", "properties": {
         "milestone": _MILESTONE,
         "reference_path": {"type": "string", "description": "Workspace path of the reference CSV."},
         "tolerance_pct": {"type": "number", "minimum": 0}},
         "additionalProperties": False}},
]
