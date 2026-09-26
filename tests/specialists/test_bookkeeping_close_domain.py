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
    # refused lexically, before the filesystem (or an SMB share) is touched
    for bad in ("//fileserver/share/gl.csv", "\\\\fileserver\\share\\gl.csv"):
        with pytest.raises(ToolError, match="UNC"):
            t.resolve(ws, bad)
    with pytest.raises(ToolError, match="internal to the kit"):
        t.resolve(ws, ".agentkit/ledger.json")
    with pytest.raises(ToolError, match="deliverables/"):
        t.resolve(ws, "deliverables", write=True)
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


# --- checks ----------------------------------------------------------------------

from specialists.bookkeeping_close.checks import CHECK_DEFS  # noqa: E402

KIT_BUILTIN_CHECKS = {"file_exists", "files_exist", "markdown_sections", "no_placeholders", "word_count",
                      "json_valid", "csv_columns", "command_succeeds", "ledger_verified", "citations_resolve",
                      "disclaimer_present", "rubric_grader", "human_signoff"}
KIT_TOOLS = {"read_file", "write_file", "edit_file", "list_files", "search_files", "run_command",
             "http_fetch", "web_search", "read_document", "record_source", "record_claim", "ask_client",
             "post_progress", "submit_milestone"}


def _manifest():
    return yaml.safe_load((PACK / "agent.yaml").read_text(encoding="utf-8"))


def _params(check, milestone):
    m = next(m for m in _manifest()["milestones"] if m["id"] == milestone)
    return next(a["params"] for a in m["acceptance"] if a["check"] == check)


def _run(ws, check, milestone):
    return CHECK_DEFS[check](ws, _params(check, milestone))


@pytest.fixture
def closed(ws):
    _close_period(ws)
    flux_path = ws / "deliverables/m3-close-package/flux.csv"
    rows = _rows(flux_path)
    for r in rows:
        if r["flagged"] == "yes":
            r["commentary"] = f"Movement of {r['change']} traced to August activity and schedules"
    _write_rows(flux_path, rows)
    return ws


DOMAIN_CHECKS = [("bank_rec_ties", "m2-period-close"), ("categorization_complete", "m2-period-close"),
                 ("journal_entries_balanced", "m2-period-close"), ("trial_balance_ties", "m2-period-close"),
                 ("no_plugs", "m2-period-close"), ("flux_commentary_complete", "m3-close-package"),
                 ("financial_statements_tie", "m3-close-package"), ("opening_balances_tie", "m1-onboarding"),
                 ("account_map_complete", "m1-onboarding")]


@pytest.mark.parametrize("check,milestone", DOMAIN_CHECKS)
def test_every_domain_check_passes_on_an_honest_close(closed, check, milestone):
    out = _run(closed, check, milestone)
    assert out["passed"] is True, out["details"]
    assert out["score"] == 1.0


@pytest.mark.parametrize("check,milestone", DOMAIN_CHECKS)
def test_checks_fail_cleanly_when_deliverables_are_missing(ws, check, milestone):
    out = _run(ws, check, milestone)
    assert out["passed"] is False and out["details"]


def test_bank_rec_forged_tie_fails(closed):
    path = closed / M2 / "bank_reconciliation.json"
    rec = json.loads(path.read_text())
    rec["gl_ending_balance"] = "36945.58"          # claims the books equal the bank
    rec["outstanding_items"] = []
    path.write_text(json.dumps(rec))
    out = _run(closed, "bank_rec_ties", "m2-period-close")
    assert out["passed"] is False and "gl_ending_balance" in out["details"]


def test_bank_rec_fails_when_books_do_not_tie(closed):
    rows = _rows(closed / "inputs/gl_detail.csv")
    rows[0]["debit"] = "42300.00"                  # opening cash off by 50
    rows[9]["credit"] = "26950.00"                 # retained earnings keeps the GL balanced
    _write_rows(closed / "inputs/gl_detail.csv", rows)
    t.reconcile_bank(closed, cash_account="1000", period="2026-08")
    out = _run(closed, "bank_rec_ties", "m2-period-close")
    assert out["passed"] is False and "unexplained difference 50.00" in out["details"]


def test_bank_rec_requires_entries_for_bank_only_items(closed):
    je = [r for r in _rows(closed / M2 / "journal_entries.csv") if r["entry_id"] != "ADJ-202608-BANK"]
    _write_rows(closed / M2 / "journal_entries.csv", je)
    out = _run(closed, "bank_rec_ties", "m2-period-close")
    assert out["passed"] is False and "MONTHLY SERVICE FEE" in out["details"]


def test_bank_rec_catches_stated_balance_mismatch(closed):
    params = json.loads((closed / "inputs/close_parameters.json").read_text())
    params["statement_ending_balance"] = "36945.00"
    (closed / "inputs/close_parameters.json").write_text(json.dumps(params))
    assert _run(closed, "bank_rec_ties", "m2-period-close")["passed"] is False


def test_categorization_dropped_or_unqueued_rows_fail(closed):
    path = closed / M2 / "categorized.csv"
    rows = _rows(path)
    _write_rows(path, rows[:-1])
    assert _run(closed, "categorization_complete", "m2-period-close")["passed"] is False
    _write_rows(path, rows)
    ex = [r for r in _rows(closed / M2 / "exceptions.csv") if "VENMO" not in r["description"].upper()]
    _write_rows(closed / M2 / "exceptions.csv", ex)
    out = _run(closed, "categorization_complete", "m2-period-close")
    assert out["passed"] is False and "VENMO" in out["details"]


def test_unbalanced_or_unsupported_entry_fails(closed):
    path = closed / M2 / "journal_entries.csv"
    rows = _rows(path)
    rows[0]["debit"] = "999.00"
    _write_rows(path, rows)
    assert _run(closed, "journal_entries_balanced", "m2-period-close")["passed"] is False
    rows = _rows(path)
    rows[0]["debit"], rows[1]["support"], rows[2]["status"] = "1000.00", "", "posted"
    _write_rows(path, rows)
    out = _run(closed, "journal_entries_balanced", "m2-period-close")
    assert out["passed"] is False and "no support" in out["details"] and "not marked draft" in out["details"]


def test_trial_balance_edited_number_fails(closed):
    path = closed / M2 / "trial_balance.csv"
    rows = _rows(path)
    next(r for r in rows if r["account"] == "1000")["debit"] = "36945.58"
    re_row = next(r for r in rows if r["account"] == "3900")
    re_row["credit"] = t.fmt(t.money(re_row["credit"]) + t.money("156.40"))   # still "balances"
    _write_rows(path, rows)
    out = _run(closed, "trial_balance_ties", "m2-period-close")
    assert out["passed"] is False and "1000" in out["details"] and "debits" not in out["details"]


def test_plug_into_suspense_or_unitemized_suspense_fails(closed):
    path = closed / M2 / "journal_entries.csv"
    rows = _rows(path)
    rows += [dict(rows[0], entry_id="ADJ-X", account="1999", debit="12.34", credit="", description="Plug to balance"),
             dict(rows[0], entry_id="ADJ-X", account="1000", debit="", credit="12.34", description="Plug to balance")]
    _write_rows(path, rows)
    out = _run(closed, "no_plugs", "m2-period-close")
    assert out["passed"] is False and "1999" in out["details"] and "plug" in out["details"]
    _write_rows(path, rows[:-2])
    ex = [r for r in _rows(closed / M2 / "exceptions.csv") if r["account"] != "6999"]
    _write_rows(closed / M2 / "exceptions.csv", ex)
    out = _run(closed, "no_plugs", "m2-period-close")
    assert out["passed"] is False and "6999" in out["details"]


def test_flux_missing_or_forged_commentary_fails(closed):
    path = closed / "deliverables/m3-close-package/flux.csv"
    rows = _rows(path)
    target = next(r for r in rows if r["account"] == "4000")
    target["commentary"] = "n/a"
    _write_rows(path, rows)
    out = _run(closed, "flux_commentary_complete", "m3-close-package")
    assert out["passed"] is False and 0 < out["score"] < 1
    target["commentary"] = "Sales up on subscription release and more deposits"
    target["change"] = "-100.00"
    _write_rows(path, rows)
    assert _run(closed, "flux_commentary_complete", "m3-close-package")["passed"] is False


def test_opening_difference_needs_explanation(closed):
    rows = _rows(closed / "inputs/prior_trial_balance.csv")
    rows[1]["debit"] = "8250.00"
    _write_rows(closed / "inputs/prior_trial_balance.csv", rows)
    t.tie_opening_balances(closed, period="2026-08")
    assert _run(closed, "opening_balances_tie", "m1-onboarding")["passed"] is False
    path = closed / "deliverables/m1-onboarding/opening_balance_tieout.csv"
    tie = _rows(path)
    next(r for r in tie if r["account"] == "1100")["explanation"] = "Invoice 2199 credit memo posted after TB export"
    _write_rows(path, tie)
    assert _run(closed, "opening_balances_tie", "m1-onboarding")["passed"] is True


def test_account_map_wrong_statement_fails(closed):
    path = closed / "deliverables/m1-onboarding/account_map.csv"
    rows = _rows(path)
    next(r for r in rows if r["account"] == "6999")["statement"] = "balance_sheet"
    _write_rows(path, [r for r in rows if r["account"] != "6200"])
    out = _run(closed, "account_map_complete", "m1-onboarding")
    assert out["passed"] is False and "6200 is not mapped" in out["details"] and "6999" in out["details"]


def test_financial_statements_forged_total_fails(closed):
    path = closed / "deliverables/m3-close-package/financial_statements.json"
    data = json.loads(path.read_text())
    data["income_statement"]["net_income"] = "1500.00"
    path.write_text(json.dumps(data))
    out = _run(closed, "financial_statements_tie", "m3-close-package")
    assert out["passed"] is False and "net_income" in out["details"]


# --- manifest ----------------------------------------------------------------------

def test_manifest_parses_and_references_known_tools_and_checks():
    m = _manifest()
    assert m["schema_version"] == 1 and m["slug"] == "bookkeeping-close"
    assert m["human_gate"]["required"] is True and "CPA" in m["human_gate"]["reviewer_role"]
    assert m["egress"]["mode"] == "none" and m["shell"]["allow"] == []
    domain_tools = {d["name"] for d in t.TOOL_DEFS}
    assert set(m["tools"]) <= KIT_TOOLS | domain_tools
    assert domain_tools <= set(m["tools"])
    assert [ms["id"] for ms in m["milestones"]] == ["m1-onboarding", "m2-period-close", "m3-close-package"]
    for ms in m["milestones"]:
        assert all(d.startswith(f"deliverables/{ms['id']}/") for d in ms["deliverables"])
        assert "human" in [a.get("kind", "automated") for a in ms["acceptance"]]
        for a in ms["acceptance"]:
            assert a["check"] in KIT_BUILTIN_CHECKS | set(CHECK_DEFS), a["check"]


def test_prompts_and_rubrics_referenced_by_the_manifest_exist():
    m = _manifest()
    for rel in [m["prompts"]["system"], *m["prompts"]["include"]]:
        assert (PACK / rel).is_file(), rel
    system = (PACK / m["prompts"]["system"]).read_text(encoding="utf-8")
    assert "not an audit" in system and "draft - do not post" in system
    rubrics = [a["params"]["rubric"] for ms in m["milestones"] for a in ms["acceptance"]
               if a["check"] == "rubric_grader"]
    assert rubrics
    for rel in rubrics:
        rubric = yaml.safe_load((PACK / rel).read_text(encoding="utf-8"))
        assert rubric["name"] and 0 < rubric["threshold"] <= 1
        assert abs(sum(c["weight"] for c in rubric["criteria"]) - 1.0) < 1e-9
        assert all(c["id"] and c["description"] for c in rubric["criteria"])


def test_eval_cases_are_well_formed():
    ids = {ms["id"] for ms in _manifest()["milestones"]}
    cases = sorted((PACK / "evals" / "cases").glob("*.json"))
    assert len(cases) >= 3
    for path in cases:
        case = json.loads(path.read_text(encoding="utf-8"))
        assert {"name", "brief", "milestone", "notes"} <= set(case) and case["milestone"] in ids
        assert case["brief"]["specialist"] == "bookkeeping-close"
        assert (PACK / "evals" / "fixtures" / case["fixture"] / "inputs").is_dir()
