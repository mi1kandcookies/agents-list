"""
tests/specialists/test_data_analyst_domain.py - offline tests for the
data-analyst domain pack: tools, acceptance checks (including forged data
that must fail) and the agent.yaml manifest.
"""
from __future__ import annotations

import csv
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from agentkit.errors import ToolError
from specialists.data_analyst import tools

PKG = Path(__file__).resolve().parents[2] / "specialists" / "data_analyst"
FIXTURE = PKG / "evals" / "fixtures" / "tamarind-loop"

AUG_REVENUE_SQL = (
    "SELECT ROUND(SUM(amount_usd), 2) AS revenue FROM (SELECT DISTINCT * FROM invoices) i "
    "JOIN customers c USING (customer_id) "
    "WHERE c.is_test = 0 AND i.status = 'paid' AND i.invoice_date LIKE '2026-08%'"
)


@pytest.fixture()
def ws(tmp_path: Path) -> Path:
    shutil.copytree(FIXTURE / "inputs", tmp_path / "inputs")
    return tmp_path


# --- loading and the read-only guard -------------------------------------------

def test_list_tables_infers_types_and_keeps_text_ids(ws):
    out = tools.list_tables(ws)
    by_name = {t["table"]: t for t in out["tables"]}
    assert {"customers", "invoices", "reference_totals"} <= set(by_name)
    inv = {c["name"]: c["type"] for c in by_name["invoices"]["columns"]}
    assert inv["amount_usd"] == "REAL" and inv["invoice_id"] == "TEXT"
    assert by_name["invoices"]["source"] == "inputs/invoices.csv"


def test_leading_zero_ids_stay_text(tmp_path):
    (tmp_path / "inputs").mkdir()
    (tmp_path / "inputs" / "zips.csv").write_text("zip,n\n02139,1\n10001,2\n", encoding="utf-8")
    out = tools.run_query(tmp_path, sql="SELECT zip FROM zips ORDER BY zip")
    assert out["rows"] == [["02139"], ["10001"]]


def test_sqlite_inputs_are_loaded(tmp_path):
    (tmp_path / "inputs").mkdir()
    db = tmp_path / "inputs" / "shop.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE orders (id INTEGER, total REAL)")
    con.executemany("INSERT INTO orders VALUES (?, ?)", [(1, 10.5), (2, 4.5)])
    con.commit()
    con.close()
    assert tools.run_query(tmp_path, sql="SELECT SUM(total) AS t FROM orders")["rows"] == [[15.0]]


@pytest.mark.parametrize("sql", [
    "DELETE FROM invoices",
    "INSERT INTO invoices VALUES (1,2,3,4,5)",
    "DROP TABLE customers",
    "SELECT 1; DROP TABLE customers",
    "PRAGMA query_only = OFF",
    "ATTACH DATABASE 'x.db' AS x",
    "UPDATE invoices SET status = 'paid'",
    "WITH x AS (SELECT 1) DELETE FROM invoices",
    "REPLACE INTO invoices VALUES (1,2,3,4,5)",
])
def test_write_sql_is_refused(ws, sql):
    with pytest.raises(ToolError):
        tools.run_query(ws, sql=sql)


def test_keywords_inside_strings_and_replace_function_are_fine(ws):
    out = tools.run_query(ws, sql="SELECT REPLACE('drop table', 'drop', 'keep') AS s, 'delete' AS w")
    assert out["rows"] == [["keep table", "delete"]]


def test_authorizer_blocks_writes_even_past_the_text_guard(ws):
    session = tools.open_session(ws)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            session.conn.execute("DELETE FROM invoices")
        with pytest.raises(sqlite3.DatabaseError):
            session.conn.execute("CREATE TABLE t (a)")
    finally:
        session.close()


def test_sql_error_is_a_tool_error(ws):
    with pytest.raises(ToolError, match="SQL error"):
        tools.run_query(ws, sql="SELECT nope FROM invoices")


# --- profile, save, figures, reconciliation --------------------------------------

def test_profile_tables_reports_duplicates_and_nulls(ws):
    out = tools.profile_tables(ws, milestone="m1-profile")
    assert out["path"] == "deliverables/m1-profile/profile.json"
    doc = json.loads((ws / out["path"]).read_text(encoding="utf-8"))
    inv = doc["tables"]["invoices"]
    assert inv["duplicate_rows"] == 1
    assert inv["columns"][0]["duplicate_keys"] == 1
    region = next(c for c in doc["tables"]["customers"]["columns"] if c["name"] == "region")
    assert region["nulls"] == 1
    assert set(doc["inputs"]) >= {"inputs/customers.csv", "inputs/invoices.csv"}


def test_profile_unknown_table_fails(ws):
    with pytest.raises(ToolError, match="unknown table"):
        tools.profile_tables(ws, tables=["nope"])


def test_save_query_writes_sql_and_result(ws):
    out = tools.save_query(ws, name="aug_revenue", sql=AUG_REVENUE_SQL + ";",
                           milestone="m3-analysis", description="Paid revenue, Aug 2026")
    sql_text = (ws / out["query_path"]).read_text(encoding="utf-8")
    assert sql_text.startswith("-- Paid revenue, Aug 2026\n")
    rows = list(csv.reader((ws / out["result_path"]).open(encoding="utf-8")))
    assert rows == [["revenue"], ["4436.0"]]


@pytest.mark.parametrize("name", ["../escape", "Bad Name", "", "a/b"])
def test_save_query_rejects_unsafe_names(ws, name):
    with pytest.raises(ToolError):
        tools.save_query(ws, name=name, sql="SELECT 1", milestone="m3-analysis")


def test_save_query_rejects_unsafe_milestone(ws):
    with pytest.raises(ToolError):
        tools.save_query(ws, name="q", sql="SELECT 1", milestone="../../etc")


def test_record_figure_reads_the_saved_query(ws):
    tools.save_query(ws, name="aug_revenue", sql=AUG_REVENUE_SQL, milestone="m3-analysis")
    out = tools.record_figure(ws, milestone="m3-analysis", figure_id="f1", query="aug_revenue",
                              column="revenue", unit="usd", label="Aug revenue")
    assert out["figure"]["display"] == "$4,436.00"
    assert out["cite_as"] == "$4,436.00 [F:f1]"
    figs = tools.load_figures(ws / "deliverables/m3-analysis/figures.json")
    assert [f["id"] for f in figs] == ["f1"]
    # Re-recording the same id replaces it instead of duplicating.
    tools.record_figure(ws, milestone="m3-analysis", figure_id="f1", query="aug_revenue",
                        column="revenue", unit="usd", decimals=0)
    figs = tools.load_figures(ws / "deliverables/m3-analysis/figures.json")
    assert len(figs) == 1 and figs[0]["display"] == "$4,436"


def test_record_figure_errors(ws):
    with pytest.raises(ToolError, match="no saved query"):
        tools.record_figure(ws, milestone="m3-analysis", figure_id="f1", query="missing", column="x")
    tools.save_query(ws, name="names", sql="SELECT company_name FROM customers", milestone="m3-analysis")
    with pytest.raises(ToolError, match="numeric"):
        tools.record_figure(ws, milestone="m3-analysis", figure_id="f2", query="names",
                            column="company_name")
    with pytest.raises(ToolError, match="out of range"):
        tools.record_figure(ws, milestone="m3-analysis", figure_id="f3", query="names",
                            column="company_name", row=999)
    with pytest.raises(ToolError, match="unit"):
        tools.record_figure(ws, milestone="m3-analysis", figure_id="f4", query="names",
                            column="company_name", unit="eur")


@pytest.mark.parametrize("value, unit, decimals, expected", [
    (4436.0, "usd", None, "$4,436.00"),
    (-12.5, "usd", 1, "-$12.5"),
    (4.237, "pct", None, "4.2%"),
    (1204.4, "count", None, "1,204"),
    (1234567, "", None, "1,234,567"),
    (3.14159, "", 3, "3.142"),
])
def test_format_value(value, unit, decimals, expected):
    assert tools.format_value(value, unit, decimals) == expected


def write_metrics(ws: Path, metrics: list[dict], queries: dict[str, str]) -> None:
    base = ws / "deliverables" / "m2-metrics"
    (base / "queries").mkdir(parents=True, exist_ok=True)
    for name, sql in queries.items():
        (base / "queries" / f"{name}.sql").write_text(sql, encoding="utf-8")
    import yaml
    (base / "metrics.yaml").write_text(yaml.safe_dump({"metrics": metrics}), encoding="utf-8")


def good_metrics(ws: Path) -> None:
    metrics, queries = [], {}
    for month in ("07", "08"):
        rev = f"paid_revenue_2026_{month}"
        cust = f"paying_customers_2026_{month}"
        base_where = ("FROM (SELECT DISTINCT * FROM invoices) i JOIN customers c USING (customer_id) "
                      f"WHERE c.is_test = 0 AND i.status = 'paid' AND i.invoice_date LIKE '2026-{month}%'")
        queries[rev] = f"SELECT ROUND(SUM(amount_usd), 2) AS value {base_where};\n"
        queries[cust] = f"SELECT COUNT(DISTINCT customer_id) AS value {base_where};\n"
        for name, desc in ((rev, "Paid revenue"), (cust, "Paying customers")):
            metrics.append({"name": name, "description": desc, "grain": "month",
                            "query": f"queries/{name}.sql", "value_column": "value",
                            "owner": "Finance lead", "filters": ["is_test = 0", "status = paid"]})
    write_metrics(ws, metrics, queries)


def test_reconcile_metrics_matches_reference(ws):
    good_metrics(ws)
    out = tools.reconcile_metrics(ws)
    assert out["all_match"], out["rows"]
    text = (ws / "deliverables/m2-metrics/reconciliation.csv").read_text(encoding="utf-8")
    assert text.splitlines()[0] == ",".join(tools.RECONCILIATION_COLUMNS)


def test_reconcile_metrics_flags_naive_sum(ws):
    good_metrics(ws)
    naive = ("SELECT ROUND(SUM(amount_usd), 2) AS value FROM invoices "
             "WHERE status = 'paid' AND invoice_date LIKE '2026-08%';")
    (ws / "deliverables/m2-metrics/queries/paid_revenue_2026_08.sql").write_text(naive, encoding="utf-8")
    out = tools.reconcile_metrics(ws)
    assert not out["all_match"]
    row = next(r for r in out["rows"] if r["metric"] == "paid_revenue_2026_08")
    assert row["status"] == "mismatch"


def test_reconcile_metrics_zero_tolerance_is_respected(ws):
    good_metrics(ws)
    off_by_one = ("SELECT COUNT(DISTINCT customer_id) + 1 AS value FROM invoices "
                  "WHERE invoice_date LIKE '2026-07%' AND status = 'paid' AND customer_id IN "
                  "(SELECT customer_id FROM customers WHERE is_test = 0);")
    (ws / "deliverables/m2-metrics/queries/paying_customers_2026_07.sql").write_text(off_by_one, encoding="utf-8")
    rows = {r["metric"]: r for r in tools.reconcile_metrics(ws)["rows"]}
    assert rows["paying_customers_2026_07"]["status"] == "mismatch"
    assert rows["paying_customers_2026_07"]["tolerance_pct"] == 0.0


def test_reconcile_metrics_reports_missing_metric(ws):
    good_metrics(ws)
    import yaml
    path = ws / "deliverables/m2-metrics/metrics.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["metrics"] = doc["metrics"][:1]
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    statuses = {r["metric"]: r["status"] for r in tools.reconcile_metrics(ws)["rows"]}
    assert statuses["paid_revenue_2026_08"] == "missing_metric"


def test_metric_query_path_cannot_escape(ws):
    good_metrics(ws)
    import yaml
    path = ws / "deliverables/m2-metrics/metrics.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["metrics"][0]["query"] = "../../../../outside.sql"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    rows = tools.reconcile_metrics(ws)["rows"]
    assert "escapes the workspace" in rows[0]["status"]


def test_tool_defs_are_well_formed():
    names = [d["name"] for d in tools.TOOL_DEFS]
    assert len(names) == len(set(names))
    for d in tools.TOOL_DEFS:
        assert set(d) == {"name", "description", "input_schema", "risk", "function"}
        assert d["risk"] in ("read", "write", "exec", "network", "external")
        assert d["input_schema"]["type"] == "object" and callable(d["function"])
