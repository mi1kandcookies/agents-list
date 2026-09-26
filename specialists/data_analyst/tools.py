"""
specialists/data_analyst/tools.py - deterministic domain tools for the
data-analyst specialist.

The customer's data arrives as files under inputs/: CSV exports and/or SQLite
databases. Every call loads them into a fresh in-memory SQLite session, then
locks that session down (PRAGMA query_only plus an authorizer that only
permits reads), so a query can never change the customer's data or the
agent's own deliverables. Each query runs under a time limit and a row cap,
and reports which input tables it read. The customer's reference totals
(inputs/reference_totals.csv) are never loaded as a table: they are the
answer key M2 reconciles against, so a metric must not be able to copy them.
The tools:

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
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

import yaml

from agentkit.errors import PolicyViolation, ToolError
from agentkit.policy import INTERNAL_DIR, jail_path

INPUTS_DIR = "inputs"
DELIVERABLES_DIR = "deliverables"
REFERENCE_FILE = "inputs/reference_totals.csv"
CSV_SUFFIXES = {".csv"}
SQLITE_SUFFIXES = {".sqlite", ".sqlite3", ".db"}
PREVIEW_ROWS = 50
MAX_PREVIEW_ROWS = 500
QUERY_TIMEOUT_SECONDS = 90.0      # one query; a runaway join or recursive CTE is stopped
MAX_RESULT_ROWS = 100_000         # rows one query may return; aggregate or LIMIT beyond that
# A result value equal to a numeric literal this large in its own SQL was
# typed in, not computed. Small literals (ROUND digits, substr positions,
# 100 in a percentage) are left alone: they coincide with real results.
TYPED_LITERAL_FLOOR = 100

_SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,63}$")
_IDENT_JUNK = re.compile(r"[^0-9a-zA-Z_]+")
_INT = re.compile(r"^[+-]?(0|[1-9][0-9]*)$")
_INT64_MIN, _INT64_MAX = -(2 ** 63), 2 ** 63 - 1
_LEADING_ZERO = re.compile(r"^[+-]?0[0-9]")   # zip codes, account numbers
_FLOAT = re.compile(r"^[+-]?([0-9]+\.[0-9]*|\.[0-9]+|[0-9]+)([eE][+-]?[0-9]+)?$")
# The digits of a number as finance exports write it, once any sign, "$" and
# parentheses are taken off: 1,200.00 (proper thousands groups only), 35, .5
_GROUPED_NUMBER = re.compile(r"^(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?$|^\.\d+$")
_NUMBER_LITERAL = re.compile(r"(?<![\w.])(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?(?![\w.])")
_METRIC_QUERY = re.compile(r"^queries/[a-z0-9][a-z0-9_\-]{0,63}\.sql$")

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
    not_loaded: dict[str, str] = field(default_factory=dict)                # inputs/ file -> why

    def close(self) -> None:
        self.conn.close()


def _ident(name: str) -> str:
    """Quote an SQL identifier."""
    return '"' + name.replace('"', '""') + '"'


def _clean_name(raw: str, fallback: str) -> str:
    name = _IDENT_JUNK.sub("_", raw.strip()).strip("_").lower() or fallback
    return "t_" + name if name[0].isdigit() else name


def parse_number(text: Any) -> float | None:
    """A number as spreadsheets and finance exports write it, or None.

    Accepts plain numbers (12, -3.5, 1e3) and formatted ones: $1,200.00,
    -$12.50, $-12.50 and accounting negatives such as (1,200.00). Thousands
    groups must be proper (1,200 but not 1,2), so lists and prose stay text.
    """
    s = str(text).strip()
    if _FLOAT.match(s):
        return float(s)
    negative = False
    if len(s) > 2 and s[0] == "(" and s[-1] == ")":
        negative, s = True, s[1:-1].strip()
    if s and s[0] in "+-":
        negative, s = negative != (s[0] == "-"), s[1:]
    if s.startswith("$"):
        s = s[1:]
        if s and s[0] in "+-":
            negative, s = negative != (s[0] == "-"), s[1:]
    if not _GROUPED_NUMBER.match(s):
        return None
    value = float(s.replace(",", ""))
    return -value if negative else value


# A CSV column of numbers written with "$", thousands separators or
# accounting parentheses. Loaded as REAL (a warning says so); as TEXT,
# SQLite's SUM would read "1,200.00" as 1 without any error.
FORMATTED = "FORMATTED"


def _infer_type(values: list[str]) -> str:
    """INTEGER, REAL, FORMATTED or TEXT for a CSV column. IDs with leading
    zeros, and whole numbers too large for a 64-bit integer, stay TEXT."""
    seen = [v.strip() for v in values if v.strip() != ""]
    if not seen or any(_LEADING_ZERO.match(v) for v in seen):
        return "TEXT"
    if all(_INT.match(v) for v in seen):
        return "INTEGER" if all(_INT64_MIN <= int(v) <= _INT64_MAX for v in seen) else "TEXT"
    if all(_FLOAT.match(v) for v in seen):
        return "REAL"
    if all(parse_number(v) is not None for v in seen):
        return FORMATTED
    return "TEXT"


def _convert(value: str, col_type: str) -> Any:
    value = value.strip()
    if value == "":
        return None
    if col_type == "INTEGER":
        return int(value)
    if col_type == "REAL":
        return float(value)
    if col_type == FORMATTED:
        return parse_number(value)
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
    formatted = [c for c, t in zip(columns, types) if t == FORMATTED]
    if formatted:
        session.warnings.append(
            f"{path.name}: column(s) {', '.join(formatted)} hold numbers written with '$', thousands "
            "separators or parentheses; loaded as REAL numbers")
    cols_sql = ", ".join(f"{_ident(c)} {'REAL' if t == FORMATTED else t}" for c, t in zip(columns, types))
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


def _file_key(path: Path) -> str:
    return os.path.normcase(str(Path(path).resolve()))


def open_session(workspace: Path, *, exclude: Iterable[str | Path] = (REFERENCE_FILE,)) -> Session:
    """Load inputs/ into memory and return a session that can only read.

    Files in `exclude` (workspace-relative; by default the reference totals)
    are not loaded, so no query can read them.
    """
    workspace = Path(workspace)
    skip = {_file_key(workspace / p) for p in exclude if p}
    conn = sqlite3.connect(":memory:")
    session = Session(conn=conn)
    for path in input_files(workspace):
        rel = path.relative_to(workspace).as_posix()
        if _file_key(path) in skip:
            session.not_loaded[rel] = ("reference totals: not queryable, so no metric can copy them; "
                                       "read the file with read_file")
            continue
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
        word = bad.group(0)
        raise ToolError(f"read-only session: {word.upper()} is not allowed. If {word!r} is a column or "
                        f'table name, quote it as an identifier: "{word}"')
    return sql.strip().rstrip(";").strip()


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[tuple]
    tables: list[str] = field(default_factory=list)   # input tables the query reads

    @property
    def sha256(self) -> str:
        return result_digest(self.columns, self.rows)


def execute(session: Session, sql: str, *, timeout: float = QUERY_TIMEOUT_SECONDS,
            max_rows: int = MAX_RESULT_ROWS) -> QueryResult:
    """Run one guarded read-only query in `session`, bounded in time and rows.

    The result names the input tables the query read, as SQLite's authorizer
    reports them when the statement is prepared.
    """
    text = check_sql(sql)
    conn = session.conn
    read: set[str] = set()
    # The authorizer reports a table name as the query spells it.
    names = {name.lower(): name for name in session.tables}

    def authorize(action: int, arg1: Any, *_rest: Any) -> int:
        if action == sqlite3.SQLITE_READ and isinstance(arg1, str) and arg1.lower() in names:
            read.add(names[arg1.lower()])
        return _authorizer(action)

    deadline = time.monotonic() + float(timeout)
    # Setting an authorizer also expires cached statements, so every call
    # re-prepares and `read` is always filled.
    conn.set_authorizer(authorize)
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 1000)
    cur = conn.cursor()
    rows: list[tuple] = []
    try:
        cur.execute(text)
        columns = [d[0] for d in (cur.description or [])]
        while True:
            chunk = cur.fetchmany(1000)
            if not chunk:
                break
            rows.extend(chunk)
            if len(rows) > max_rows:
                raise ToolError(f"query returned more than {max_rows:,} rows; aggregate the result or "
                                "add a LIMIT")
    except sqlite3.DatabaseError as exc:
        if time.monotonic() > deadline:
            raise ToolError(f"query stopped after the {float(timeout):g}-second time limit; check the "
                            "join conditions or aggregate earlier") from None
        raise ToolError(f"SQL error: {exc}") from exc
    finally:
        cur.close()
        conn.set_progress_handler(None, 0)
        conn.set_authorizer(_authorizer)
    return QueryResult(columns=columns, rows=rows, tables=sorted(read))


def require_input_tables(result: QueryResult, what: str) -> None:
    """Refuse a query that reads no input table: its numbers are typed, not computed."""
    if not result.tables:
        raise ToolError(f"{what} reads none of the input tables; a saved query must compute its "
                        "result from the customer's data")


def typed_literals(sql: str, floor: float = TYPED_LITERAL_FLOOR) -> set[float]:
    """Numeric literals with an absolute value above `floor` written into
    `sql` (outside strings and comments)."""
    out = set()
    for token in _NUMBER_LITERAL.findall(_strip_sql(sql)):
        value = float(token)
        if abs(value) > floor:
            out.add(value)
    return out


def _same_number(a: float, b: float) -> bool:
    return abs(float(a) - float(b)) <= 1e-9 * max(1.0, abs(float(b)))


def reject_typed_value(value: float, sql: str, what: str, *, target: float | None = None) -> None:
    """Refuse a value that appears as a literal in its own query.

    Literals above TYPED_LITERAL_FLOOR always count. With a `target` (the
    reference total a metric must hit), a value equal to the target that is
    also written into the SQL counts at any size except 0, 1 and 100, which
    ordinary filters and percentages use. A literal's sign is not part of
    it (-4436.0 is 4436.0 negated), so values compare by magnitude.
    """
    size = abs(float(value))
    literals = typed_literals(sql)
    if target is not None and _same_number(value, target) and size not in (0.0, 1.0, 100.0):
        literals |= {lit for lit in typed_literals(sql, floor=0) if _same_number(lit, size)}
    if any(_same_number(size, lit) for lit in literals):
        raise ToolError(f"{what}: the value {value!r} is written into the SQL as a literal; compute it "
                        "from the data instead")


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
        out: dict[str, Any] = {"tables": tables, "warnings": session.warnings}
        if session.not_loaded:
            out["not_loaded"] = [{"source": rel, "reason": why} for rel, why in session.not_loaded.items()]
        return out
    finally:
        session.close()


# Value-bearing profile fields; left out for masked (personal-data) columns.
VALUE_FIELDS = ("min", "max", "top_values")


def mask_for(table: str, mask_columns: Iterable[str] | None) -> set[str]:
    """Columns of `table` named in mask_columns ("column" or "table.column")."""
    out = set()
    for item in mask_columns or ():
        tab, _, col = str(item).strip().rpartition(".")
        if not tab or tab == table:
            out.add(col)
    return out


def _holds_emails(conn: sqlite3.Connection, t: str, c: str) -> bool:
    return conn.execute(f"SELECT EXISTS (SELECT 1 FROM {t} WHERE CAST({c} AS TEXT) LIKE '%_@_%._%')"
                        ).fetchone()[0] == 1


def profile_table(session: Session, table: str, mask: Iterable[str] = ()) -> dict:
    """Data-quality profile of one table (row count, per-column nulls/distinct/min/max/top).

    Columns in `mask`, and any column holding e-mail addresses, are masked:
    their nulls and distinct counts are kept but no values are reported.
    """
    if table not in session.tables:
        raise ToolError(f"unknown table {table!r}; known: {', '.join(session.tables) or 'none'}")
    mask = set(mask)
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
        non_null = row_count - nulls
        entry: dict[str, Any] = {
            "name": name, "type": col_type, "nulls": nulls,
            "null_rate": round(nulls / row_count, 4) if row_count else 0.0,
            "distinct": distinct,
        }
        if name in mask or _holds_emails(conn, t, c):
            entry["masked"] = True
        else:
            top = conn.execute(
                f"SELECT {c}, COUNT(*) AS n FROM {t} WHERE {c} IS NOT NULL "
                f"GROUP BY {c} ORDER BY n DESC, {c} LIMIT 5").fetchall()
            entry.update({"min": _jsonable(lo), "max": _jsonable(hi),
                          "top_values": [{"value": _jsonable(v), "count": n} for v, n in top]})
        # A leading id column is treated as the key: report duplicates so M1
        # surfaces broken keys.
        if name == first_column and (name == "id" or name.endswith("_id")):
            entry["duplicate_keys"] = non_null - distinct
        columns.append(entry)
    return {"table": table, "source": session.sources[table], "row_count": row_count,
            "duplicate_rows": duplicate_rows, "columns": columns}


def profile_tables(workspace: Path, *, fetch=None, run=None, resolve_path: Resolver | None = None,
                   tables: list[str] | None = None, milestone: str = "m1-profile",
                   mask_columns: list[str] | None = None) -> dict:
    """Profile tables (default: all) and merge them into deliverables/<milestone>/profile.json.

    mask_columns ("column" or "table.column") are profiled without values;
    columns holding e-mail addresses are always masked.
    """
    out = resolver(workspace, resolve_path)(f"{milestone_rel(milestone)}/profile.json", write=True)
    session = open_session(workspace)
    try:
        wanted = list(tables) if tables else list(session.tables)
        profiles = [profile_table(session, name, mask_for(name, mask_columns)) for name in wanted]
        known = {t: {c for c, _ in cols} for t, cols in session.tables.items()}
        inputs = {rel: _sha256_file(Path(workspace) / rel) for rel in sorted(set(session.sources.values()))}
    finally:
        session.close()
    unmatched = [m for m in mask_columns or () if not any(mask_for(t, [m]) & cols for t, cols in known.items())]
    doc: dict[str, Any] = {"inputs": {}, "tables": {}}
    if out.exists():
        try:
            doc = json.loads(out.read_text(encoding="utf-8"))
        except ValueError:
            pass
    if not isinstance(doc, dict) or not isinstance(doc.get("tables"), dict):
        doc = {"inputs": {}, "tables": {}}
    doc["inputs"] = inputs
    for p in profiles:
        doc["tables"][p["table"]] = p
    doc["masked_columns"] = sorted(f"{t}.{c['name']}" for t, p in doc["tables"].items() if isinstance(p, dict)
                                   for c in p.get("columns", []) if isinstance(c, dict) and c.get("masked"))
    _write(out, json.dumps(doc, indent=2, sort_keys=True) + "\n")
    result: dict[str, Any] = {
        "path": _rel(workspace, out), "profiled": wanted, "masked_columns": doc["masked_columns"],
        "summary": [{"table": p["table"], "rows": p["row_count"], "duplicate_rows": p["duplicate_rows"],
                     "columns_with_nulls": [c["name"] for c in p["columns"] if c["nulls"]]}
                    for p in profiles]}
    if unmatched:
        result["unmatched_mask_columns"] = unmatched
    return result


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
            "tables_read": result.tables, "sha256": result.sha256}


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
    require_input_tables(result, f"query {name!r}")
    header = "".join(f"-- {line}\n" for line in description.strip().splitlines()) if description.strip() else ""
    _write(query_path, header + check_sql(sql) + ";\n")
    _write(result_path, result_csv(result))
    return {"query_path": _rel(workspace, query_path), "result_path": _rel(workspace, result_path),
            "columns": result.columns, "row_count": len(result.rows), "tables_read": result.tables,
            "sha256": result.sha256, "preview": [[_jsonable(v) for v in r] for r in result.rows[:10]]}


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


def computed_value(session: Session, sql: str, column: str, row: int, what: str, *,
                   target: float | None = None) -> int | float:
    """The number at (row, column) of `sql`, refused unless it is computed
    from the input tables: the query must read one, and the value must not be
    a literal typed into the SQL (see reject_typed_value for `target`)."""
    result = execute(session, sql)
    require_input_tables(result, what)
    value = pick_value(result, column, row)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolError(f"{what}: value {value!r} is not numeric")
    reject_typed_value(value, sql, what, target=target)
    return value


def format_value(value: float, unit: str = "", decimals: int | None = None) -> str:
    """How a figure must appear in the report text (e.g. $12,480.00, 4.2%, 1,204).

    "pct" takes a value already in percent (24.0 -> 24.0%); "ratio" takes a
    fraction and shows it as a percent (0.24 -> 24.0%).
    """
    if unit == "usd":
        d = 2 if decimals is None else int(decimals)
        text = f"${abs(value):,.{d}f}"
        return "-" + text if value < 0 else text
    if unit in ("pct", "ratio"):
        pct = value * 100 if unit == "ratio" else value
        return f"{pct:,.{1 if decimals is None else int(decimals)}f}%"
    if unit == "count":
        return f"{int(round(value)):,}"
    if decimals is None and isinstance(value, int):
        return f"{value:,}"
    return f"{value:,.{2 if decimals is None else int(decimals)}f}"


FIGURE_UNITS = ("", "usd", "pct", "ratio", "count")


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
        value = computed_value(session, sql, column, row, f"figure {figure_id!r}")
    finally:
        session.close()
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
    """Customer reference totals: CSV with columns metric, value[, tolerance_pct, note].

    Values may be formatted as finance files write them ($4,436.00). A row
    whose value or tolerance is not a number carries an "error" instead of
    failing the whole file.
    """
    try:
        with Path(path).open(newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise ToolError(f"cannot read the reference totals {Path(path).name}: {type(exc).__name__}") from None
    ref: dict[str, dict] = {}
    for r in rows:
        name = (r.get("metric") or "").strip()
        if not name:
            continue
        raw_value, raw_tol = (r.get("value") or "").strip(), (r.get("tolerance_pct") or "").strip()
        value, tol = parse_number(raw_value), parse_number(raw_tol) if raw_tol else None
        error = ""
        if value is None:
            error = f"reference value {raw_value!r} is not a number"
        elif raw_tol and (tol is None or tol < 0):
            error = f"reference tolerance_pct {raw_tol!r} is not a non-negative number"
        ref[name] = {"value": value, "tolerance_pct": tol, "note": (r.get("note") or "").strip(),
                     "error": error}
    return ref


def metric_value(workspace: Path, session: Session, base: Path, metric: dict,
                 resolve_path: Resolver | None = None, *, reference: float | None = None) -> int | float:
    """Execute a metric's saved query (queries/<name>.sql under the milestone
    folder, so it is re-run by queries_reexecute and hashed with the
    evidence) and return its computed value. With the metric's `reference`
    total, a query that hits it by writing it into the SQL is refused."""
    what = f"metric {metric.get('name')!r}"
    query = str(metric.get("query", ""))
    if not _METRIC_QUERY.match(query):
        raise ToolError(f"{what}: query must name a saved query as queries/<name>.sql, got {query!r}")
    query_path = resolver(workspace, resolve_path)(_rel(workspace, base) + "/" + query)
    if not query_path.is_file():
        raise ToolError(f"{what}: query file {query!r} not found")
    return computed_value(session, query_path.read_text(encoding="utf-8"),
                          str(metric.get("value_column", "")), 0, what, target=reference)


def reference_file(workspace: Path, reference_path: str | None = None,
                   resolve_path: Resolver | None = None) -> tuple[Path, bool]:
    """The reference totals file and whether it exists (default REFERENCE_FILE)."""
    path = resolver(workspace, resolve_path)(reference_path or REFERENCE_FILE)
    return path, path.is_file()


def evaluate_metrics(workspace: Path, milestone: str, reference_path: str | None = None,
                     tolerance_pct: float = 0.5, *, resolve_path: Resolver | None = None,
                     require_reference: bool | None = None) -> list[dict]:
    """Recompute every metric and compare it with the reference totals.

    The metrics run in a session without the reference file, so no metric
    can read the numbers it is compared with. A metric whose query returns a
    whole number (a count, or a sum of integer cents) must tie exactly unless
    its reference row sets tolerance_pct; other values use `tolerance_pct`.
    A missing reference file is an error when `require_reference` is true
    (default: when a reference_path is given); otherwise every metric is
    recomputed and marked "no_reference".
    """
    base = milestone_dir(workspace, milestone)
    metrics = load_metrics(base / "metrics.yaml")
    ref_path, found = reference_file(workspace, reference_path, resolve_path)
    if require_reference is None:
        require_reference = reference_path is not None
    if found:
        reference = load_reference(ref_path)
    elif require_reference:
        raise ToolError(f"reference totals not found: {reference_path or REFERENCE_FILE}")
    else:
        reference = {}
    rows = []
    session = open_session(workspace, exclude=(REFERENCE_FILE, ref_path))
    try:
        names = set()
        for m in metrics:
            name = str(m.get("name", "")) if isinstance(m, dict) else ""
            names.add(name)
            ref = reference.get(name)
            try:
                if not isinstance(m, dict):
                    raise ToolError("metric entry is not a mapping")
                computed = metric_value(workspace, session, base, m, resolve_path,
                                        reference=ref["value"] if ref else None)
                error = ""
            except ToolError as exc:
                computed, error = None, str(exc)
            tol = None
            if ref:
                tol = ref["tolerance_pct"]
                if tol is None:
                    tol = 0.0 if isinstance(computed, int) else float(tolerance_pct)
            row = {"metric": name, "computed": computed, "reference": ref["value"] if ref else None,
                   "diff_pct": None, "tolerance_pct": tol, "status": "no_reference"}
            if error:
                row["status"] = "error: " + error
            elif ref and ref["error"]:
                row["status"] = "error: " + ref["error"]
            elif ref:
                diff = _diff_pct(computed, ref["value"])
                row["diff_pct"] = round(diff, 4)
                row["status"] = "match" if diff <= tol else "mismatch"
            rows.append(row)
        for name, ref in reference.items():
            if name not in names:
                rows.append({"metric": name, "computed": None, "reference": ref["value"], "diff_pct": None,
                             "tolerance_pct": float(tolerance_pct) if ref["tolerance_pct"] is None
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
                      milestone: str = "m2-metrics", reference_path: str | None = None,
                      tolerance_pct: float = 0.5) -> dict:
    """Recompute metrics.yaml and write reconciliation.csv against the reference totals.

    Without reference_path the default inputs/reference_totals.csv is used
    when the customer supplied one; otherwise every metric is recomputed and
    marked no_reference.
    """
    out = resolver(workspace, resolve_path)(f"{milestone_rel(milestone)}/reconciliation.csv", write=True)
    rows = evaluate_metrics(workspace, milestone, reference_path, tolerance_pct, resolve_path=resolve_path)
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(RECONCILIATION_COLUMNS)
    for r in rows:
        writer.writerow(["" if r[k] is None else (cell(r[k]) if k == "computed" else r[k])
                         for k in RECONCILIATION_COLUMNS])
    _write(out, buf.getvalue())
    result = {"path": _rel(workspace, out), "rows": rows,
              "all_match": all(r["status"] in ("match", "no_reference") for r in rows)}
    if all(r["status"] == "no_reference" for r in rows):
        result["note"] = ("no reference totals to reconcile against; every metric was recomputed and "
                          "marked no_reference")
    return result


# --- tool table ------------------------------------------------------------------------

_MILESTONE = {"type": "string", "description": "Milestone id, e.g. m3-analysis."}

TOOL_DEFS = [
    {"name": "list_tables", "risk": "read", "function": list_tables,
     "description": "List every table loaded from inputs/ (CSV files and SQLite databases) with its "
                    "source file, columns, inferred types and row count. The reference totals file "
                    "is listed under not_loaded: it is not queryable.",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "profile_tables", "risk": "write", "function": profile_tables,
     "description": "Compute a data-quality profile (row count, duplicate rows, per-column nulls, "
                    "distinct values, min/max, most common values) and merge it into "
                    "deliverables/<milestone>/profile.json. Masked columns (mask_columns, and any "
                    "column holding e-mail addresses) keep their counts but show no values.",
     "input_schema": {"type": "object", "properties": {
         "tables": {"type": "array", "items": {"type": "string"},
                    "description": "Tables to profile; omit for all."},
         "milestone": _MILESTONE,
         "mask_columns": {"type": "array", "items": {"type": "string"},
                          "description": "Personal-data columns (the intake's sensitive_columns) as "
                                         "column or table.column; no values from them are written."}},
         "additionalProperties": False}},
    {"name": "run_query", "risk": "read", "function": run_query,
     "description": "Run one read-only SQLite SELECT/WITH query over the inputs and return the "
                    "columns, up to `limit` rows, the input tables it read and a hash of the full "
                    f"result. Writes are refused; a query is stopped after {QUERY_TIMEOUT_SECONDS:g} "
                    f"seconds or {MAX_RESULT_ROWS:,} rows.",
     "input_schema": {"type": "object", "properties": {
         "sql": {"type": "string"},
         "limit": {"type": "integer", "minimum": 1, "maximum": MAX_PREVIEW_ROWS}},
         "required": ["sql"], "additionalProperties": False}},
    {"name": "save_query", "risk": "write", "function": save_query,
     "description": "Execute a read-only query and save it as deliverables/<milestone>/queries/<name>.sql "
                    "with its full result in results/<name>.csv, so a reviewer can re-run it. The query "
                    "must read at least one input table.",
     "input_schema": {"type": "object", "properties": {
         "name": {"type": "string", "description": "lowercase name, e.g. revenue_by_month"},
         "sql": {"type": "string"},
         "milestone": _MILESTONE,
         "description": {"type": "string", "description": "What the query answers; saved as a comment."}},
         "required": ["name", "sql", "milestone"], "additionalProperties": False}},
    {"name": "record_figure", "risk": "write", "function": record_figure,
     "description": "Re-run a saved query, take the value at (row, column) and record it in "
                    "deliverables/<milestone>/figures.json. Returns the exact display text; the report "
                    "must show it immediately followed by the marker [F:<figure_id>].",
     "input_schema": {"type": "object", "properties": {
         "milestone": _MILESTONE,
         "figure_id": {"type": "string"},
         "query": {"type": "string", "description": "Name of a query saved with save_query."},
         "column": {"type": "string"},
         "row": {"type": "integer", "minimum": 0},
         "label": {"type": "string"},
         "unit": {"type": "string", "enum": list(FIGURE_UNITS),
                  "description": "usd: $1,234.00; pct: value already in percent (24.0 -> 24.0%); "
                                 "ratio: a fraction shown as percent (0.24 -> 24.0%); count: 1,234; "
                                 "empty: plain number."},
         "decimals": {"type": "integer", "minimum": 0, "maximum": 6}},
         "required": ["milestone", "figure_id", "query", "column"], "additionalProperties": False}},
    {"name": "reconcile_metrics", "risk": "write", "function": reconcile_metrics,
     "description": "Execute every metric in deliverables/<milestone>/metrics.yaml and compare it with "
                    "the customer's reference totals (inputs/reference_totals.csv when supplied); "
                    "writes reconciliation.csv.",
     "input_schema": {"type": "object", "properties": {
         "milestone": _MILESTONE,
         "reference_path": {"type": "string",
                            "description": "Workspace path of the reference CSV; omit for the default."},
         "tolerance_pct": {"type": "number", "minimum": 0}},
         "additionalProperties": False}},
]
