"""bookkeeping-close end to end: a scripted model drives the real domain tools,
kit tools, policy gate, ledger, acceptance checks and evidence hashing through
Specialist.run_milestone, on a workspace seeded from the Harbor Lane eval
fixture. Offline: no model provider and no network."""
import csv
import hashlib
import io
import itertools
import json
from pathlib import Path

import pytest

from agentkit.__main__ import main
from agentkit.checks.builtin import load_rubric
from agentkit.events import MemorySink
from agentkit.evals import case_brief, load_cases, prepare_workspace
from agentkit.evidence import evidence_hash
from agentkit.ledger import Ledger
from agentkit.llm import ScriptedAdapter
from agentkit.loop import RunOutcome
from agentkit.registry import load_specialist
from agentkit.specialist import RunContext, Specialist
from agentkit.types import ModelResponse, Submission, ToolCall, Usage
from specialists.bookkeeping_close.agent import BookkeepingClose, materiality_thresholds
from specialists.bookkeeping_close.checks import CHECK_DEFS
from specialists.bookkeeping_close.tools import DRAFT_STATUS, TOOL_DEFS

PACK = Path(__file__).resolve().parents[2] / "specialists" / "bookkeeping_close"
M1, M2, M3 = "m1-onboarding", "m2-period-close", "m3-close-package"
D1, D2, D3 = (f"deliverables/{m}" for m in (M1, M2, M3))
_ids = itertools.count(1)


@pytest.fixture
def spec():
    return load_specialist("bookkeeping-close")


def _case(spec, stem):
    return next(c for c in load_cases(spec) if c.path.stem == stem)


def _workspace(spec, tmp_path, stem="harbor_lane_august_close"):
    ws = tmp_path / "ws"
    prepare_workspace(spec, _case(spec, stem), ws)
    return ws


def _brief(spec, stem):
    return case_brief(spec, _case(spec, stem))


def _response(calls):
    return ModelResponse(text="", stop_reason="tool_use", usage=Usage(input_tokens=100, output_tokens=50),
                         model="scripted",
                         tool_calls=[ToolCall(f"call_{next(_ids)}", name, dict(args)) for name, args in calls])


def _adapter(plan):
    """A plan item is (tool, args), a list of them (one turn) or a function
    returning either, called when the model's turn comes (it may read what
    earlier tools wrote, as a model would after read_file)."""
    steps = []
    for item in plan:
        if callable(item):
            steps.append(lambda _history, fn=item: _response(_group(fn())))
        else:
            steps.append(_response(_group(item)))
    return ScriptedAdapter(steps)


def _group(item):
    return item if isinstance(item, list) else [item]


def _run(spec, ws, brief, milestone, plan, *, grader=None):
    events = MemorySink()
    adapter = _adapter(plan)
    ctx = RunContext(brief=brief, workspace=ws, adapter=adapter, grader=grader, events=events)
    return spec.run_milestone(ctx, milestone), adapter, events


def _rows(path):
    with open(path, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _csv_text(rows):
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0]), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def _errors(events):
    return [e.data for e in events.of_type("tool_result") if e.data["is_error"]]


def _assert_submission(spec, ws, sub, milestone, deliverables):
    """Everything a ready submission must carry, re-derived from disk."""
    failing = [(r.check, r.passed, r.details) for r in sub.check_results
               if r.kind == "automated" and r.passed is not True]
    assert sub.status == "ready_for_review", failing
    results = {r.check: r for r in sub.check_results}
    manifest_checks = [a.check for a in spec.manifest.milestone(milestone).acceptance]
    assert [r.check for r in sub.check_results] == manifest_checks
    assert results["human_signoff"].passed is None and results["human_signoff"].kind == "human"
    # the human gate comes from the manifest and stays required
    assert sub.human_review == spec.manifest.human_review()
    assert sub.human_review.required is True and sub.human_review.reviewer_role == "accountant or CPA"
    # every deliverable is hashed as it is on disk
    assert {a.path for a in sub.artifacts} == set(deliverables)
    for a in sub.artifacts:
        data = (ws / a.path).read_bytes()
        assert a.sha256 == hashlib.sha256(data).hexdigest() and a.bytes == len(data)
    # the saved submission re-hashes to the same evidence
    assert sub.evidence_hash.startswith("0x") and len(sub.evidence_hash) == 66
    saved = json.loads(spec.submission_path(ws, milestone).read_text(encoding="utf-8"))
    assert evidence_hash(Submission.from_dict(saved)) == sub.evidence_hash == saved["evidence_hash"]


# --- scripted deliverables ----------------------------------------------------------

def _disclaimer(spec):
    return spec.manifest.human_gate.disclaimer


RULEBOOK = """pattern,account,confidence,note
^SQUARE DEPOSIT,4000,0.97,Daily card settlements
HARBOR PROPERTIES RENT,6000,0.99,Monthly lease
^CHECK \\d+,2000,0.60,Checks clear vendor bills; confirm the payee against AP
CAFE IMPORTS,5000,0.95,Green coffee supplier
BLUE RIDGE GROCERS,1100,0.92,Wholesale customer collections
CITY OF HARBOR UTILITIES,6100,0.98,Utilities
GUSTO PAYROLL,6200,0.96,Payroll runs
ROASTWARE,6300,0.95,Roasting software subscription
PINE ST MARKET,4100,0.90,Wholesale customer
MONTHLY SERVICE FEE,6400,0.99,Bank fees
INTEREST PAID,4900,0.99,Bank interest
^VENMO,,0.00,Peer-to-peer payments always go to the exceptions queue
"""

CHECKLIST = """# Close checklist: Harbor Lane Coffee Roasters LLC

| Step | Owner | Due |
|---|---|---|
| Upload bank statement and GL export | Owner | Business day 1 |
| Categorize bank activity and queue exceptions | Bookkeeping agent | Business day 2 |
| Reconcile operating checking (GL 1000) to the cent | Bookkeeping agent | Business day 2 |
| Draft schedule and bank entries (draft - do not post) | Bookkeeping agent | Business day 3 |
| Review and approve every draft entry | Accountant or CPA | Business day 4 |
| Post approved entries and lock the period | Accountant or CPA | Business day 5 |
"""


def _diagnostic(spec):
    return f"""# Books diagnostic: Harbor Lane Coffee Roasters LLC

## Summary
The chart of accounts covers every account used in the GL export, and the July 31
opening balances agree with the FY2026 closing trial balance account by account
(`opening_balance_tieout.csv`).

## Chart of accounts
Each account maps to the balance sheet or income statement by type (`account_map.csv`).
Accounts 1999 Suspense and 6999 Ask My Accountant are flagged as suspense.

## Opening balances
All ten opening balances tie to the prior closing trial balance with no difference.

## Categorization rules
The rulebook keeps the client's rules, holds checks at 0.60 confidence so they are
reviewed, and sends peer-to-peer payments to the exceptions queue.

## Open questions
- What was the 300.00 Venmo payment to J Marsh on 2026-08-20 (GL-0012, account 6999)?

{_disclaimer(spec)}
"""


def _close_package(spec):
    return f"""# August 2026 close package: Harbor Lane Coffee Roasters LLC

## Summary
Operating checking reconciles with 0.00 unexplained difference, the adjusted trial
balance balances, and every draft entry is marked draft - do not post.

## Reconciliations
See `bank_reconciliation.json`: outstanding check 1052 and the August 31 deposit are
timing items; the service fee and interest have a draft entry (ADJ-202608-BANK).

## Adjusting entries
Schedule entries ADJ-202608-PRE-001, ADJ-202608-DEP-001, ADJ-202608-DEF-001 and
ADJ-202608-ACR-001, plus ADJ-202608-BANK. None is posted.

## Flux commentary
Every flagged movement in `flux.csv` is explained from August activity and schedules.

## Financial statements
`financial_statements.json` is built from the adjusted trial balance; the balance
sheet balances.

## Open items
The Venmo payment in account 6999 waits for the owner's answer.

## Reviewer sign-off
The accountant or CPA approves each entry, posts it and locks August.

{_disclaimer(spec)}
"""


BANK_ENTRY = {"entry_id": "ADJ-202608-BANK", "date": "2026-08-31",
              "description": "Record August bank service fee and interest per statement",
              "support": "bank_reconciliation.json unrecorded rows 17-18",
              "lines": [{"account": "6400", "debit": "25.00"}, {"account": "1000", "credit": "25.00"},
                        {"account": "1000", "debit": "3.18"}, {"account": "4900", "credit": "3.18"}]}

M1_FILES = [f"{D1}/diagnostic.md", f"{D1}/account_map.csv", f"{D1}/opening_balance_tieout.csv",
            f"{D1}/categorization_rulebook.csv", f"{D1}/close_checklist.md"]
M2_FILES = [f"{D2}/{n}" for n in ("categorized.csv", "exceptions.csv", "bank_reconciliation.json",
                                  "accrual_schedule.csv", "journal_entries.csv", "trial_balance.csv")]
M3_FILES = [f"{D3}/flux.csv", f"{D3}/financial_statements.json", f"{D3}/close_package.md"]


def _m1_plan(spec):
    return [
        [("read_file", {"path": "inputs/close_parameters.json"}), ("list_files", {"path": "inputs"})],
        [("build_account_map", {}), ("tie_opening_balances", {"period": "2026-08"})],
        [("write_file", {"path": f"{D1}/categorization_rulebook.csv", "content": RULEBOOK}),
         ("write_file", {"path": f"{D1}/close_checklist.md", "content": CHECKLIST}),
         ("write_file", {"path": f"{D1}/diagnostic.md", "content": _diagnostic(spec)})],
        ("submit_milestone", {"summary": "Account map, opening tie-out, rulebook and checklist",
                              "artifacts": M1_FILES}),
    ]


def _m2_work():
    return [
        ("parse_bank_statement", {}),
        ("categorize_transactions", {}),
        ("find_exceptions", {}),
        ("reconcile_bank", {"cash_account": "1000", "period": "2026-08"}),
        ("build_accrual_schedule", {"period": "2026-08"}),
        ("draft_journal_entry", BANK_ENTRY),
        ("build_trial_balance", {}),
    ]


M2_SUBMIT = ("submit_milestone", {"summary": "August categorized, reconciled and adjusted TB built",
                                  "artifacts": M2_FILES})


def _flux_with_commentary(ws):
    rows = _rows(ws / D3 / "flux.csv")
    for r in rows:
        if r["flagged"] == "yes":
            r["commentary"] = f"Movement of {r['change']} traced to August activity and schedule entries"
    return ("write_file", {"path": f"{D3}/flux.csv", "content": _csv_text(rows)})


def _m3_plan(spec, ws):
    return [
        ("flux_analysis", {}),
        ("read_file", {"path": f"{D3}/flux.csv"}),
        lambda: _flux_with_commentary(ws),
        ("build_financial_statements", {}),
        ("write_file", {"path": f"{D3}/close_package.md", "content": _close_package(spec)}),
        ("submit_milestone", {"summary": "Flux, statements and close package", "artifacts": M3_FILES}),
    ]


def _score_all(rubric):
    criteria = load_rubric(PACK / rubric)
    call = ToolCall("grader_1", "score_rubric", {"scores": [
        {"id": c["id"], "score": 0.9, "rationale": "met"} for c in criteria]})
    return ScriptedAdapter([ModelResponse(text="", tool_calls=[call], stop_reason="tool_use",
                                          usage=Usage(), model="grader")])


# --- wiring -----------------------------------------------------------------------------

def test_registry_loads_the_subclass_with_every_domain_tool_and_check(spec):
    assert isinstance(spec, BookkeepingClose)
    names = spec.tool_registry().names()
    assert names == spec.manifest.tools
    assert {d["name"] for d in TOOL_DEFS} <= set(names)
    checks = spec.check_registry()
    assert all(name in checks for name in CHECK_DEFS)
    assert spec.validate() == []


def test_cli_lists_shows_and_scopes_the_specialist(tmp_path):
    intake = tmp_path / "intake.json"
    brief = json.loads((PACK / "evals/cases/harbor_lane_august_close.json").read_text(encoding="utf-8"))
    intake.write_text(json.dumps(brief["brief"]["intake"]), encoding="utf-8")
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({**brief["brief"]["intake"], "period": "August 2026"}), encoding="utf-8")

    def run(*argv):
        out = io.StringIO()
        return main(list(argv), stdout=out, env={}), out.getvalue()

    code, out = run("list")
    assert code == 0 and "bookkeeping-close" in out and "human-gated" in out
    assert run("validate", "bookkeeping-close") == (0, "[]\n")
    code, out = run("show", "bookkeeping-close")
    assert code == 0 and json.loads(out)["human_review"]["reviewer_role"] == "accountant or CPA"
    code, out = run("milestones", "bookkeeping-close")
    assert code == 0 and [m["id"] for m in json.loads(out)] == [M1, M2, M3]
    code, out = run("estimate", "bookkeeping-close", "--intake", str(intake))
    assert code == 0 and json.loads(out)["hours_high"] == 24.0
    assert run("validate-intake", "bookkeeping-close", "--intake", str(intake)) == (0, "[]\n")
    code, out = run("validate-intake", "bookkeeping-close", "--intake", str(bad))
    assert code == 1 and json.loads(out)[0]["field"] == "period"


def test_materiality_answer_becomes_flux_thresholds():
    assert materiality_thresholds("1000 and 10%") == {"flux_threshold_abs": "1000", "flux_threshold_pct": "10"}
    assert materiality_thresholds("$2,500 / 5 percent") == {"flux_threshold_abs": "2500",
                                                            "flux_threshold_pct": "5"}
    assert materiality_thresholds("use your default") == {}
    assert materiality_thresholds("1k") == {}
    assert materiality_thresholds(None) == {} and materiality_thresholds(True) == {}


# --- milestones -------------------------------------------------------------------------

def test_m1_onboarding_ready_for_review(spec, tmp_path):
    ws = _workspace(spec, tmp_path, "harbor_lane_onboarding")
    params_before = (ws / "inputs/close_parameters.json").read_bytes()
    sub, adapter, events = _run(spec, ws, _brief(spec, "harbor_lane_onboarding"), M1, _m1_plan(spec),
                                grader=_score_all("rubrics/diagnostic.yaml"))

    assert not _errors(events), _errors(events)
    _assert_submission(spec, ws, sub, M1, M1_FILES)
    results = {r.check: r for r in sub.check_results}
    assert results["account_map_complete"].passed is True
    assert results["opening_balances_tie"].passed is True
    assert results["rubric_grader"].passed is True and results["rubric_grader"].score == pytest.approx(0.9)
    # the model was told who reviews and which disclaimer goes where
    system = adapter.calls[0]["system"]
    assert "accountant or CPA" in system and _disclaimer(spec) in system
    # the client's own close_parameters.json is left as it was
    assert (ws / "inputs/close_parameters.json").read_bytes() == params_before
    assert not events.of_type("close_parameters")
    # tool outputs are agent-authored, so they can never be ledger sources
    ledger = Ledger(ws)
    assert ledger.is_authored(f"{D1}/account_map.csv") and ledger.is_authored(f"{D1}/opening_balance_tieout.csv")


def test_m1_writes_close_parameters_from_the_intake_when_missing(spec, tmp_path):
    ws = _workspace(spec, tmp_path, "harbor_lane_onboarding")
    (ws / "inputs/close_parameters.json").unlink()
    sub, _, events = _run(spec, ws, _brief(spec, "harbor_lane_onboarding"), M1, _m1_plan(spec))

    params = json.loads((ws / "inputs/close_parameters.json").read_text(encoding="utf-8"))
    assert params == {"entity_name": "Harbor Lane Coffee Roasters LLC", "period": "2026-08",
                      "cash_account": "1000", "statement_ending_balance": "36945.58",
                      "flux_threshold_abs": "1000", "flux_threshold_pct": "10"}
    assert events.of_type("close_parameters")[0].data["path"] == "inputs/close_parameters.json"
    # opening_balances_tie keys off the period in that file
    _assert_submission(spec, ws, sub, M1, M1_FILES)
    assert next(r for r in sub.check_results if r.check == "rubric_grader").passed is None   # no grader


def test_m2_and_m3_close_the_period(spec, tmp_path):
    ws = _workspace(spec, tmp_path)
    sub2, _, events = _run(spec, ws, _brief(spec, "harbor_lane_august_close"), M2, [*_m2_work(), M2_SUBMIT])

    assert not _errors(events), _errors(events)
    _assert_submission(spec, ws, sub2, M2, M2_FILES)
    assert all(r.passed is True for r in sub2.check_results if r.check in CHECK_DEFS)
    # the tools really ran: the eval case's ground truth
    rec = json.loads((ws / D2 / "bank_reconciliation.json").read_text(encoding="utf-8"))
    assert rec["adjusted_bank_balance"] == rec["adjusted_book_balance"] == "36789.18"
    assert rec["unexplained_difference"] == "0.00" and rec["matched_count"] == 16
    tb = _rows(ws / D2 / "trial_balance.csv")
    assert sum(float(r["debit"] or 0) for r in tb) == pytest.approx(115861.78)
    journal = _rows(ws / D2 / "journal_entries.csv")
    assert {r["entry_id"] for r in journal} == {"ADJ-202608-PRE-001", "ADJ-202608-DEP-001", "ADJ-202608-DEF-001",
                                                "ADJ-202608-ACR-001", "ADJ-202608-BANK"}
    assert all(r["status"] == DRAFT_STATUS for r in journal)       # nothing is ever posted
    assert any("VENMO" in r["description"] for r in _rows(ws / D2 / "exceptions.csv"))

    sub3, _, events = _run(spec, ws, _brief(spec, "harbor_lane_close_package"), M3, _m3_plan(spec, ws))
    assert not _errors(events), _errors(events)
    _assert_submission(spec, ws, sub3, M3, M3_FILES)
    results = {r.check: r for r in sub3.check_results}
    assert results["flux_commentary_complete"].passed is True
    assert results["financial_statements_tie"].passed is True
    assert results["rubric_grader"].passed is None                    # no grader configured
    statements = json.loads((ws / D3 / "financial_statements.json").read_text(encoding="utf-8"))
    assert statements["income_statement"]["net_income"] == "-13850.82"
    assert statements["balance_sheet"]["total_assets"] == "71189.18"
    assert statements["balance_sheet"]["difference"] == "0.00"
    assert sub3.evidence_hash != sub2.evidence_hash


# --- negative paths ---------------------------------------------------------------------

def test_forged_reconciliation_and_refused_plugs_need_revision(spec, tmp_path):
    ws = _workspace(spec, tmp_path)
    forged = {"account": "1000", "period_start": "2026-08-01", "period_end": "2026-08-31",
              "statement_ending_balance": "36945.58", "gl_ending_balance": "36945.58",
              "outstanding_items": [], "unrecorded_items": [], "total_outstanding": "0.00",
              "total_unrecorded": "0.00", "adjusted_bank_balance": "36945.58",
              "adjusted_book_balance": "36945.58", "unexplained_difference": "0.00"}
    plug = {**BANK_ENTRY, "entry_id": "ADJ-X", "description": "Plug to balance the bank"}
    suspense = {**BANK_ENTRY, "entry_id": "ADJ-Y", "description": "Park the Venmo payment",
                "lines": [{"account": "1999", "debit": "300.00"}, {"account": "6999", "credit": "300.00"}]}
    plan = [*_m2_work(),
            [("draft_journal_entry", plug), ("draft_journal_entry", suspense)],
            ("write_file", {"path": f"{D2}/bank_reconciliation.json", "content": json.dumps(forged)}),
            M2_SUBMIT]
    sub, _, events = _run(spec, ws, _brief(spec, "harbor_lane_august_close"), M2, plan)

    refused = _errors(events)
    assert [r["name"] for r in refused] == ["draft_journal_entry", "draft_journal_entry"]
    assert "plug" in refused[0]["content"] and "suspense" in refused[1]["content"]
    assert {r["entry_id"] for r in _rows(ws / D2 / "journal_entries.csv")}.isdisjoint({"ADJ-X", "ADJ-Y"})
    results = {r.check: r for r in sub.check_results}
    assert results["bank_rec_ties"].passed is False
    assert "gl_ending_balance" in results["bank_rec_ties"].details
    assert results["no_plugs"].passed is True and results["trial_balance_ties"].passed is True
    assert sub.status == "needs_revision"
    assert sub.human_review.required is True and sub.evidence_hash.startswith("0x")


def test_tampered_trial_balance_fails_on_recheck(spec, tmp_path):
    ws = _workspace(spec, tmp_path)
    sub, _, _ = _run(spec, ws, _brief(spec, "harbor_lane_august_close"), M2, [*_m2_work(), M2_SUBMIT])
    assert sub.status == "ready_for_review"
    path = ws / D2 / "trial_balance.csv"
    rows = _rows(path)
    next(r for r in rows if r["account"] == "1000")["debit"] = "36945.58"   # the bank figure, not the books
    path.write_text(_csv_text(rows), encoding="utf-8")
    results = spec.check(ws, M2)
    assert next(r for r in results if r.check == "trial_balance_ties").passed is False
    assert Specialist.status_for(RunOutcome(status="submitted"), results) == "needs_revision"


def test_domain_tools_cannot_escape_or_touch_inputs_and_kit_files(spec, tmp_path):
    ws = _workspace(spec, tmp_path)
    before = {p: p.read_bytes() for p in (ws / "inputs").rglob("*") if p.is_file()}
    plan = [
        [("parse_bank_statement", {"path": "../outside.csv"}),
         ("parse_bank_statement", {"path": ".agentkit/ledger.json"}),
         ("categorize_transactions", {"output": "inputs/bank_statement.csv"}),
         ("reconcile_bank", {"cash_account": "1000", "period": "2026-08", "output": ".agentkit/rec.json"}),
         ("build_trial_balance", {"output": "../trial_balance.csv"})],
        *_m2_work(), M2_SUBMIT,
    ]
    sub, _, events = _run(spec, ws, _brief(spec, "harbor_lane_august_close"), M2, plan)

    refused = _errors(events)
    assert len(refused) == 5
    assert "deliverables/" in refused[2]["content"] and "internal to the kit" in refused[3]["content"]
    assert len(events.of_type("policy_denied")) == 2                 # the two reads, via the kit's gate
    assert {p: p.read_bytes() for p in (ws / "inputs").rglob("*") if p.is_file()} == before
    assert not (ws / ".agentkit/rec.json").exists() and not (tmp_path / "trial_balance.csv").exists()
    assert not Ledger(ws).is_authored("inputs/bank_statement.csv")
    assert sub.status == "ready_for_review"                            # the honest work that followed
