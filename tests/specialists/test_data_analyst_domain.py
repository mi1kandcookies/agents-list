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
import yaml

from agentkit.errors import ToolError
from specialists.data_analyst import checks, tools

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
    assert set(by_name) == {"customers", "invoices"}
    inv = {c["name"]: c["type"] for c in by_name["invoices"]["columns"]}
    assert inv["amount_usd"] == "REAL" and inv["invoice_id"] == "TEXT"
    assert by_name["invoices"]["source"] == "inputs/invoices.csv"
    # The reference totals are the answer key for M2: listed, never queryable.
    assert [n["source"] for n in out["not_loaded"]] == [tools.REFERENCE_FILE]
    with pytest.raises(ToolError, match="no such table"):
        tools.run_query(ws, sql="SELECT value FROM reference_totals")


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
    path = ws / "deliverables/m2-metrics/metrics.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["metrics"] = doc["metrics"][:1]
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    statuses = {r["metric"]: r["status"] for r in tools.reconcile_metrics(ws)["rows"]}
    assert statuses["paid_revenue_2026_08"] == "missing_metric"


@pytest.mark.parametrize("query", ["../../../../outside.sql", "scratch/paid_revenue_2026_07.sql",
                                   "queries/../scratch/q.sql", "queries/Paid.sql"])
def test_metric_query_must_be_a_saved_query(ws, query):
    """A metric's SQL must live in queries/, where it is re-run and hashed."""
    good_metrics(ws)
    scratch = ws / "deliverables/m2-metrics/scratch"
    scratch.mkdir()
    (scratch / "paid_revenue_2026_07.sql").write_text(
        "SELECT ROUND(SUM(amount_usd), 2) AS value FROM invoices;", encoding="utf-8")
    path = ws / "deliverables/m2-metrics/metrics.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["metrics"][0]["query"] = query
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    rows = tools.reconcile_metrics(ws)["rows"]
    assert rows[0]["status"].startswith("error: ") and "queries/<name>.sql" in rows[0]["status"]
    res = checks.metrics_valid(ws, {})
    assert res["passed"] is False and "queries/<name>.sql" in res["details"]


@pytest.mark.parametrize("rel", ["../outside.csv", ".agentkit/ledger.json", "//host/share/x.csv",
                                 "/etc/passwd", "inputs/../../outside.csv"])
def test_resolve_in_refuses_escapes_and_kit_files(ws, rel):
    with pytest.raises(ToolError):
        tools.resolve_in(ws, rel)


def test_reconcile_reference_must_stay_in_the_workspace(ws):
    good_metrics(ws)
    (ws / ".agentkit").mkdir()
    shutil.copy(ws / "inputs/reference_totals.csv", ws / ".agentkit/reference_totals.csv")
    for rel in ("../reference_totals.csv", ".agentkit/reference_totals.csv"):
        with pytest.raises(ToolError):
            tools.reconcile_metrics(ws, reference_path=rel)


def test_tools_resolve_paths_through_the_kit_when_given(ws):
    seen = []

    def resolve_path(p, *, write=False):
        seen.append((p, write))
        return tools.resolve_in(ws, p)

    tools.save_query(ws, resolve_path=resolve_path, name="aug_revenue", sql=AUG_REVENUE_SQL,
                     milestone="m3-analysis")
    tools.record_figure(ws, resolve_path=resolve_path, milestone="m3-analysis", figure_id="f1",
                        query="aug_revenue", column="revenue", unit="usd")
    good_metrics(ws)
    tools.reconcile_metrics(ws, resolve_path=resolve_path)
    assert seen[:3] == [("deliverables/m3-analysis/queries/aug_revenue.sql", True),
                        ("deliverables/m3-analysis/results/aug_revenue.csv", True),
                        ("deliverables/m3-analysis/figures.json", True)]
    assert ("deliverables/m2-metrics/reconciliation.csv", True) in seen
    assert ("inputs/reference_totals.csv", False) in seen                     # model-chosen read
    assert ("deliverables/m2-metrics/queries/paid_revenue_2026_07.sql", False) in seen


def test_tool_defs_are_well_formed():
    names = [d["name"] for d in tools.TOOL_DEFS]
    assert len(names) == len(set(names))
    for d in tools.TOOL_DEFS:
        assert set(d) == {"name", "description", "input_schema", "risk", "function"}
        assert d["risk"] in ("read", "write", "exec", "network", "external")
        assert d["input_schema"]["type"] == "object" and callable(d["function"])


# --- checks ------------------------------------------------------------------------

def test_profile_check_passes_on_tool_output(ws):
    tools.profile_tables(ws)
    res = checks.profile_matches_source(ws, {})
    assert res["passed"] is True and res["score"] == 1.0, res


def test_profile_check_fails_on_forged_counts(ws):
    tools.profile_tables(ws)
    path = ws / "deliverables/m1-profile/profile.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["tables"]["invoices"]["row_count"] -= 1           # hide the duplicate row
    doc["tables"]["invoices"]["duplicate_rows"] = 0
    path.write_text(json.dumps(doc), encoding="utf-8")
    res = checks.profile_matches_source(ws, {})
    assert res["passed"] is False and "invoices" in res["details"]


def test_profile_check_fails_when_a_table_is_missing(ws):
    tools.profile_tables(ws, tables=["customers"])
    res = checks.profile_matches_source(ws, {})
    assert res["passed"] is False and "not profiled" in res["details"]
    assert checks.profile_matches_source(ws, {"tables": ["customers"]})["passed"] is True


def test_profile_check_fails_on_forged_null_count(ws):
    tools.profile_tables(ws)
    path = ws / "deliverables/m1-profile/profile.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    region = next(c for c in doc["tables"]["customers"]["columns"] if c["name"] == "region")
    region["nulls"] = 0
    path.write_text(json.dumps(doc), encoding="utf-8")
    res = checks.profile_matches_source(ws, {})
    assert res["passed"] is False and "region.nulls" in res["details"]


def test_profile_check_without_file_fails(ws):
    assert checks.profile_matches_source(ws, {})["passed"] is False


def test_queries_reexecute_pass_and_tamper(ws):
    tools.save_query(ws, name="aug_revenue", sql=AUG_REVENUE_SQL, milestone="m3-analysis")
    assert checks.queries_reexecute(ws, {"milestone": "m3-analysis"})["passed"] is True
    result = ws / "deliverables/m3-analysis/results/aug_revenue.csv"
    result.write_text("revenue\n5458.0\n", encoding="utf-8")
    res = checks.queries_reexecute(ws, {"milestone": "m3-analysis"})
    assert res["passed"] is False and "differs" in res["details"]


def test_queries_reexecute_rejects_write_sql_and_missing_results(ws):
    qdir = ws / "deliverables/m3-analysis/queries"
    qdir.mkdir(parents=True)
    (qdir / "evil.sql").write_text("DELETE FROM invoices;", encoding="utf-8")
    (qdir / "orphan.sql").write_text("SELECT 1 AS one;", encoding="utf-8")
    res = checks.queries_reexecute(ws, {"milestone": "m3-analysis"})
    assert res["passed"] is False
    assert "evil.sql" in res["details"] and "orphan.sql" in res["details"]


def test_queries_reexecute_needs_minimum(ws):
    res = checks.queries_reexecute(ws, {"milestone": "m3-analysis", "min_queries": 2})
    assert res["passed"] is False


def _analysis(ws: Path, line: str) -> None:
    tools.save_query(ws, name="aug_revenue", sql=AUG_REVENUE_SQL, milestone="m3-analysis")
    tools.record_figure(ws, milestone="m3-analysis", figure_id="aug-rev", query="aug_revenue",
                        column="revenue", unit="usd")
    (ws / "deliverables/m3-analysis/report.md").write_text(
        "# Report\n\n## Findings\n\n" + line + "\n", encoding="utf-8")


def test_figures_check_passes_when_report_matches(ws):
    _analysis(ws, "August paid revenue was $4,436.00 [F:aug-rev].")
    res = checks.figures_match_queries(ws, {"milestone": "m3-analysis"})
    assert res["passed"] is True, res


def test_figures_check_fails_when_report_misstates_number(ws):
    _analysis(ws, "August paid revenue was $5,458.00 [F:aug-rev].")
    res = checks.figures_match_queries(ws, {"milestone": "m3-analysis"})
    assert res["passed"] is False and "[F:aug-rev] must directly follow $4,436.00" in res["details"]


def test_figures_check_fails_on_forged_figure_value(ws):
    _analysis(ws, "August paid revenue was $5,458.00 [F:aug-rev].")
    path = ws / "deliverables/m3-analysis/figures.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["figures"][0].update(value=5458.0, display="$5,458.00")
    path.write_text(json.dumps(doc), encoding="utf-8")
    res = checks.figures_match_queries(ws, {"milestone": "m3-analysis"})
    assert res["passed"] is False and "query returns 4436.0" in res["details"]


def test_figures_check_fails_on_unknown_or_uncited_marker(ws):
    _analysis(ws, "Revenue grew 12% [F:made-up].")
    res = checks.figures_match_queries(ws, {"milestone": "m3-analysis"})
    assert res["passed"] is False
    assert "made-up" in res["details"] and "aug-rev: not cited" in res["details"]


def test_figures_check_fails_when_query_changes_underneath(ws):
    _analysis(ws, "August paid revenue was $4,436.00 [F:aug-rev].")
    (ws / "deliverables/m3-analysis/queries/aug_revenue.sql").write_text(
        "SELECT 1.0 AS revenue;", encoding="utf-8")
    assert checks.figures_match_queries(ws, {"milestone": "m3-analysis"})["passed"] is False


def test_figures_check_missing_files(ws):
    assert checks.figures_match_queries(ws, {"milestone": "m3-analysis"})["passed"] is False


def test_metrics_checks_pass_on_good_definitions(ws):
    good_metrics(ws)
    tools.reconcile_metrics(ws)
    assert checks.metrics_valid(ws, {"min_metrics": 4})["passed"] is True
    res = checks.metrics_reconcile(ws, {})
    assert res["passed"] is True and res["score"] == 1.0, res


def test_metrics_valid_flags_incomplete_and_broken(ws):
    good_metrics(ws)
    path = ws / "deliverables/m2-metrics/metrics.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["metrics"][0]["owner"] = ""
    doc["metrics"][1]["value_column"] = "nope"
    doc["metrics"].append(dict(doc["metrics"][2]))
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    res = checks.metrics_valid(ws, {})
    assert res["passed"] is False
    assert "missing owner" in res["details"] and "nope" in res["details"] and "twice" in res["details"]


def test_metrics_valid_bad_yaml(ws):
    base = ws / "deliverables/m2-metrics"
    base.mkdir(parents=True)
    (base / "metrics.yaml").write_text("metrics: [unclosed", encoding="utf-8")
    assert checks.metrics_valid(ws, {})["passed"] is False


NAIVE_AUG = ("SELECT SUM(amount_usd) AS value FROM invoices WHERE status = 'paid' "
             "AND invoice_date LIKE '2026-08%';")


def test_metrics_reconcile_fails_on_naive_definition(ws):
    good_metrics(ws)
    (ws / "deliverables/m2-metrics/queries/paid_revenue_2026_08.sql").write_text(NAIVE_AUG, encoding="utf-8")
    tools.reconcile_metrics(ws)
    res = checks.metrics_reconcile(ws, {})
    assert res["passed"] is False and "paid_revenue_2026_08: mismatch" in res["details"]


def test_metrics_reconcile_fails_on_forged_reconciliation(ws):
    good_metrics(ws)
    (ws / "deliverables/m2-metrics/queries/paid_revenue_2026_08.sql").write_text(NAIVE_AUG, encoding="utf-8")
    tools.reconcile_metrics(ws)
    path = ws / "deliverables/m2-metrics/reconciliation.csv"
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    for r in rows:
        if r["metric"] == "paid_revenue_2026_08":
            r.update(computed="4436.0", diff_pct="0.0", status="match")
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    res = checks.metrics_reconcile(ws, {})
    assert res["passed"] is False and "reconciliation claims paid_revenue_2026_08" in res["details"]


def test_metrics_reconcile_requires_every_reference(ws):
    good_metrics(ws)
    path = ws / "deliverables/m2-metrics/metrics.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["metrics"] = doc["metrics"][:2]
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    tools.reconcile_metrics(ws)
    res = checks.metrics_reconcile(ws, {})
    assert res["passed"] is False and "no metric definition" in res["details"]
    relaxed = checks.metrics_reconcile(ws, {"require_all_references": False})
    assert relaxed["passed"] is True, relaxed


def test_metrics_reconcile_needs_reconciliation_file(ws):
    good_metrics(ws)
    res = checks.metrics_reconcile(ws, {})
    assert res["passed"] is False and "reconciliation.csv not found" in res["details"]
    assert checks.metrics_reconcile(ws, {"reconciliation": ""})["passed"] is True


# --- manifest -----------------------------------------------------------------------

KIT_TOOLS = {"read_document", "read_file", "write_file", "edit_file", "list_files", "search_files",
             "record_source", "record_claim", "ask_client", "post_progress", "submit_milestone",
             "run_command", "http_fetch", "web_search"}
KIT_CHECKS = {"file_exists", "files_exist", "markdown_sections", "no_placeholders", "word_count",
              "json_valid", "csv_columns", "command_succeeds", "ledger_verified", "citations_resolve",
              "disclaimer_present", "rubric_grader", "human_signoff"}


def _manifest() -> dict:
    return yaml.safe_load((PKG / "agent.yaml").read_text(encoding="utf-8"))


def test_manifest_parses_and_references_known_tools_and_checks():
    m = _manifest()
    assert m["schema_version"] == 1 and m["slug"] == "data-analyst" and m["profile"] == "data"
    domain_tools = {d["name"] for d in tools.TOOL_DEFS}
    assert set(m["tools"]) <= KIT_TOOLS | domain_tools
    assert domain_tools <= set(m["tools"])
    used_checks = {a["check"] for ms in m["milestones"] for a in ms["acceptance"]}
    assert used_checks <= KIT_CHECKS | set(checks.CHECK_DEFS)
    assert set(checks.CHECK_DEFS) <= used_checks


def test_manifest_milestones_and_files():
    m = _manifest()
    assert [ms["id"] for ms in m["milestones"]] == ["m1-profile", "m2-metrics", "m3-analysis"]
    for ms in m["milestones"]:
        assert ms["deliverables"]
        assert all(d.startswith(f"deliverables/{ms['id']}/") for d in ms["deliverables"])
        lo, hi = ms["hours"]
        assert 0 < lo <= hi
        for a in ms["acceptance"]:
            assert a.get("kind", "automated") in ("automated", "rubric", "human")
            if a["check"] == "rubric_grader":
                rubric = yaml.safe_load((PKG / a["params"]["rubric"]).read_text(encoding="utf-8"))
                assert {"name", "criteria", "threshold"} <= set(rubric)
                assert abs(sum(c["weight"] for c in rubric["criteria"]) - 1.0) < 1e-9
    for rel in [m["prompts"]["system"], *m["prompts"]["include"]]:
        assert (PKG / rel).is_file(), rel
    assert m["egress"]["mode"] == "none" and m["shell"]["allow"] == []
    assert m["human_gate"]["required"] is False
    assert m["listing"]["pricing"]["currency"] == "USDC"
    assert m["models"]["primary"] == "anthropic:claude-opus-5"
    assert any(i["required"] for i in m["intake"])


def test_eval_cases_reference_real_milestones():
    ids = {ms["id"] for ms in _manifest()["milestones"]}
    cases = sorted((PKG / "evals" / "cases").glob("*.json"))
    assert cases
    for path in cases:
        case = json.loads(path.read_text(encoding="utf-8"))
        assert {"name", "brief", "milestone", "notes"} <= set(case)
        assert case["milestone"] in ids


def test_check_defs_are_callable():
    assert set(checks.CHECK_DEFS) == {"profile_matches_source", "queries_reexecute",
                                      "figures_match_queries", "metrics_valid", "metrics_reconcile"}
    assert all(callable(fn) for fn in checks.CHECK_DEFS.values())


# --- numbers must be computed from the customer's data --------------------------------

def _write_query(ws: Path, milestone: str, name: str, sql: str) -> None:
    """Write a query and a matching result CSV by hand, bypassing save_query
    (the model can do this with write_file)."""
    base = ws / "deliverables" / milestone
    (base / "queries").mkdir(parents=True, exist_ok=True)
    (base / "results").mkdir(parents=True, exist_ok=True)
    (base / "queries" / f"{name}.sql").write_text(sql + ";\n", encoding="utf-8")
    session = tools.open_session(ws)
    try:
        result = tools.execute(session, sql)
    finally:
        session.close()
    (base / "results" / f"{name}.csv").write_text(tools.result_csv(result), encoding="utf-8")


def test_metrics_cannot_read_the_reference_totals(ws):
    """Copying the reference totals would reconcile trivially; they are not a table."""
    with pytest.raises(ToolError, match="no such table"):
        tools.save_query(ws, name="paid_revenue_2026_08", milestone="m2-metrics",
                         sql="SELECT value FROM reference_totals WHERE metric = 'paid_revenue_2026_08'")
    good_metrics(ws)
    (ws / "deliverables/m2-metrics/queries/paid_revenue_2026_08.sql").write_text(
        "SELECT value FROM reference_totals WHERE metric = 'paid_revenue_2026_08';", encoding="utf-8")
    tools.reconcile_metrics(ws)
    for res in (checks.metrics_valid(ws, {}), checks.metrics_reconcile(ws, {}),
                checks.queries_reexecute(ws, {"milestone": "m2-metrics"})):
        assert res["passed"] is False and "no such table: reference_totals" in res["details"], res


def test_a_reference_in_another_place_is_also_hidden_from_metrics(ws):
    good_metrics(ws)
    (ws / "inputs/finance").mkdir()
    shutil.move(ws / "inputs/reference_totals.csv", ws / "inputs/finance/ref.csv")
    (ws / "deliverables/m2-metrics/queries/paid_revenue_2026_08.sql").write_text(
        "SELECT value FROM ref WHERE metric = 'paid_revenue_2026_08';", encoding="utf-8")
    out = tools.reconcile_metrics(ws, reference_path="inputs/finance/ref.csv")
    rows = {r["metric"]: r for r in out["rows"]}
    assert "no such table: ref" in rows["paid_revenue_2026_08"]["status"]
    assert rows["paid_revenue_2026_07"]["status"] == "match"


@pytest.mark.parametrize("sql", ["SELECT 4500.0 AS revenue, 24.0 AS pct, 19 AS n", "VALUES (1)",
                                 "WITH x AS (SELECT 4436.0 AS value) SELECT value FROM x"])
def test_save_query_refuses_sql_that_reads_no_input_table(ws, sql):
    with pytest.raises(ToolError, match="reads none of the input tables"):
        tools.save_query(ws, name="made_up", sql=sql, milestone="m3-analysis")
    assert not (ws / "deliverables/m3-analysis/queries/made_up.sql").exists()


def test_execute_reports_the_input_tables_read(ws):
    session = tools.open_session(ws)
    try:
        assert tools.execute(session, AUG_REVENUE_SQL).tables == ["customers", "invoices"]
        assert tools.execute(session, "SELECT COUNT(*) AS n FROM invoices").tables == ["invoices"]
        assert tools.execute(session, "SELECT COUNT(*) AS n FROM INVOICES").tables == ["invoices"]
        assert tools.execute(session, "SELECT name FROM sqlite_master").tables == []
        # A CTE named like a table is not the table.
        shadow = "WITH invoices AS (SELECT 4436.0 AS value) SELECT value FROM invoices"
        assert tools.execute(session, shadow).tables == []
        # The same SQL twice on one session still reports its tables.
        assert tools.execute(session, AUG_REVENUE_SQL).tables == ["customers", "invoices"]
    finally:
        session.close()


def test_hand_written_constant_queries_fail_every_check(ws):
    _write_query(ws, "m3-analysis", "made_up", "SELECT 4500.0 AS revenue, 24.0 AS pct, 19 AS n")
    res = checks.queries_reexecute(ws, {"milestone": "m3-analysis"})
    assert res["passed"] is False and "made_up.sql" in res["details"] and "reads none" in res["details"]
    figures = [{"id": "rev", "query": "made_up", "column": "revenue", "row": 0, "value": 4500.0,
                "unit": "usd", "decimals": None, "display": "$4,500.00"}]
    (ws / "deliverables/m3-analysis/figures.json").write_text(json.dumps({"figures": figures}), encoding="utf-8")
    (ws / "deliverables/m3-analysis/report.md").write_text("Revenue was $4,500.00 [F:rev].\n", encoding="utf-8")
    res = checks.figures_match_queries(ws, {"milestone": "m3-analysis"})
    assert res["passed"] is False and "reads none of the input tables" in res["details"]


@pytest.mark.parametrize("sql", ["SELECT 4436.0 AS revenue FROM invoices LIMIT 1",
                                 "SELECT -4436.0 AS revenue FROM invoices LIMIT 1",
                                 "SELECT 4436 + 0 * COUNT(*) AS revenue FROM invoices"])
def test_a_value_typed_into_the_sql_is_refused(ws, sql):
    tools.save_query(ws, name="typed", sql=sql, milestone="m3-analysis")     # it does read a table
    with pytest.raises(ToolError, match="written into the SQL as a literal"):
        tools.record_figure(ws, milestone="m3-analysis", figure_id="rev", query="typed", column="revenue")
    good_metrics(ws)
    (ws / "deliverables/m2-metrics/queries/paid_revenue_2026_08.sql").write_text(
        sql.replace("revenue", "value"), encoding="utf-8")
    res = checks.metrics_valid(ws, {})
    assert res["passed"] is False and "paid_revenue_2026_08" in res["details"] and "literal" in res["details"]


def test_small_literals_that_match_a_result_are_not_flagged(ws):
    """substr(..., 1, 7) or ROUND(..., 2) can equal a real count; only large literals count."""
    sql = ("SELECT substr(invoice_date, 1, 7) AS month, COUNT(*) AS n FROM invoices "
           "WHERE invoice_date LIKE '2026-06%' GROUP BY month HAVING COUNT(*) > 2")
    tools.save_query(ws, name="june", sql=sql, milestone="m3-analysis")
    out = tools.record_figure(ws, milestone="m3-analysis", figure_id="n", query="june", column="n", unit="count")
    assert out["figure"]["value"] > 2
    assert tools.typed_literals(sql) == set()
    assert tools.typed_literals("SELECT 4436.0 AS v, '9999' AS s /* 5000 */ FROM t -- 7000") == {4436.0}


def test_a_metric_cannot_type_in_even_a_small_reference_value(ws):
    """Below the literal floor, a metric that writes its own reference total
    into the SQL is still refused when reconciling."""
    good_metrics(ws)
    queries = ws / "deliverables/m2-metrics/queries"
    (queries / "paying_customers_2026_08.sql").write_text(
        "SELECT 19 AS value FROM invoices LIMIT 1;", encoding="utf-8")
    # A computed count is refused too once its reference value (11) is written
    # anywhere in the SQL; without it, substr(..., 1, 7) is fine.
    (queries / "paying_customers_2026_07.sql").write_text(
        "SELECT COUNT(DISTINCT i.customer_id) AS value FROM (SELECT DISTINCT * FROM invoices) i "
        "JOIN customers c USING (customer_id) WHERE c.is_test = 0 AND i.status = 'paid' "
        "AND substr(i.invoice_date, 1, 7) = '2026-07' AND 11 > 1;", encoding="utf-8")
    rows = {r["metric"]: r for r in tools.reconcile_metrics(ws)["rows"]}
    assert rows["paying_customers_2026_08"]["status"].endswith("is written into the SQL as a literal; "
                                                               "compute it from the data instead")
    assert rows["paying_customers_2026_07"]["status"].startswith("error: ")
    (queries / "paying_customers_2026_07.sql").write_text(
        "SELECT COUNT(DISTINCT i.customer_id) AS value FROM (SELECT DISTINCT * FROM invoices) i "
        "JOIN customers c USING (customer_id) WHERE c.is_test = 0 AND i.status = 'paid' "
        "AND substr(i.invoice_date, 1, 7) = '2026-07';", encoding="utf-8")
    rows = {r["metric"]: r for r in tools.reconcile_metrics(ws)["rows"]}
    assert rows["paying_customers_2026_07"]["status"] == "match"
    res = checks.metrics_reconcile(ws, {})
    assert res["passed"] is False and "paying_customers_2026_08: error" in res["details"]


# --- the report: every number cited, every citation exact -----------------------------

def _counts(ws: Path) -> None:
    tools.save_query(ws, name="aug_customers", milestone="m3-analysis",
                     sql="SELECT COUNT(DISTINCT customer_id) AS customers FROM invoices "
                         "WHERE status = 'paid' AND invoice_date LIKE '2026-08%'")
    tools.record_figure(ws, milestone="m3-analysis", figure_id="cust", query="aug_customers",
                        column="customers", unit="count")


@pytest.mark.parametrize("line", [
    "August paid revenue was -$4,436.00 [F:aug-rev].",                         # sign flipped
    "August paid revenue was $14,436.00 [F:aug-rev].",                         # digit prefix
    "August paid revenue was $4,436.00, see [F:aug-rev].",                     # marker off the number
    "August paid revenue was $4,436.00 [F:aug-rev], later $5.00 [F:aug-rev].",  # one of two wrong
])
def test_figures_check_requires_the_exact_display_before_each_marker(ws, line):
    _analysis(ws, line)
    res = checks.figures_match_queries(ws, {"milestone": "m3-analysis"})
    assert res["passed"] is False and "[F:aug-rev] must directly follow $4,436.00" in res["details"], res


def test_figures_check_catches_a_count_prefix_and_swapped_markers(ws):
    _analysis(ws, "placeholder")
    _counts(ws)
    count = tools.load_figures(ws / "deliverables/m3-analysis/figures.json")[-1]["display"]
    report = ws / "deliverables/m3-analysis/report.md"
    report.write_text(f"Revenue was $4,436.00 [F:aug-rev] from {count} [F:cust] customers.\n", encoding="utf-8")
    assert checks.figures_match_queries(ws, {"milestone": "m3-analysis"})["passed"] is True
    report.write_text(f"Revenue was $4,436.00 [F:aug-rev] from 1{count} [F:cust] customers.\n", encoding="utf-8")
    res = checks.figures_match_queries(ws, {"milestone": "m3-analysis"})
    assert res["passed"] is False and "[F:cust] must directly follow" in res["details"]
    report.write_text(f"Revenue was $4,436.00 [F:cust] from {count} [F:aug-rev] customers.\n", encoding="utf-8")
    res = checks.figures_match_queries(ws, {"milestone": "m3-analysis"})
    assert res["passed"] is False and "[F:aug-rev] must" in res["details"] and "[F:cust] must" in res["details"]


def test_figures_check_accepts_emphasis_around_the_display(ws):
    _analysis(ws, "August paid revenue was **$4,436.00** [F:aug-rev].")
    assert checks.figures_match_queries(ws, {"milestone": "m3-analysis"})["passed"] is True


def test_numbers_without_a_marker_fail(ws):
    _analysis(ws, "August paid revenue was $4,436.00 [F:aug-rev]. Churn was 37.5% and ARR is $1,200,000.")
    res = checks.figures_match_queries(ws, {"milestone": "m3-analysis"})
    assert res["passed"] is False
    assert "2 number(s) in the report have no [F:id] marker: '37.5%', '$1,200,000'" in res["details"]


UNMARKED_REPORT = """# Revenue 2026: $1,000 up 5%

## Summary

Revenue was $4,436.00 [F:rev] across 19 customers in 2026-08, 3 of 62 rows affected.

## Method

Reconciled within 0.5% of the $4,436.00 reference.

### Queries

Metric tolerance 1,000 rows.

## Findings

```sql
SELECT 1,000 AS x, '12%' AS y
```
Inline `LIMIT 1,000` is code. Growth was 12.5 % and **$9.10** here, US$95 [F:rev] there.
ARR is 1200000 and churn fell 3.5 points; invoice INV-00012 and v2.1 are not numbers.

## Findings and method

Revenue was US$1,200 last year.
"""


def test_unmarked_numbers_skip_method_headings_code_and_short_integers():
    assert checks.unmarked_numbers(UNMARKED_REPORT, ["Method"]) == [
        "12.5 %", "$9.10", "1200000", "3.5", "$1,200"]
    assert checks.unmarked_numbers(UNMARKED_REPORT, []) == [
        "0.5%", "$4,436.00", "1,000", "12.5 %", "$9.10", "1200000", "3.5", "$1,200"]


def test_ratio_unit_shows_a_fraction_as_percent():
    assert tools.format_value(0.24, "ratio") == "24.0%"
    assert tools.format_value(0.2437, "ratio", 2) == "24.37%"
    assert tools.format_value(24.0, "pct") == "24.0%"


# --- M2 without reference totals, and reference files as finance writes them -------------

def test_m2_without_reference_totals_can_pass(ws):
    (ws / "inputs/reference_totals.csv").unlink()
    good_metrics(ws)
    out = tools.reconcile_metrics(ws)
    assert {r["status"] for r in out["rows"]} == {"no_reference"} and "no reference totals" in out["note"]
    res = checks.metrics_reconcile(ws, {"reference": "inputs/reference_totals.csv"})
    assert res["passed"] is True and "nothing reconciled" in res["details"], res
    required = checks.metrics_reconcile(ws, {"require_reference": True})
    assert required["passed"] is False and "reference totals not found" in required["details"]
    # A hand-edited reconciliation is still caught without a reference.
    path = ws / "deliverables/m2-metrics/reconciliation.csv"
    path.write_text(path.read_text(encoding="utf-8").replace("no_reference", "match", 1), encoding="utf-8")
    assert checks.metrics_reconcile(ws, {})["passed"] is False


def test_a_missing_explicit_reference_is_a_clean_tool_error(ws):
    good_metrics(ws)
    with pytest.raises(ToolError) as info:
        tools.reconcile_metrics(ws, reference_path="inputs/finance.csv")
    assert str(info.value) == "reference totals not found: inputs/finance.csv"


def test_reference_values_may_be_formatted_and_bad_rows_fail_alone(ws):
    good_metrics(ws)
    (ws / "inputs/reference_totals.csv").write_text(
        "metric,value,tolerance_pct,note\n"
        'paid_revenue_2026_07,"$2,659.00",0.5,\n'
        'paid_revenue_2026_08,"4,436.00",,\n'
        "paying_customers_2026_07,eleven,0,\n"
        "paying_customers_2026_08,19,,\n", encoding="utf-8")
    rows = {r["metric"]: r for r in tools.reconcile_metrics(ws)["rows"]}
    assert rows["paid_revenue_2026_07"]["status"] == "match"
    assert rows["paid_revenue_2026_08"]["status"] == "match"
    assert rows["paying_customers_2026_07"]["status"] == "error: reference value 'eleven' is not a number"
    assert rows["paying_customers_2026_08"]["status"] == "match"
    res = checks.metrics_reconcile(ws, {})
    assert res["passed"] is False and "paying_customers_2026_07: error" in res["details"]
    assert "paid_revenue" not in res["details"]


def test_counts_tie_exactly_unless_the_reference_sets_a_tolerance(ws):
    good_metrics(ws)
    (ws / "inputs/reference_totals.csv").write_text(
        "metric,value,tolerance_pct,note\n"
        "paid_revenue_2026_08,4450.0,,\n"                  # 0.3% off: money, default 0.5%
        "paying_customers_2026_08,19.05,,\n"               # 0.26% off: a count must be exact
        "paying_customers_2026_07,11.05,1,\n", encoding="utf-8")   # an explicit tolerance wins
    rows = {r["metric"]: r for r in tools.reconcile_metrics(ws)["rows"]}
    assert rows["paid_revenue_2026_08"]["status"] == "match"
    assert rows["paying_customers_2026_08"]["tolerance_pct"] == 0.0
    assert rows["paying_customers_2026_08"]["status"] == "mismatch"
    assert rows["paying_customers_2026_07"]["status"] == "match"


def test_reconciliation_rows_must_be_complete_and_exact(ws):
    good_metrics(ws)
    tools.reconcile_metrics(ws)
    path = ws / "deliverables/m2-metrics/reconciliation.csv"
    original = path.read_text(encoding="utf-8")
    path.write_text("\n".join(original.splitlines()[:-1]) + "\n", encoding="utf-8")      # drop a row
    res = checks.metrics_reconcile(ws, {})
    assert res["passed"] is False and "reconciliation has no row for paying_customers_2026_08" in res["details"]
    assert ",4436.0,4436.0,0.0,0.5,match" in original
    path.write_text(original.replace(",4436.0,4436.0,", ",4436.0,4400.0,"), encoding="utf-8")   # misstate a reference
    res = checks.metrics_reconcile(ws, {})
    assert res["passed"] is False and "paid_revenue_2026_08 reference = '4400.0'" in res["details"], res


# --- loading: formatted numbers, huge ids, keywords, personal data, runaway queries ------

def test_money_with_symbols_and_separators_loads_as_numbers(tmp_path):
    (tmp_path / "inputs").mkdir()
    (tmp_path / "inputs" / "sales.csv").write_text(
        'id,amount,note\n1,"$1,200.00",a\n2,"2,300.00",b\n3,($100.50),c\n4,,d\n', encoding="utf-8")
    out = tools.list_tables(tmp_path)
    types = {c["name"]: c["type"] for c in out["tables"][0]["columns"]}
    assert types["amount"] == "REAL" and types["note"] == "TEXT"
    assert any("amount" in w and "REAL" in w for w in out["warnings"])
    assert tools.run_query(tmp_path, sql="SELECT SUM(amount) AS s FROM sales")["rows"] == [[3399.5]]


@pytest.mark.parametrize("text, value", [("$1,200.00", 1200.0), ("(35.10)", -35.1), ("-$12.50", -12.5),
                                         ("$-12.50", -12.5), ("1e3", 1000.0), ("12", 12.0),
                                         ("1,2", None), ("Smith, John", None), ("$", None), ("", None)])
def test_parse_number(text, value):
    assert tools.parse_number(text) == value


def test_non_utf8_exports_and_blank_lines_load(tmp_path):
    """Spreadsheets often save Windows-1252; a stray blank line is not a row."""
    (tmp_path / "inputs").mkdir()
    (tmp_path / "inputs" / "clients.csv").write_bytes(
        "id,name,fee\n1,Café Nord,10\n\n2,Zürich AG,20\n\n".encode("cp1252"))
    out = tools.list_tables(tmp_path)
    assert out["tables"][0]["rows"] == 2
    assert any("Windows-1252" in w for w in out["warnings"])
    rows = tools.run_query(tmp_path, sql="SELECT name, fee FROM clients ORDER BY id")["rows"]
    assert rows == [["Café Nord", 10], ["Zürich AG", 20]]


def test_an_unparseable_csv_is_skipped_with_a_warning(tmp_path):
    (tmp_path / "inputs").mkdir()
    (tmp_path / "inputs" / "ok.csv").write_text("a\n1\n", encoding="utf-8")
    (tmp_path / "inputs" / "bad.csv").write_text("a\n" + "x" * 200_000 + "\n", encoding="utf-8")
    out = tools.list_tables(tmp_path)
    assert [t["table"] for t in out["tables"]] == ["ok"]
    assert any(w.startswith("bad.csv: cannot be parsed as CSV") for w in out["warnings"])


def test_exempt_sections_can_be_emptied(ws):
    _analysis(ws, "August paid revenue was $4,436.00 [F:aug-rev].\n\n## Method\n\nTolerance 0.5%.")
    assert checks.figures_match_queries(ws, {"milestone": "m3-analysis"})["passed"] is True
    res = checks.figures_match_queries(ws, {"milestone": "m3-analysis", "exempt_sections": []})
    assert res["passed"] is False and "'0.5%'" in res["details"]


def test_ids_beyond_64_bits_stay_text(tmp_path):
    (tmp_path / "inputs").mkdir()
    (tmp_path / "inputs" / "orders.csv").write_text(
        "order_id,total\n12345678901234567890,5\n2,7\n", encoding="utf-8")
    table = tools.list_tables(tmp_path)["tables"][0]
    assert {c["name"]: c["type"] for c in table["columns"]} == {"order_id": "TEXT", "total": "INTEGER"}


def test_keyword_error_suggests_quoting_the_identifier(tmp_path):
    (tmp_path / "inputs").mkdir()
    (tmp_path / "inputs" / "builds.csv").write_text("release,ok\nv1,1\n", encoding="utf-8")
    with pytest.raises(ToolError, match='quote it as an identifier: "release"'):
        tools.run_query(tmp_path, sql="SELECT release FROM builds")
    assert tools.run_query(tmp_path, sql='SELECT "release" FROM builds')["rows"] == [["v1"]]


def _contacts(ws: Path) -> None:
    (ws / "inputs" / "contacts.csv").write_text(
        "contact_id,email,phone,plan\n1,alice.smith@example.com,555-0100,pro\n"
        "2,bob.jones@example.com,555-0101,free\n3,,555-0102,pro\n", encoding="utf-8")


def test_profile_masks_personal_data_and_the_check_accepts_it(ws):
    _contacts(ws)
    out = tools.profile_tables(ws, mask_columns=["contacts.phone", "no_such_column"])
    text = (ws / out["path"]).read_text(encoding="utf-8")
    assert "alice.smith" not in text and "555-01" not in text
    assert out["masked_columns"] == ["contacts.email", "contacts.phone"]
    assert out["unmatched_mask_columns"] == ["no_such_column"]
    email = next(c for c in json.loads(text)["tables"]["contacts"]["columns"] if c["name"] == "email")
    assert email == {"name": "email", "type": "TEXT", "nulls": 1, "null_rate": 0.3333, "distinct": 2,
                     "masked": True}
    res = checks.profile_matches_source(ws, {})
    assert res["passed"] is True, res


def test_profile_check_still_verifies_counts_and_keeps_emails_masked(ws):
    _contacts(ws)
    tools.profile_tables(ws)
    path = ws / "deliverables/m1-profile/profile.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    email = next(c for c in doc["tables"]["contacts"]["columns"] if c["name"] == "email")
    email["nulls"] = 0
    path.write_text(json.dumps(doc), encoding="utf-8")
    res = checks.profile_matches_source(ws, {})
    assert res["passed"] is False and "email.nulls" in res["details"]
    email.update(nulls=1, masked=False, min="alice.smith@example.com", max="bob.jones@example.com")
    path.write_text(json.dumps(doc), encoding="utf-8")
    res = checks.profile_matches_source(ws, {})
    assert res["passed"] is False and "email holds personal data" in res["details"]


def test_runaway_queries_are_stopped_and_the_session_stays_usable(ws):
    endless = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) "
    session = tools.open_session(ws)
    try:
        with pytest.raises(ToolError, match="time limit"):
            tools.execute(session, endless + "SELECT COUNT(*) FROM c", timeout=0.3)
        with pytest.raises(ToolError, match="more than 1,000 rows"):
            tools.execute(session, endless + "SELECT x FROM c", max_rows=1000)
        assert tools.execute(session, "SELECT COUNT(*) AS n FROM invoices").rows == [(62,)]
    finally:
        session.close()
