"""
specialists/bookkeeping_close/checks.py - acceptance checks for the close.

Each check recomputes from the customer's source files in inputs/ with the
same Decimal engine the tools use, then compares against what the agent
delivered. A forged or hand-edited number fails; nothing reported by the
agent is taken on trust. Checks are plain functions
`fn(workspace, params, *, run=None) -> {"passed", "details", "score"}`
listed in CHECK_DEFS; agent.py wraps them for the kit.

Period, cash account, matching window and flux thresholds come from params
first, then from inputs/close_parameters.json (written from the intake),
never from a deliverable.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from agentkit.errors import ToolError
from specialists.bookkeeping_close import tools as eng
from specialists.bookkeeping_close.tools import CLOSE_PARAMETERS

DEFAULT_MIN_CONFIDENCE = 0.8
STATEMENT = "inputs/bank_statement.csv"
GL = "inputs/gl_detail.csv"
# Commentary like "n/a" or "see GL" explains nothing.
EMPTY_COMMENTARY = {"", "n/a", "na", "tbd", "todo", "none", "-", "see gl", "variance", "change"}

__all__ = ["CHECK_DEFS", "CLOSE_PARAMETERS"]


def _result(passed: bool | None, details: str, score: float | None = None) -> dict:
    return {"passed": passed, "details": details, "score": score}


def _guarded(fn: Callable[[Path, dict], dict]) -> Callable[..., dict]:
    """Missing or malformed files fail the check instead of raising."""
    def check(workspace: Path, params: dict, *, run=None) -> dict:
        try:
            return fn(Path(workspace), dict(params or {}))
        except (ToolError, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            return _result(False, f"{type(exc).__name__}: {exc}", 0.0)
    check.__name__ = fn.__name__
    check.__doc__ = fn.__doc__
    return check


def _setting(workspace: Path, params: dict, key: str, default: Any = None) -> Any:
    if params.get(key) not in (None, ""):
        return params[key]
    path = eng.resolve(workspace, params.get("close_parameters", CLOSE_PARAMETERS))
    if path.is_file():
        value = json.loads(path.read_text(encoding="utf-8-sig")).get(key)
        if value not in (None, ""):
            return value
    return default


def _period_end(workspace: Path, params: dict):
    period = _setting(workspace, params, "period")
    return eng.period_bounds(period)[1] if period else None


def _reconcile(workspace: Path, params: dict, statement: dict, gl: list[eng.GLLine],
               journal: list[dict[str, Any]] = (), rec: dict | None = None) -> dict:
    """The reconciliation recomputed from inputs, optionally with the drafts
    posted; the reported one supplies the account and period only when the
    close parameters do not."""
    rec = rec or {}
    cash = str(_setting(workspace, params, "cash_account", rec.get("account", "")))
    period = _setting(workspace, params, "period")
    if not cash or not (period or rec.get("period_start")):
        raise ToolError(f"the cash account and period must be set in {CLOSE_PARAMETERS}")
    start, end = eng.period_bounds(period) if period else (eng.parse_date(rec["period_start"]),
                                                          eng.parse_date(rec["period_end"]))
    window = int(_setting(workspace, params, "date_window", eng.DEFAULT_DATE_WINDOW))
    prior = eng.load_prior_outstanding(workspace, params.get("prior_reconciliation", eng.PRIOR_RECONCILIATION))
    return eng.reconcile(statement, gl + eng.journal_as_gl(list(journal)), cash, period_start=start,
                         period_end=end, date_window=window, prior_outstanding=prior)


# --- checks ---------------------------------------------------------------------

@_guarded
def bank_rec_ties(workspace: Path, params: dict) -> dict:
    """Recompute the bank reconciliation from the statement, the GL and the
    prior reconciliation's outstanding items; the reported figures must
    match and the unexplained difference must be 0.00. With journal_entries,
    the drafts must clear every unrecorded bank item, and a drafted cash line
    may not leave anything newly outstanding (that would duplicate a
    recorded receipt or payment)."""
    rec = json.loads(eng.resolve(workspace, params["reconciliation"]).read_text(encoding="utf-8"))
    statement = eng.load_statement(workspace, params.get("statement", STATEMENT))
    gl = eng.load_gl(workspace, params.get("gl", GL))
    fresh = _reconcile(workspace, params, statement, gl, rec=rec)
    cash = fresh["account"]
    problems = []
    if statement["breaks"]:
        problems.append("statement running balance breaks: " + "; ".join(statement["breaks"]))
    stated = _setting(workspace, params, "statement_ending_balance")
    if stated is not None and eng.money(stated) != statement["ending"]:
        problems.append(f"CSV ending balance {eng.fmt(statement['ending'])} differs from the stated "
                        f"statement balance {eng.fmt(eng.money(stated))}")
    if str(rec.get("account")) != cash:
        problems.append(f"reconciliation is for account {rec.get('account')!r}, expected {cash}")
    for key in ("statement_ending_balance", "gl_opening_balance", "prior_outstanding_total", "opening_difference",
                "gl_ending_balance", "total_outstanding", "total_unrecorded", "adjusted_bank_balance",
                "adjusted_book_balance", "unexplained_difference"):
        try:
            reported = eng.money(rec.get(key))
        except ToolError:
            reported = None
        if reported != eng.money(fresh[key]):
            problems.append(f"{key}: reported {rec.get(key)!r}, recomputed {fresh[key]}")
    for key in ("outstanding_items", "unrecorded_items"):
        want = Counter(i["amount"] for i in fresh[key])
        got = Counter(eng.fmt(eng.money(i.get("amount"))) for i in rec.get(key, []) if isinstance(i, dict))
        if want != got:
            problems.append(f"{key} amounts differ from recomputation: reported {sorted(got.elements())}, "
                            f"recomputed {sorted(want.elements())}")
    if eng.money(fresh["unexplained_difference"]) != 0:
        problems.append(f"unexplained difference {fresh['unexplained_difference']} (must be 0.00; opening "
                        f"difference {fresh['opening_difference']})")
    if params.get("journal_entries"):
        journal = eng.load_journal(workspace, params["journal_entries"])
        after = _reconcile(workspace, params, statement, gl, journal, rec=rec)
        if after["unrecorded_items"]:
            problems.append("bank items with no draft entry: " + ", ".join(
                f"{i['date']} {i['description']} {i['amount']}" for i in after["unrecorded_items"]))
        if eng.money(after["unexplained_difference"]) != 0:
            problems.append(f"after draft entries the difference is {after['unexplained_difference']}")
        drafted = {r["entry_id"] for r in journal}
        voidable = Counter(eng.fmt(-eng.money(i["amount"])) for i in fresh["outstanding_items"])
        before = Counter((i["entry_id"], i["amount"]) for i in fresh["outstanding_items"])
        added = Counter((i["entry_id"], i["amount"]) for i in after["outstanding_items"]) - before
        for (entry_id, amount) in added.elements():
            if entry_id in drafted and voidable[amount] > 0:
                voidable[amount] -= 1                # voids an outstanding item; the reviewer approves it
                continue
            problems.append(f"after draft entries {entry_id} {amount} is left outstanding: a drafted cash line "
                            "must clear a bank-only item, not duplicate a recorded receipt or payment")
    if problems:
        return _result(False, "; ".join(problems), 0.0)
    return _result(True, f"account {cash}: adjusted bank = adjusted book = {fresh['adjusted_book_balance']}, "
                         f"{fresh['matched_count']} matched, {len(fresh['outstanding_items'])} outstanding, "
                         f"{len(fresh['unrecorded_items'])} unrecorded", 1.0)


@_guarded
def categorization_complete(workspace: Path, params: dict) -> dict:
    """Every bank line is categorized exactly once (count and dollars equal
    the feed) to an account in the chart; a line that cites a rule agrees
    with that rule; a line no rule covers carries the agent's own rationale;
    and every unknown, suspense or low-confidence line is in the exceptions queue."""
    statement = eng.load_statement(workspace, params.get("statement", STATEMENT))
    rows = eng.read_rows(workspace, params["categorized"], ["date", "description", "amount", "account",
                                                            "confidence", "rationale"])
    min_conf = float(params.get("min_confidence", DEFAULT_MIN_CONFIDENCE))
    coa = eng.load_coa(workspace, params["chart_of_accounts"]) if params.get("chart_of_accounts") else None
    suspense = eng.suspense_accounts(coa) if coa else set()
    rules: dict[str, list[dict[str, Any]]] = {}     # pattern -> the client's rule and the m1 rulebook's
    for rel in eng.as_list(params.get("rules")):
        if eng.exists(workspace, rel):
            for rule in eng.load_rules(workspace, rel):
                rules.setdefault(rule["pattern"], []).append(rule)

    def key(day, desc, amount):
        return (eng.parse_date(day).isoformat(), desc.strip().lower(), eng.fmt(eng.money(amount)))

    def confidence(r) -> float:
        try:
            return float(r["confidence"] or 0)
        except ValueError:
            return 0.0

    feed = Counter(key(ln.date.isoformat(), ln.description, ln.amount) for ln in statement["lines"])
    got = Counter(key(r["date"], r["description"], r["amount"]) for r in rows)
    problems = []
    if feed != got:
        missing, extra = feed - got, got - feed
        problems.append(f"categorized rows differ from the feed: {sum(missing.values())} missing, "
                        f"{sum(extra.values())} not in the feed")
    total = sum((eng.money(r["amount"]) for r in rows), eng.ZERO)
    if len(rows) != len(statement["lines"]) or total != statement["net"]:
        problems.append(f"count/dollars {len(rows)}/{eng.fmt(total)} vs feed "
                        f"{len(statement['lines'])}/{eng.fmt(statement['net'])}")
    review = []
    for r in rows:
        label = f"{r['date']} {r['description']} {r['amount']}"
        if r["account"] and coa is not None and r["account"] not in coa:
            problems.append(f"{label}: account {r['account']} is not in the chart of accounts")
        cited = re.fullmatch(r"matched rule /(.*)/", r["rationale"])
        if cited:
            candidates = rules.get(cited[1], [])
            if not candidates:
                problems.append(f"{label}: cites rule /{cited[1]}/, which is not in the rulebook")
            elif not any(rule["regex"].search(r["description"]) and rule["account"] == r["account"]
                         and abs(rule["confidence"] - confidence(r)) <= 0.005 for rule in candidates):
                rule = candidates[0]
                problems.append(f"{label}: rule /{cited[1]}/ gives account {rule['account'] or '(none)'} at "
                                f"{rule['confidence']:.2f}, not {r['account'] or '(none)'} at {r['confidence']}")
        own = not cited and r["rationale"] != "no rule matched" and len(r["rationale"].split()) >= 3
        if (not r["account"] or r["account"] in suspense or confidence(r) < min_conf
                or r.get("needs_review", "").lower() == "yes" or not (cited or own)):
            review.append(r)
    if review:
        queued: Counter = Counter()
        if params.get("exceptions"):
            queued = Counter(key(q["date"], q["description"], q["amount"]) for q in eng.read_rows(
                workspace, params["exceptions"], ["date", "description", "amount"]))
        unqueued = [r for r in review if not queued[key(r["date"], r["description"], r["amount"])]]
        if unqueued:
            problems.append("not decided and not in the exceptions queue: " + ", ".join(
                f"{r['date']} {r['description']} {r['amount']}" for r in unqueued))
    if problems:
        return _result(False, "; ".join(problems), 0.0)
    return _result(True, f"{len(rows)} rows totalling {eng.fmt(total)} match the feed; "
                         f"{len(review)} queued for review", 1.0)


@_guarded
def journal_entries_balanced(workspace: Path, params: dict) -> dict:
    """Every draft entry balances, has two or more one-sided lines, cites
    support, is dated in the period, uses chart-of-accounts accounts (never
    suspense, except a reclass out of it the client explained), is marked
    draft and does not read like a plug. An empty journal passes: other
    checks require the entries a period needs."""
    rel = params["journal_entries"]
    if not eng.exists(workspace, rel):
        raise ToolError(f"file not found: {rel}")
    journal = eng.load_journal(workspace, rel)
    if not journal:
        return _result(True, f"no draft entries in {rel}", 1.0)
    coa = eng.load_coa(workspace, params["chart_of_accounts"]) if params.get("chart_of_accounts") else None
    period = _setting(workspace, params, "period")
    start, end = eng.period_bounds(period) if period else (None, None)
    entries: dict[str, list[dict[str, Any]]] = {}
    for r in journal:
        entries.setdefault(r["entry_id"], []).append(r)
    problems: dict[str, list[str]] = {}
    for entry_id, lines in entries.items():
        issues = problems.setdefault(entry_id, [])
        debits = sum((ln["debit_d"] for ln in lines), eng.ZERO)
        credits = sum((ln["credit_d"] for ln in lines), eng.ZERO)
        if len(lines) < 2 or debits != credits or debits == 0:
            issues.append(f"debits {eng.fmt(debits)} vs credits {eng.fmt(credits)}")
        for ln in lines:
            if ln["debit_d"] < 0 or ln["credit_d"] < 0 or bool(ln["debit_d"]) == bool(ln["credit_d"]):
                issues.append(f"line {ln['account']} needs exactly one positive side")
            if not ln.get("support", "").strip():
                issues.append(f"line {ln['account']} has no support")
            if "draft" not in ln.get("status", "").lower():
                issues.append(f"line {ln['account']} is not marked draft")
            if eng.PLUG_WORDS_RE.search(ln.get("description", "")):
                issues.append(f"line {ln['account']} reads like a plug")
            if coa is not None and ln["account"] not in coa:
                issues.append(f"unknown account {ln['account']}")
        if len({ln["date"] for ln in lines}) > 1:
            issues.append("lines carry different dates")
        if start and not all(start <= ln["date_d"] <= end for ln in lines):
            issues.append(f"dated outside the period {period}")
    if coa is not None:
        balances = eng.balances_from(eng.load_gl(workspace, params.get("gl", GL)), as_of=end)
        for ids, problem in eng.suspense_problems(balances, journal, eng.suspense_accounts(coa)):
            for entry_id in ids:
                problems[entry_id].append(problem)
    bad = {k: v for k, v in problems.items() if v}
    score = 1 - len(bad) / len(entries)
    if bad:
        return _result(False, "; ".join(f"{k}: {', '.join(v)}" for k, v in bad.items()), score)
    return _result(True, f"{len(entries)} draft entries balance and cite support", 1.0)


@_guarded
def schedule_entries_tie(workspace: Path, params: dict) -> dict:
    """Recompute the schedule roll-forward from the client's schedule: the
    delivered accrual schedule must match it, and every item with a
    this-period amount needs exactly one draft entry that cites it with the
    right accounts and amount (none when nothing is due)."""
    _, end = eng.period_bounds(_setting(workspace, params, "period"))
    source = params.get("schedule", "inputs/accrual_schedule.csv")
    expected = eng.schedule_rows(workspace, source, end) if eng.exists(workspace, source) else []
    delivered = {r["item_id"]: r for r in eng.read_rows(
        workspace, params["accrual_schedule"],
        ["item_id", "due_through_period", "booked_to_date", "this_period", "remaining_after"])}
    want = {r["item_id"]: r for r in expected}
    problems = []
    for item in sorted(set(want) | set(delivered)):
        if item not in delivered:
            problems.append(f"{item} is missing from the delivered schedule")
        elif item not in want:
            problems.append(f"{item} is not in {source}")
        else:
            for col in ("due_through_period", "booked_to_date", "this_period", "remaining_after"):
                if eng.money(delivered[item][col]) != eng.money(want[item][col]):
                    problems.append(f"{item} {col}: reported {delivered[item][col]}, recomputed {want[item][col]}")
    entries: dict[str, list[dict[str, Any]]] = {}
    for r in eng.load_journal(workspace, params["journal_entries"]):
        entries.setdefault(r["entry_id"], []).append(r)
    for row in expected:
        item, spec = row["item_id"], eng.schedule_entry(row)
        citing = [eid for eid, lines in entries.items() if any(eng.cites(ln.get("support", ""), item) for ln in lines)]
        if spec is None:
            if citing:
                problems.append(f"{item}: nothing is due this period, but {', '.join(citing)} cite it")
            continue
        dr, cr, amount = spec
        expect = f"Dr {dr} / Cr {cr} {eng.fmt(amount)}"
        if len(citing) != 1:
            problems.append(f"{item}: expected one draft entry citing it ({expect}), found {len(citing)}")
        for eid in citing:
            net: dict[str, Decimal] = {}
            for ln in entries[eid]:
                net[ln["account"]] = net.get(ln["account"], eng.ZERO) + ln["debit_d"] - ln["credit_d"]
            if {a: v for a, v in net.items() if v != 0} != {dr: amount, cr: -amount}:
                problems.append(f"{eid} for {item} does not match the schedule: expected {expect}")
    if problems:
        return _result(False, "; ".join(problems), 1 - min(len(problems), max(len(want), 1)) / max(len(want), 1))
    due = sum(1 for r in expected if eng.schedule_entry(r))
    return _result(True, f"{len(expected)} schedule items recompute; {due} have the draft entry they need", 1.0)


@_guarded
def trial_balance_ties(workspace: Path, params: dict) -> dict:
    """Recompute every account balance from the GL plus draft entries as of
    period end; the delivered trial balance must match to the cent and
    debits must equal credits. With statement, its cash balance must equal
    the reconciled bank balance (statement ending plus outstanding items)."""
    gl = eng.load_gl(workspace, params.get("gl", GL))
    journal = eng.load_journal(workspace, params.get("journal_entries"))
    as_of = eng.parse_date(params["as_of"]) if params.get("as_of") else _period_end(workspace, params)
    fresh = {a: b for a, b in eng.balances_from(gl, journal, as_of=as_of).items() if b != 0}
    rows = eng.read_rows(workspace, params["trial_balance"], ["account", "debit", "credit"])
    reported: dict[str, Decimal] = {}
    for r in rows:
        reported[r["account"]] = reported.get(r["account"], eng.ZERO) + eng.money(r["debit"]) - eng.money(r["credit"])
    debits = sum((eng.money(r["debit"]) for r in rows), eng.ZERO)
    credits = sum((eng.money(r["credit"]) for r in rows), eng.ZERO)
    problems = []
    if debits != credits:
        problems.append(f"debits {eng.fmt(debits)} != credits {eng.fmt(credits)}")
    unbalanced = eng.unbalanced_entries([(g.entry_id, g.debit, g.credit) for g in gl] +
                                        [(r["entry_id"], r["debit_d"], r["credit_d"]) for r in journal])
    if unbalanced:
        problems.append("unbalanced source entries: " + ", ".join(sorted(unbalanced)))
    mismatched = [f"{a}: reported {eng.fmt(reported.get(a, eng.ZERO))}, recomputed {eng.fmt(fresh.get(a, eng.ZERO))}"
                  for a in sorted(set(fresh) | set(reported)) if reported.get(a, eng.ZERO) != fresh.get(a, eng.ZERO)]
    problems += mismatched
    if params.get("statement"):
        after = _reconcile(workspace, params, eng.load_statement(workspace, params["statement"]), gl, journal)
        cash = after["account"]
        if reported.get(cash, eng.ZERO) != eng.money(after["adjusted_bank_balance"]):
            problems.append(f"cash {cash} is {eng.fmt(reported.get(cash, eng.ZERO))} in the trial balance but the "
                            f"reconciled bank balance is {after['adjusted_bank_balance']}")
    total = max(len(set(fresh) | set(reported)), 1)
    if problems:
        return _result(False, "; ".join(problems), 1 - len(mismatched) / total)
    as_of_text = f" as of {as_of}" if as_of else ""
    return _result(True, f"{len(fresh)} accounts tie{as_of_text}; debits = credits = {eng.fmt(debits)}", 1.0)


@_guarded
def no_plugs(workspace: Path, params: dict) -> dict:
    """Suspense / uncategorized / Ask My Accountant balances at period end are
    zero or fully itemized in the exceptions queue; no draft entry touches
    them (except a reclass out that the client explained) or reads like a
    forcing entry; the reconciliation has no unexplained difference."""
    coa = eng.load_coa(workspace, params["chart_of_accounts"])
    suspense = eng.suspense_accounts(coa)
    gl = eng.load_gl(workspace, params.get("gl", GL))
    journal = eng.load_journal(workspace, params.get("journal_entries"))
    end = _period_end(workspace, params)
    balances = eng.balances_from(gl, journal, as_of=end)
    queue = (eng.read_rows(workspace, params["exceptions"], ["account", "amount"])
             if params.get("exceptions") else [])
    problems = []
    for account in sorted(suspense):
        balance = balances.get(account, eng.ZERO)
        itemized = sum((eng.money(q["amount"]) for q in queue
                        if q["account"] == account and q.get("source", "gl") == "gl"
                        and not (end and q.get("date") and eng.parse_date(q["date"]) > end)), eng.ZERO)
        if balance != 0 and itemized != balance:
            problems.append(f"suspense account {account} ({coa[account]['name']}) holds {eng.fmt(balance)} "
                            f"but only {eng.fmt(itemized)} is itemized in the exceptions queue")
    problems += [f"draft entries {', '.join(ids)} {problem}"
                 for ids, problem in eng.suspense_problems(eng.balances_from(gl, as_of=end), journal, suspense)]
    for r in journal:
        if eng.PLUG_WORDS_RE.search(r.get("description", "")):
            problems.append(f"draft entry {r['entry_id']} reads like a plug: {r['description']!r}")
    if params.get("reconciliation"):
        rec = json.loads(eng.resolve(workspace, params["reconciliation"]).read_text(encoding="utf-8"))
        if eng.money(rec.get("unexplained_difference", "")) != 0:
            problems.append(f"reconciliation leaves {rec.get('unexplained_difference')} unexplained")
        for item in rec.get("outstanding_items", []) + rec.get("unrecorded_items", []):
            if eng.PLUG_WORDS_RE.search(str(item.get("description", ""))):
                problems.append(f"reconciling item reads like a plug: {item.get('description')!r}")
    if problems:
        return _result(False, "; ".join(problems), 0.0)
    return _result(True, f"no plugs; suspense accounts {', '.join(sorted(suspense)) or 'none'} are zero "
                         f"or itemized", 1.0)


@_guarded
def flux_commentary_complete(workspace: Path, params: dict) -> dict:
    """Recompute the variances (balance-sheet balances against the prior
    closing trial balance, the month's P&L activity against the prior
    month's P&L); every flagged account must be in the flux file with the
    recomputed amounts and a real explanation."""
    threshold_abs = eng.money(_setting(workspace, params, "threshold_abs",
                                       _setting(workspace, params, "flux_threshold_abs", "1000")))
    threshold_pct = eng.percent(_setting(workspace, params, "threshold_pct",
                                         _setting(workspace, params, "flux_threshold_pct", "10")))
    rule = str(_setting(workspace, params, "rule", _setting(workspace, params, "flux_threshold_rule", "all")))
    table = eng.flux_table(workspace, current=params["current"],
                           prior_trial_balance=params.get("prior_trial_balance", "inputs/prior_trial_balance.csv"),
                           prior_pl=params.get("prior_pl"), gl=params.get("gl", GL),
                           journal_entries=params.get("journal_entries"),
                           chart_of_accounts=params["chart_of_accounts"], period=_setting(workspace, params, "period"),
                           threshold_abs=threshold_abs, threshold_pct=threshold_pct, rule=rule.lower())
    fresh = {r["account"]: r for r in table["rows"]}
    rows = {r["account"]: r for r in eng.read_rows(workspace, params["flux"],
                                                   ["account", "prior", "current", "change", "commentary"])}
    flagged = [a for a, r in fresh.items() if r["flagged"] == "yes"]
    problems = []
    for account, r in rows.items():
        want = fresh.get(account)
        if want is None:
            problems.append(f"{account} is not in the recomputed balances")
            continue
        for col in ("prior", "current", "change"):
            if eng.money(r[col]) != eng.money(want[col]):
                problems.append(f"{account} {col}: reported {r[col]}, recomputed {want[col]}")
    explained = 0
    for account in flagged:
        text = rows.get(account, {}).get("commentary", "").strip()
        if text.lower() in EMPTY_COMMENTARY or len(text.split()) < 4:
            problems.append(f"{account} changed {fresh[account]['change']} with no real commentary")
        else:
            explained += 1
    score = explained / len(flagged) if flagged else 1.0
    notes = "".join(f"; {n}" for n in table["notes"])
    if problems:
        return _result(False, "; ".join(problems) + notes, score)
    return _result(True, f"{len(flagged)} flagged variances explained (thresholds {eng.fmt(threshold_abs)} "
                         f"{'or' if rule == 'any' else 'and'} {threshold_pct}%){notes}", 1.0)


@_guarded
def opening_balances_tie(workspace: Path, params: dict) -> dict:
    """Opening GL balances equal the prior closing trial balance within the
    tolerance, or each difference is explained in the tie-out."""
    start, _end = eng.period_bounds(_setting(workspace, params, "period"))
    opening = eng.balances_from(eng.load_gl(workspace, params.get("gl", GL)), before=start)
    prior = eng.load_balances(workspace, params["prior_trial_balance"])
    tol = eng.money(params.get("tolerance", "1.00"))
    rows = {r["account"]: r for r in eng.read_rows(workspace, params["tieout"],
                                                   ["account", "gl_opening", "prior_tb", "difference"])}
    problems = []
    accounts = sorted(set(opening) | set(prior))
    for account in accounts:
        gl_bal, tb_bal = opening.get(account, eng.ZERO), prior.get(account, eng.ZERO)
        row = rows.get(account)
        if row is None:
            problems.append(f"{account} missing from the tie-out")
            continue
        if (eng.money(row["gl_opening"]), eng.money(row["prior_tb"]), eng.money(row["difference"])) != \
                (gl_bal, tb_bal, gl_bal - tb_bal):
            problems.append(f"{account}: tie-out figures do not recompute "
                            f"(GL {eng.fmt(gl_bal)}, prior TB {eng.fmt(tb_bal)})")
        if abs(gl_bal - tb_bal) > tol and len(row.get("explanation", "").split()) < 4:
            problems.append(f"{account}: difference {eng.fmt(gl_bal - tb_bal)} is not explained")
    if problems:
        return _result(False, "; ".join(problems), 1 - min(len(problems), len(accounts)) / max(len(accounts), 1))
    return _result(True, f"{len(accounts)} opening balances tie within {eng.fmt(tol)} or are explained", 1.0)


@_guarded
def account_map_complete(workspace: Path, params: dict) -> dict:
    """Every account used in the GL is in the chart of accounts and mapped to
    the statement its type belongs on, with a line name."""
    coa = eng.load_coa(workspace, params["chart_of_accounts"])
    used = {g.account for g in eng.load_gl(workspace, params.get("gl", GL))}
    rows = {r["account"]: r for r in eng.read_rows(workspace, params["account_map"],
                                                   ["account", "statement", "line"])}
    problems = [f"{a} is used in the GL but missing from the chart of accounts" for a in sorted(used - set(coa))]
    for account in sorted(used & set(coa)):
        row = rows.get(account)
        expected = ("balance_sheet" if coa[account]["type"] in ("asset", "liability", "equity")
                    else "income_statement")
        if row is None:
            problems.append(f"{account} is not mapped")
        elif row["statement"] != expected or not row["line"].strip():
            problems.append(f"{account} maps to {row['statement']!r}/{row['line']!r}; expected {expected}")
    if problems:
        return _result(False, "; ".join(problems), 1 - len(problems) / max(len(used), 1))
    return _result(True, f"all {len(used)} GL accounts mapped", 1.0)


@_guarded
def financial_statements_tie(workspace: Path, params: dict) -> dict:
    """Recompute the P&L and balance sheet from the trial balance; reported
    totals must match and assets must equal liabilities plus equity."""
    balances = eng.load_balances(workspace, params["trial_balance"])
    fresh = eng.statements(balances, eng.load_coa(workspace, params["chart_of_accounts"]))
    reported = json.loads(eng.resolve(workspace, params["statements"]).read_text(encoding="utf-8"))
    problems = []
    if sum(balances.values(), eng.ZERO) != 0:
        problems.append("the trial balance does not balance")
    for section, keys in (("income_statement", ("total_revenue", "total_expenses", "net_income")),
                          ("balance_sheet", ("total_assets", "total_liabilities",
                                             "total_equity_including_net_income", "difference"))):
        for key in keys:
            got = reported.get(section, {}).get(key)
            try:
                same = eng.money(got) == eng.money(fresh[section][key])
            except ToolError:
                same = False
            if not same:
                problems.append(f"{section}.{key}: reported {got!r}, recomputed {fresh[section][key]}")
    if eng.money(fresh["balance_sheet"]["difference"]) != 0:
        problems.append(f"balance sheet is out by {fresh['balance_sheet']['difference']}")
    if problems:
        return _result(False, "; ".join(problems), 0.0)
    return _result(True, f"net income {fresh['income_statement']['net_income']}, total assets "
                         f"{fresh['balance_sheet']['total_assets']}; balance sheet balances", 1.0)


CHECK_DEFS: dict[str, Callable[..., dict]] = {
    "bank_rec_ties": bank_rec_ties,
    "categorization_complete": categorization_complete,
    "journal_entries_balanced": journal_entries_balanced,
    "schedule_entries_tie": schedule_entries_tie,
    "trial_balance_ties": trial_balance_ties,
    "no_plugs": no_plugs,
    "flux_commentary_complete": flux_commentary_complete,
    "opening_balances_tie": opening_balances_tie,
    "account_map_complete": account_map_complete,
    "financial_statements_tie": financial_statements_tie,
}
