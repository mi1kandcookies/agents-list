"""bookkeeping-close domain pack: deterministic tools, acceptance checks and
manifest wiring, run offline against the synthetic Harbor Lane fixture."""
import csv
import json
import shutil
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from agentkit.errors import ToolError
from specialists.bookkeeping_close import tools as t

PACK = Path(__file__).resolve().parents[2] / "specialists" / "bookkeeping_close"
FIXTURE = PACK / "evals" / "fixtures" / "harbor_lane"
M2 = "deliverables/m2-period-close"


@pytest.fixture
def ws(tmp_path):
    shutil.copytree(FIXTURE / "inputs", tmp_path / "inputs")
    return tmp_path


def _rows(path):
    with open(path, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _write_rows(path, rows):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _close_period(ws):
    """Run the whole close with the tools, as the agent would."""
    t.build_account_map(ws)
    t.tie_opening_balances(ws, period="2026-08")
    t.categorize_transactions(ws)
    t.find_exceptions(ws)
    t.reconcile_bank(ws, cash_account="1000", period="2026-08")
    t.build_accrual_schedule(ws, period="2026-08")
    t.draft_journal_entry(ws, entry_id="ADJ-202608-BANK", date="2026-08-31",
                          description="Record August bank service fee and interest per statement",
                          lines=[{"account": "6400", "debit": "25.00"}, {"account": "1000", "credit": "25.00"},
                                 {"account": "1000", "debit": "3.18"}, {"account": "4900", "credit": "3.18"}],
                          support="bank_reconciliation.json unrecorded rows 17-18")
    t.build_trial_balance(ws)
    t.flux_analysis(ws)
    t.build_financial_statements(ws)


# --- helpers ---------------------------------------------------------------------

def test_money_parses_common_formats():
    assert t.money("$1,234.50") == Decimal("1234.50")
    assert t.money("(45.00)") == Decimal("-45.00")
    assert t.money("") == Decimal("0.00")
    with pytest.raises(ToolError):
        t.money("12abc")
    with pytest.raises(ToolError):
        t.money("NaN")


def test_paths_are_jailed_and_outputs_go_to_deliverables(ws):
    with pytest.raises(ToolError):
        t.resolve(ws, "../outside.csv")
    with pytest.raises(ToolError):
        t.categorize_transactions(ws, output="inputs/categorized.csv")
    assert t.period_bounds("2026-12")[1].isoformat() == "2026-12-31"
    with pytest.raises(ToolError):
        t.period_bounds("August")


# --- tools -----------------------------------------------------------------------

def test_parse_bank_statement_totals_and_breaks(ws):
    out = t.parse_bank_statement(ws)
    assert out["rows"] == 18
    assert out["opening_balance"] == "42350.00" and out["ending_balance"] == "36945.58"
    assert out["net_activity"] == "-5404.42" and out["running_balance_breaks"] == []
    rows = _rows(ws / "inputs/bank_statement.csv")
    rows[5]["balance"] = "1.00"   # tampered running balance
    _write_rows(ws / "inputs/bank_statement.csv", rows)
    assert t.parse_bank_statement(ws)["running_balance_breaks"]


def test_categorize_transactions_flags_unknown_and_low_confidence(ws):
    out = t.categorize_transactions(ws)
    assert out["rows"] == 18 and out["uncategorized"] == 1 and out["needs_review"] == 2
    rows = _rows(ws / M2 / "categorized.csv")
    venmo = next(r for r in rows if r["description"] == "VENMO J MARSH")
    assert venmo["account"] == "" and venmo["needs_review"] == "yes"


def test_find_exceptions_queues_suspense_and_duplicates(ws):
    t.categorize_transactions(ws)
    out = t.find_exceptions(ws)
    reasons = [i["reason"] for i in out["items"]]
    assert any("suspense account 6999" in r for r in reasons)
    assert "6999" in out["suspense_accounts"] and "1999" in out["suspense_accounts"]
    rows = _rows(ws / M2 / "categorized.csv")
    rows.append(dict(rows[10], row="19", date="2026-08-19"))
    _write_rows(ws / M2 / "categorized.csv", rows)
    assert any(i["reason"] == "possible duplicate" for i in t.find_exceptions(ws)["items"])


def test_reconcile_bank_ties_to_the_cent(ws):
    out = t.reconcile_bank(ws, cash_account="1000", period="2026-08")
    assert out["unexplained_difference"] == "0.00"
    assert out["gl_ending_balance"] == "36811.00" and out["matched_count"] == 16
    assert {i["amount"] for i in out["outstanding_items"]} == {"-1275.00", "1118.60"}
    assert {i["amount"] for i in out["unrecorded_items"]} == {"-25.00", "3.18"}
    with pytest.raises(ToolError):
        t.reconcile_bank(ws, period="2026-08")


def test_reconcile_bank_date_window_matters(ws):
    # The check 1041 posted 08-02 clears 08-05: outside a 1-day window it is
    # both outstanding and unrecorded, which still nets to zero difference.
    out = t.reconcile_bank(ws, cash_account="1000", period="2026-08", date_window=1)
    assert out["matched_count"] < 16 and out["unexplained_difference"] == "0.00"


def test_draft_journal_entry_refuses_bad_entries(ws):
    good = dict(entry_id="ADJ-1", date="2026-08-31", description="Bank fee per statement",
                lines=[{"account": "6400", "debit": "25"}, {"account": "1000", "credit": "25"}], support="stmt row 17")
    assert t.draft_journal_entry(ws, **good)["status"] == t.DRAFT_STATUS
    with pytest.raises(ToolError):
        t.draft_journal_entry(ws, **good)                                      # duplicate id
    with pytest.raises(ToolError):
        t.draft_journal_entry(ws, **dict(good, entry_id="ADJ-2", lines=[{"account": "6400", "debit": "25"},
                                                                         {"account": "1000", "credit": "24"}]))
    with pytest.raises(ToolError):
        t.draft_journal_entry(ws, **dict(good, entry_id="ADJ-3", support=""))
    with pytest.raises(ToolError):
        t.draft_journal_entry(ws, **dict(good, entry_id="ADJ-4", description="Plug to balance cash"))
    with pytest.raises(ToolError):
        t.draft_journal_entry(ws, **dict(good, entry_id="ADJ-5", lines=[{"account": "1999", "debit": "25"},
                                                                         {"account": "1000", "credit": "25"}]))
    with pytest.raises(ToolError):
        t.draft_journal_entry(ws, **dict(good, entry_id="ADJ-6", lines=[{"account": "9999", "debit": "25"},
                                                                         {"account": "1000", "credit": "25"}]))


def test_build_accrual_schedule_rolls_forward(ws):
    out = t.build_accrual_schedule(ws, period="2026-08")
    amounts = {d["entry_id"]: d["amount"] for d in out["drafted"]}
    assert amounts == {"ADJ-202608-PRE-001": "1000.00", "ADJ-202608-DEP-001": "600.00",
                       "ADJ-202608-DEF-001": "1000.00", "ADJ-202608-ACR-001": "540.00"}
    je = _rows(ws / M2 / "journal_entries.csv")
    deferral = [r for r in je if r["entry_id"] == "ADJ-202608-DEF-001"]
    assert {(r["account"], r["debit"], r["credit"]) for r in deferral} == {("2200", "1000.00", ""),
                                                                         ("4000", "", "1000.00")}
    assert all(r["status"] == t.DRAFT_STATUS and r["support"] for r in je)


def test_build_trial_balance_balances(ws):
    _close_period(ws)
    out = t.build_trial_balance(ws)
    assert out["balanced"] and out["total_debits"] == "115861.78" and out["unknown_accounts"] == []
    tb = {r["account"]: r for r in _rows(ws / M2 / "trial_balance.csv")}
    assert tb["1000"]["debit"] == "36789.18"


def test_flux_analysis_flags_material_changes(ws):
    _close_period(ws)
    out = t.flux_analysis(ws)
    assert {r["account"] for r in out["flagged_rows"]} == {"1000", "1100", "1200", "2000", "2200", "4000"}


def test_tie_opening_balances_reports_differences(ws):
    assert t.tie_opening_balances(ws, period="2026-08")["differences"] == []
    rows = _rows(ws / "inputs/prior_trial_balance.csv")
    rows[1]["debit"] = "8250.00"
    _write_rows(ws / "inputs/prior_trial_balance.csv", rows)
    diffs = t.tie_opening_balances(ws, period="2026-08")["differences"]
    assert [d["account"] for d in diffs] == ["1100"] and diffs[0]["difference"] == "-50.00"


def test_build_account_map_and_statements(ws):
    assert t.build_account_map(ws)["unmapped_gl_accounts"] == []
    _close_period(ws)
    out = t.build_financial_statements(ws)
    assert out["difference"] == "0.00" and out["net_income"] == "-13850.82"


def test_tool_defs_are_well_formed():
    names = [d["name"] for d in t.TOOL_DEFS]
    assert len(names) == len(set(names))
    for d in t.TOOL_DEFS:
        assert callable(d["function"]) and d["risk"] in ("read", "write")
        assert d["input_schema"]["type"] == "object" and d["description"]
