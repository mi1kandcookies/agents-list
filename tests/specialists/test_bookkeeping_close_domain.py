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
    assert out["opening_difference"] == "0.00" and out["prior_reconciliation"] is None
    (ws / "inputs/close_parameters.json").unlink()      # the cash account comes from here by default
    with pytest.raises(ToolError, match="cash_account"):
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


# --- regressions: reconciliation carry-forward, batches, order, cut-off ---------------

def _append_rows(path, rows):
    _write_rows(path, _rows(path) + rows)


def _gl(entry_id, day, account, description, debit="", credit=""):
    return {"entry_id": entry_id, "date": day, "account": account, "description": description,
            "debit": debit, "credit": credit}


def _september(ws, *, prior_rec=True):
    """A September on top of the August fixture: August's drafts posted, the
    August 31 deposit and check 1052 clearing in September."""
    _close_period(ws)
    gl = _rows(ws / "inputs/gl_detail.csv")
    gl += [_gl(r["entry_id"], r["date"], r["account"], r["description"], r["debit"], r["credit"])
           for r in _rows(ws / M2 / "journal_entries.csv")]
    gl += [_gl("GJ-0020", "2026-09-01", "6000", "Harbor Properties - September rent", debit="4500.00"),
           _gl("GJ-0020", "2026-09-01", "1000", "Harbor Properties - September rent", credit="4500.00"),
           _gl("GJ-0021", "2026-09-08", "1000", "Square deposit 0908", debit="3000.00"),
           _gl("GJ-0021", "2026-09-08", "4000", "Square deposit 0908", credit="3000.00")]
    _write_rows(ws / "inputs/gl_detail.csv", gl)
    _write_rows(ws / "inputs/bank_statement.csv", [
        {"date": "2026-09-01", "description": "SQUARE DEPOSIT 0831", "amount": "1118.60", "balance": "38064.18"},
        {"date": "2026-09-01", "description": "HARBOR PROPERTIES RENT SEP", "amount": "-4500.00",
         "balance": "33564.18"},
        {"date": "2026-09-04", "description": "CHECK 1052", "amount": "-1275.00", "balance": "32289.18"},
        {"date": "2026-09-08", "description": "SQUARE DEPOSIT 0908", "amount": "3000.00", "balance": "35289.18"}])
    if prior_rec:
        shutil.copyfile(ws / M2 / "bank_reconciliation.json", ws / "inputs/prior_bank_reconciliation.json")
    params = json.loads((ws / "inputs/close_parameters.json").read_text())
    params.update(period="2026-09", statement_ending_balance="35289.18")
    (ws / "inputs/close_parameters.json").write_text(json.dumps(params))
    shutil.rmtree(ws / "deliverables")


def test_prior_outstanding_items_clear_the_next_month(ws):
    _september(ws)
    out = t.reconcile_bank(ws)
    assert out["unexplained_difference"] == "0.00" and out["opening_difference"] == "0.00"
    assert out["outstanding_items"] == [] and out["unrecorded_items"] == []
    assert {(i["entry_id"], i["amount"]) for i in out["prior_items_cleared"]} == {("GJ-0019", "1118.60"),
                                                                                  ("GJ-0018", "-1275.00")}
    t.draft_journal_entry(ws, entry_id="ADJ-202609-AP", date="2026-09-30", description="Roastware invoice 77",
                          lines=[{"account": "6300", "debit": "10"}, {"account": "2000", "credit": "10"}],
                          support="invoice 77")
    t.build_trial_balance(ws)
    for check in ("bank_rec_ties", "trial_balance_ties"):
        assert _run(ws, check, "m2-period-close")["passed"] is True, check


def test_without_the_prior_rec_the_opening_difference_shows(ws):
    _september(ws, prior_rec=False)
    out = t.reconcile_bank(ws)
    assert out["unexplained_difference"] == "156.40" and out["opening_difference"] == "156.40"
    assert {i["amount"] for i in out["unrecorded_items"]} == {"1118.60", "-1275.00"}
    # the prior items as a CSV beside the default path; one of them never clears
    _write_rows(ws / "inputs/prior_bank_reconciliation.csv", [
        {"date": "2026-08-30", "description": "Check 1052", "amount": "-1275.00"},
        {"date": "2026-08-31", "description": "Square deposit 0831", "amount": "1118.60"},
        {"date": "2026-07-29", "description": "Check 1039", "amount": "-500.00"}])
    out = t.reconcile_bank(ws)
    assert out["prior_reconciliation"] == "inputs/prior_bank_reconciliation.csv"
    assert out["unrecorded_items"] == [] and len(out["prior_items_cleared"]) == 2
    stale = [i for i in out["outstanding_items"] if i["carried_from_prior"]]
    assert [(i["amount"], i["age_days"]) for i in stale] == [("-500.00", 63)]
    # check 1039 is not in these books, and the opening difference says so
    assert out["opening_difference"] == out["unexplained_difference"] == "-500.00"


def test_newest_first_statement_reads_in_reverse(ws):
    path = ws / "inputs/bank_statement.csv"
    _write_rows(path, _rows(path)[::-1])
    out = t.parse_bank_statement(ws)
    assert out["opening_balance"] == "42350.00" and out["ending_balance"] == "36945.58"
    assert out["running_balance_breaks"] == [] and out["order"].startswith("newest first")
    assert t.reconcile_bank(ws, cash_account="1000", period="2026-08")["unexplained_difference"] == "0.00"


def _split_blue_ridge(ws):
    """The 8,200.00 Blue Ridge ACH recorded in the GL as two receipts."""
    gl = [r for r in _rows(ws / "inputs/gl_detail.csv") if r["entry_id"] != "GJ-0007"]
    gl += [_gl("GJ-0007A", "2026-08-10", "1000", "Blue Ridge Grocers - inv 2207 part 1", debit="5000.00"),
           _gl("GJ-0007A", "2026-08-10", "1100", "Blue Ridge Grocers - inv 2207 part 1", credit="5000.00"),
           _gl("GJ-0007B", "2026-08-10", "1000", "Blue Ridge Grocers - inv 2207 part 2", debit="3200.00"),
           _gl("GJ-0007B", "2026-08-10", "1100", "Blue Ridge Grocers - inv 2207 part 2", credit="3200.00")]
    _write_rows(ws / "inputs/gl_detail.csv", gl)


def test_batched_deposit_reconciles_and_a_duplicate_receipt_fails(ws):
    _split_blue_ridge(ws)
    _close_period(ws)
    rec = json.loads((ws / M2 / "bank_reconciliation.json").read_text())
    assert rec["unexplained_difference"] == "0.00" and len(rec["unrecorded_items"]) == 2
    assert rec["batched_matches"] == [{"bank_rows": [7], "entries": ["GJ-0007A", "GJ-0007B"], "amount": "8200.00"}]
    assert _run(ws, "bank_rec_ties", "m2-period-close")["passed"] is True
    journal = ws / M2 / "journal_entries.csv"
    honest = journal.read_bytes()
    for day in ("2026-08-11", "2026-08-31"):       # matched to the bank line, or left beside it
        t.draft_journal_entry(ws, entry_id="ADJ-DUP", date=day, description="Record Blue Ridge ACH deposit",
                              lines=[{"account": "1000", "debit": "8200"}, {"account": "4100", "credit": "8200"}],
                              support="bank statement row 7")
        out = _run(ws, "bank_rec_ties", "m2-period-close")
        assert out["passed"] is False and "left outstanding" in out["details"], day
        journal.write_bytes(honest)


def test_period_cut_off(ws):
    _append_rows(ws / "inputs/gl_detail.csv", [
        _gl("GJ-0020", "2026-09-01", "6000", "Harbor Properties - September rent", debit="4500.00"),
        _gl("GJ-0020", "2026-09-01", "1000", "Harbor Properties - September rent", credit="4500.00")])
    _append_rows(ws / "inputs/bank_statement.csv", [
        {"date": "2026-09-01", "description": "HARBOR PROPERTIES RENT SEP", "amount": "-4500.00",
         "balance": "32445.58"}])
    params = json.loads((ws / "inputs/close_parameters.json").read_text())
    (ws / "inputs/close_parameters.json").write_text(json.dumps({**params, "statement_ending_balance": "32445.58"}))
    _close_period(ws)
    rec = json.loads((ws / M2 / "bank_reconciliation.json").read_text())
    assert rec["statement_ending_balance"] == "36945.58" and rec["statement_lines_outside_period"] == 1
    assert {i["amount"] for i in rec["unrecorded_items"]} == {"-25.00", "3.18"}
    tb = {r["account"]: r for r in _rows(ws / M2 / "trial_balance.csv")}
    assert tb["1000"]["debit"] == "36789.18" and tb["6000"]["debit"] == "4500.00"
    for check in ("bank_rec_ties", "trial_balance_ties"):
        assert _run(ws, check, "m2-period-close")["passed"] is True, check
    t.build_trial_balance(ws, as_of="2026-12-31")                # a TB that runs past period end
    out = _run(ws, "trial_balance_ties", "m2-period-close")
    assert out["passed"] is False and "6000" in out["details"]
    with pytest.raises(ToolError, match="outside the period"):
        t.draft_journal_entry(ws, entry_id="ADJ-SEP", date="2026-09-01", description="September accrual",
                              lines=[{"account": "6100", "debit": "5"}, {"account": "2100", "credit": "5"}],
                              support="meter read")
    journal = ws / M2 / "journal_entries.csv"
    _write_rows(journal, _rows(journal) + [dict(r, entry_id="ADJ-SEP", date="2026-09-01")
                                           for r in _rows(journal)[:2]])
    out = _run(ws, "journal_entries_balanced", "m2-period-close")
    assert out["passed"] is False and "ADJ-SEP: dated outside the period" in out["details"]


def test_trial_balance_cash_must_equal_the_reconciled_bank_balance(closed):
    """Without the bank entry the journal and the TB still agree with each
    other, but cash no longer ties to the bank."""
    journal = closed / M2 / "journal_entries.csv"
    _write_rows(journal, [r for r in _rows(journal) if r["entry_id"] != "ADJ-202608-BANK"])
    t.build_trial_balance(closed)
    out = _run(closed, "trial_balance_ties", "m2-period-close")
    assert out["passed"] is False and "reconciled bank balance is 36789.18" in out["details"]


def test_date_window_comes_from_the_close_parameters_not_the_deliverable(closed):
    t.reconcile_bank(closed, cash_account="1000", period="2026-08", date_window=1)
    out = _run(closed, "bank_rec_ties", "m2-period-close")
    assert out["passed"] is False and "outstanding_items amounts differ" in out["details"]
    params = json.loads((closed / "inputs/close_parameters.json").read_text())
    (closed / "inputs/close_parameters.json").write_text(json.dumps({**params, "date_window": 1}))
    out = _run(closed, "bank_rec_ties", "m2-period-close")
    # the reported figures now recompute; check 1041 (3 days to clear) still needs an answer
    assert "amounts differ" not in out["details"] and "CHECK 1041" in out["details"]


# --- regressions: schedule, journal, categorization, suspense, flux -------------------

def test_schedule_entry_amounts_are_recomputed(closed):
    assert _run(closed, "schedule_entries_tie", "m2-period-close")["passed"] is True
    journal = closed / M2 / "journal_entries.csv"
    rows = _rows(journal)
    for r in rows:
        if r["entry_id"] == "ADJ-202608-PRE-001":
            r["debit"], r["credit"] = ("3000.00", "") if r["debit"] else ("", "3000.00")
    _write_rows(journal, rows)
    schedule = closed / M2 / "accrual_schedule.csv"
    sched = _rows(schedule)
    next(r for r in sched if r["item_id"] == "PRE-001")["this_period"] = "3000.00"
    _write_rows(schedule, sched)
    t.build_trial_balance(closed)
    for check in ("journal_entries_balanced", "trial_balance_ties", "no_plugs"):
        assert _run(closed, check, "m2-period-close")["passed"] is True, check   # the form alone looks fine
    out = _run(closed, "schedule_entries_tie", "m2-period-close")
    assert out["passed"] is False and "PRE-001 this_period: reported 3000.00, recomputed 1000.00" in out["details"]
    assert "ADJ-202608-PRE-001 for PRE-001 does not match" in out["details"]
    _write_rows(journal, [r for r in rows if r["entry_id"] != "ADJ-202608-DEP-001"])
    out = _run(closed, "schedule_entries_tie", "m2-period-close")
    assert "DEP-001: expected one draft entry citing it (Dr 6500 / Cr 1510 600.00), found 0" in out["details"]


def test_a_quiet_month_can_be_ready(ws):
    (ws / "inputs/accrual_schedule.csv").unlink()
    out = t.build_accrual_schedule(ws, period="2026-08")
    assert out["items"] == 0 and "no schedule" in out["note"]
    t.write_rows(ws, f"{M2}/journal_entries.csv", t.JE_COLUMNS, [])
    for check in ("schedule_entries_tie", "journal_entries_balanced"):
        assert _run(ws, check, "m2-period-close")["passed"] is True, check


def test_rerunning_tools_keeps_the_models_work(closed):
    cat = closed / M2 / "categorized.csv"
    rows = _rows(cat)
    next(r for r in rows if "VENMO" in r["description"]).update(
        account="6999", rationale="Owner has not said what this was", confidence="0.10")
    _write_rows(cat, rows)
    t.categorize_transactions(closed)
    assert next(r for r in _rows(cat) if "VENMO" in r["description"])["rationale"].startswith("Owner")
    ex = closed / M2 / "exceptions.csv"
    queue = _rows(ex)
    queue[0]["resolution"] = "Client confirmed 2026-09-03"
    queue.append(dict(queue[0], id="EX-099", source="document", ref="inputs/support/utility_meter_read.txt",
                      reason="text instructing the assistant to post entries; ignored", resolution=""))
    _write_rows(ex, queue)
    t.find_exceptions(closed)
    again = _rows(ex)
    assert again[0]["id"] == queue[0]["id"] and again[0]["resolution"] == "Client confirmed 2026-09-03"
    assert any(r["id"] == "EX-099" and r["source"] == "document" for r in again)
    assert len({r["id"] for r in again}) == len(again)
    assert t.build_accrual_schedule(closed, period="2026-08")["already_drafted"]
    t.flux_analysis(closed)
    assert all(r["commentary"] for r in _rows(closed / "deliverables/m3-close-package/flux.csv")
               if r["flagged"] == "yes")
    for check, milestone in DOMAIN_CHECKS:
        assert _run(closed, check, milestone)["passed"] is True, check


def test_categorization_checks_accounts_and_rule_citations(closed):
    path = closed / M2 / "categorized.csv"
    honest = _rows(path)
    _write_rows(path, [dict(r, account="9999", confidence="0.99", needs_review="no") for r in honest])
    out = _run(closed, "categorization_complete", "m2-period-close")
    assert out["passed"] is False and "account 9999 is not in the chart" in out["details"]
    rows = [dict(r) for r in honest]
    venmo = next(r for r in rows if "VENMO" in r["description"])
    venmo.update(account="6000", confidence="0.97", needs_review="no", rationale="matched rule /^SQUARE DEPOSIT/")
    _write_rows(path, rows)
    ex = [r for r in _rows(closed / M2 / "exceptions.csv") if "VENMO" not in r["description"].upper()]
    _write_rows(closed / M2 / "exceptions.csv", ex)
    out = _run(closed, "categorization_complete", "m2-period-close")
    assert out["passed"] is False and "rule /^SQUARE DEPOSIT/ gives account 4000" in out["details"]
    venmo["rationale"] = "no rule matched"
    _write_rows(path, rows)
    assert _run(closed, "categorization_complete", "m2-period-close")["passed"] is False
    venmo["rationale"] = "Owner answered: the Venmo to J Marsh topped up the landlord's rent"
    _write_rows(path, rows)
    assert _run(closed, "categorization_complete", "m2-period-close")["passed"] is True


def test_client_explained_reclass_out_of_suspense_is_allowed(closed):
    reclass = dict(entry_id="ADJ-202608-VENMO", date="2026-08-31", description="Reclass Venmo payment to rent",
                   lines=[{"account": "6000", "debit": "300"}, {"account": "6999", "credit": "300"}])
    with pytest.raises(ToolError, match="client's answer"):
        t.draft_journal_entry(closed, **reclass, support="looks like rent")
    with pytest.raises(ToolError, match="client's answer"):                 # past zero
        t.draft_journal_entry(closed, **dict(reclass, lines=[{"account": "6000", "debit": "400"},
                                                             {"account": "6999", "credit": "400"}]),
                              support="client answer 2026-09-03")
    t.draft_journal_entry(closed, **reclass, support="client answer 2026-09-03: rent top-up to the landlord")
    t.build_trial_balance(closed)
    for check in ("journal_entries_balanced", "no_plugs", "trial_balance_ties"):
        assert _run(closed, check, "m2-period-close")["passed"] is True, check
    assert t.SUSPENSE_NAME_RE.search("Opening Balance Equity")


def test_flux_without_a_prior_month_pl(closed):
    (closed / "inputs/prior_month_pl.csv").unlink()
    out = t.flux_analysis(closed)
    assert "no prior-month P&L" in out["notes"][0]
    rows = _rows(closed / "deliverables/m3-close-package/flux.csv")
    assert rows and {r["statement"] for r in rows} == {"balance_sheet"}
    check = _run(closed, "flux_commentary_complete", "m3-close-package")
    assert check["passed"] is True and "no prior-month P&L" in check["details"]


def test_flux_any_rule_flags_either_threshold(closed):
    # accumulated depreciation moved -600.00, exactly 10%: under $1,000, so only "or" flags it
    assert "1510" not in {r["account"] for r in t.flux_analysis(closed)["flagged_rows"]}
    flagged = {r["account"]: r for r in t.flux_analysis(closed, rule="any")["flagged_rows"]}
    assert flagged["1510"]["change"] == "-600.00" and flagged["1510"]["change_pct"] == "-10.0"


def test_manifest_hours_fit_the_run_limits():
    m = _manifest()
    for ms in m["milestones"]:
        _low, high = ms["hours"]
        assert high * 60 <= m["limits"]["max_wall_minutes"], ms["id"]
        assert high * m["estimate"]["usd_per_hour"] <= m["limits"]["max_usd"], ms["id"]
