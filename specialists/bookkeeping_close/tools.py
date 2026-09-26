"""
specialists/bookkeeping_close/tools.py - deterministic accounting engine and
domain tools for the month-end close specialist.

All arithmetic lives here, in Decimal, so the model never adds numbers: it
decides what an ambiguous transaction is, explains variances and writes the
memo. Tools are plain functions `fn(workspace, *, fetch=None, run=None,
**args) -> dict` listed in TOOL_DEFS; agent.py wraps them for the kit.

File formats (CSV with a header row; amounts may use $, commas and
parentheses for negatives):

    bank statement   date, description, amount (+ deposit / - withdrawal), balance
    GL detail        entry_id, date, account, description, debit, credit
    chart of accts   account, name, type (asset|liability|equity|revenue|expense)
    trial balance    account, name, debit, credit
    journal entries  entry_id, date, account, description, debit, credit, support, status
    rules            pattern, account, confidence

Paths are workspace-relative. Outputs may only be written under
deliverables/; inputs/ is the customer's and is never modified. Nothing here
posts to a ledger: journal entries are drafts with status "draft - do not post".
"""
from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from agentkit.errors import ToolError

CENT = Decimal("0.01")
ZERO = Decimal("0.00")
DRAFT_STATUS = "draft - do not post"
ACCOUNT_TYPES = ("asset", "liability", "equity", "revenue", "expense")
# Accounts whose balance means "someone still has to decide": they may carry
# a balance only when every item in them sits in the exceptions queue.
SUSPENSE_NAME_RE = re.compile(r"suspense|uncategori[sz]ed|ask my accountant|clearing - unknown", re.I)
# Words that give away an entry made only to force a tie.
PLUG_WORDS_RE = re.compile(r"\bplug\b|\bforce[ds]?\b|to balance|balancing entry|unreconciled difference|"
                           r"rounding adjustment|misc(ellaneous)? adjustment", re.I)

JE_COLUMNS = ["entry_id", "date", "account", "description", "debit", "credit", "support", "status"]
TB_COLUMNS = ["account", "name", "debit", "credit"]


# --- parsing helpers ------------------------------------------------------------

def money(value: Any) -> Decimal:
    """Parse "1,234.50", "$-12", "(45.00)" or "" into Decimal cents."""
    if isinstance(value, Decimal):
        return value.quantize(CENT, rounding=ROUND_HALF_UP)
    text = str(value if value is not None else "").strip().replace(",", "").replace("$", "")
    if text in ("", "-"):
        return ZERO
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    try:
        amount = Decimal(text)
    except InvalidOperation:
        raise ToolError(f"not an amount: {value!r}") from None
    if not amount.is_finite():
        raise ToolError(f"not an amount: {value!r}")
    amount = amount.quantize(CENT, rounding=ROUND_HALF_UP)
    return -amount if negative else amount


def fmt(amount: Decimal) -> str:
    return f"{amount.quantize(CENT, rounding=ROUND_HALF_UP):.2f}"


def parse_date(value: Any) -> date:
    text = str(value or "").strip()
    for pattern in ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    raise ToolError(f"not a date (use YYYY-MM-DD): {value!r}")


def resolve(workspace: Path, rel: str, *, write: bool = False) -> Path:
    """Workspace-relative path, jailed; writes only under deliverables/."""
    if not rel or not isinstance(rel, str):
        raise ToolError("a workspace-relative path is required")
    root = Path(workspace).resolve()
    path = (root / rel).resolve()
    if path != root and root not in path.parents:
        raise ToolError(f"path escapes the workspace: {rel}")
    if write and (root / "deliverables").resolve() not in path.parents:
        raise ToolError(f"outputs must be written under deliverables/: {rel}")
    return path


def read_rows(workspace: Path, rel: str, required: list[str]) -> list[dict[str, str]]:
    path = resolve(workspace, rel)
    if not path.is_file():
        raise ToolError(f"file not found: {rel}")
    text = path.read_text(encoding="utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    header = [(h or "").strip().lower() for h in (reader.fieldnames or [])]
    missing = [c for c in required if c not in header]
    if missing:
        raise ToolError(f"{rel}: missing column(s) {', '.join(missing)}")
    rows = []
    for raw in reader:
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items() if k is not None}
        if any(row.values()):
            rows.append(row)
    return rows


def write_rows(workspace: Path, rel: str, columns: list[str], rows: list[dict[str, Any]]) -> Path:
    path = resolve(workspace, rel, write=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return path


def write_json(workspace: Path, rel: str, data: Any) -> Path:
    path = resolve(workspace, rel, write=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def as_list(value: Any) -> list[str]:
    if value is None:
        return []
    return [value] if isinstance(value, str) else list(value)


# --- engine ---------------------------------------------------------------------

@dataclass
class BankLine:
    index: int
    date: date
    description: str
    amount: Decimal
    balance: Decimal | None


def load_statement(workspace: Path, rel: str) -> dict[str, Any]:
    """Bank lines plus opening/ending balances and running-balance breaks."""
    rows = read_rows(workspace, rel, ["date", "description", "amount"])
    if not rows:
        raise ToolError(f"{rel}: no transactions")
    lines = [BankLine(i, parse_date(r["date"]), r["description"], money(r["amount"]),
                      money(r["balance"]) if r.get("balance") else None)
             for i, r in enumerate(rows, start=1)]
    breaks: list[str] = []
    opening = ending = None
    if all(line.balance is not None for line in lines):
        opening = lines[0].balance - lines[0].amount
        running = opening
        for line in lines:
            running += line.amount
            if running != line.balance:
                breaks.append(f"row {line.index} ({line.date}): running balance {fmt(line.balance)} "
                              f"but opening + activity = {fmt(running)}")
                running = line.balance
        ending = lines[-1].balance
    deposits = sum((ln.amount for ln in lines if ln.amount > 0), ZERO)
    withdrawals = sum((ln.amount for ln in lines if ln.amount < 0), ZERO)
    return {"lines": lines, "opening": opening, "ending": ending, "deposits": deposits,
            "withdrawals": withdrawals, "net": deposits + withdrawals, "breaks": breaks,
            "start": min(ln.date for ln in lines), "end": max(ln.date for ln in lines)}


@dataclass
class GLLine:
    entry_id: str
    date: date
    account: str
    description: str
    debit: Decimal
    credit: Decimal

    @property
    def net(self) -> Decimal:
        return self.debit - self.credit


def load_gl(workspace: Path, rel: str) -> list[GLLine]:
    rows = read_rows(workspace, rel, ["entry_id", "date", "account", "debit", "credit"])
    lines = []
    for r in rows:
        debit, credit = money(r["debit"]), money(r["credit"])
        if debit < 0 or credit < 0:
            raise ToolError(f"{rel}: negative debit/credit in entry {r['entry_id']}")
        lines.append(GLLine(r["entry_id"], parse_date(r["date"]), r["account"],
                            r.get("description", ""), debit, credit))
    return lines


def load_journal(workspace: Path, rel: str | None) -> list[dict[str, Any]]:
    """Draft journal entry lines (missing file = no drafts yet)."""
    if not rel or not resolve(workspace, rel).is_file():
        return []
    rows = read_rows(workspace, rel, ["entry_id", "date", "account", "debit", "credit"])
    for r in rows:
        r["debit_d"], r["credit_d"] = money(r["debit"]), money(r["credit"])
    return rows


def load_coa(workspace: Path, rel: str) -> dict[str, dict[str, str]]:
    rows = read_rows(workspace, rel, ["account", "name", "type"])
    coa = {}
    for r in rows:
        kind = r["type"].lower()
        if kind not in ACCOUNT_TYPES:
            raise ToolError(f"{rel}: account {r['account']} has unknown type {r['type']!r}")
        coa[r["account"]] = {"name": r["name"], "type": kind}
    return coa


def load_balances(workspace: Path, paths: Any) -> dict[str, Decimal]:
    """Net (debit - credit) balance per account from one or more TB-style CSVs."""
    balances: dict[str, Decimal] = {}
    for rel in as_list(paths):
        for r in read_rows(workspace, rel, ["account", "debit", "credit"]):
            balances[r["account"]] = balances.get(r["account"], ZERO) + money(r["debit"]) - money(r["credit"])
    return balances


def balances_from(gl: list[GLLine], journal: list[dict[str, Any]] = (), *,
                  as_of: date | None = None, before: date | None = None) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for line in gl:
        if (as_of and line.date > as_of) or (before and line.date >= before):
            continue
        out[line.account] = out.get(line.account, ZERO) + line.net
    for r in journal:
        out[r["account"]] = out.get(r["account"], ZERO) + r["debit_d"] - r["credit_d"]
    return out


def unbalanced_entries(lines: list[tuple[str, Decimal, Decimal]]) -> dict[str, Decimal]:
    """entry_id -> (debits - credits) for entries that do not balance."""
    totals: dict[str, Decimal] = {}
    for entry_id, debit, credit in lines:
        totals[entry_id] = totals.get(entry_id, ZERO) + debit - credit
    return {k: v for k, v in totals.items() if v != 0}


def reconcile(statement: dict[str, Any], gl: list[GLLine], cash_account: str, *,
              period_start: date, period_end: date, date_window: int = 5) -> dict[str, Any]:
    """Match bank lines to GL cash lines (same amount, closest date within the
    window) and compute the adjusted bank and book balances.

    GL lines dated before period_start are the opening balance, assumed
    reconciled at the prior close; unmatched GL lines in the period are
    outstanding (deposits in transit / outstanding checks); unmatched bank
    lines are unrecorded in the books (fees, interest) and need an entry.
    """
    cash = [ln for ln in gl if ln.account == cash_account]
    if not cash:
        raise ToolError(f"no GL lines for cash account {cash_account}")
    if statement["ending"] is None:
        raise ToolError("statement needs a running balance column to reconcile")
    candidates = [ln for ln in cash if period_start <= ln.date <= period_end]
    used: set[int] = set()
    matches, bank_only = [], []
    for bank in statement["lines"]:
        best, best_gap = None, None
        for i, g in enumerate(candidates):
            if i in used or g.net != bank.amount:
                continue
            gap = abs((g.date - bank.date).days)
            if gap <= date_window and (best_gap is None or gap < best_gap):
                best, best_gap = i, gap
        if best is None:
            bank_only.append(bank)
        else:
            used.add(best)
            matches.append((bank, candidates[best]))
    gl_only = [g for i, g in enumerate(candidates) if i not in used]
    gl_ending = sum((ln.net for ln in cash if ln.date <= period_end), ZERO)
    outstanding = sum((g.net for g in gl_only), ZERO)
    unrecorded = sum((b.amount for b in bank_only), ZERO)
    adjusted_bank = statement["ending"] + outstanding
    adjusted_book = gl_ending + unrecorded
    return {
        "account": cash_account,
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "statement_opening_balance": fmt(statement["opening"]),
        "statement_ending_balance": fmt(statement["ending"]),
        "gl_ending_balance": fmt(gl_ending),
        "matched_count": len(matches),
        "outstanding_items": [
            {"entry_id": g.entry_id, "date": g.date.isoformat(), "description": g.description,
             "amount": fmt(g.net), "type": "deposit_in_transit" if g.net > 0 else "outstanding_payment",
             "age_days": (period_end - g.date).days}
            for g in gl_only],
        "unrecorded_items": [
            {"row": b.index, "date": b.date.isoformat(), "description": b.description,
             "amount": fmt(b.amount), "type": "bank_credit" if b.amount > 0 else "bank_charge"}
            for b in bank_only],
        "total_outstanding": fmt(outstanding),
        "total_unrecorded": fmt(unrecorded),
        "adjusted_bank_balance": fmt(adjusted_bank),
        "adjusted_book_balance": fmt(adjusted_book),
        "unexplained_difference": fmt(adjusted_bank - adjusted_book),
        "statement_breaks": statement["breaks"],
    }


def load_rules(workspace: Path, rel: str) -> list[dict[str, Any]]:
    rules = []
    for r in read_rows(workspace, rel, ["pattern", "account", "confidence"]):
        try:
            regex = re.compile(r["pattern"], re.I)
        except re.error as exc:
            raise ToolError(f"{rel}: bad pattern {r['pattern']!r}: {exc}") from None
        rules.append({"pattern": r["pattern"], "regex": regex, "account": r["account"],
                      "confidence": float(r["confidence"] or 0)})
    return rules


def categorize(lines: list[BankLine], rules: list[dict[str, Any]], min_confidence: float) -> list[dict[str, Any]]:
    """First matching rule wins; no match -> uncategorized, confidence 0."""
    out = []
    for line in lines:
        rule = next((r for r in rules if r["regex"].search(line.description)), None)
        confidence = rule["confidence"] if rule else 0.0
        out.append({
            "row": line.index, "date": line.date.isoformat(), "description": line.description,
            "amount": fmt(line.amount), "account": rule["account"] if rule else "",
            "confidence": f"{confidence:.2f}", "rule": rule["pattern"] if rule else "",
            "rationale": f"matched rule /{rule['pattern']}/" if rule else "no rule matched",
            "needs_review": "yes" if confidence < min_confidence else "no",
        })
    return out


def flux(current: dict[str, Decimal], prior: dict[str, Decimal], *, threshold_abs: Decimal,
         threshold_pct: Decimal) -> list[dict[str, Any]]:
    """Variance per account; flagged when |change| >= threshold_abs and the
    percentage change is >= threshold_pct (or the prior balance was zero)."""
    rows = []
    for account in sorted(set(current) | set(prior)):
        cur, pri = current.get(account, ZERO), prior.get(account, ZERO)
        change = cur - pri
        pct = None if pri == 0 else (change / abs(pri) * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
        flagged = abs(change) >= threshold_abs and (pct is None or abs(pct) >= threshold_pct)
        rows.append({"account": account, "prior": fmt(pri), "current": fmt(cur), "change": fmt(change),
                     "change_pct": "" if pct is None else f"{pct}", "flagged": "yes" if flagged else "no"})
    return rows


def months_elapsed(start: date, period_end: date, months: int) -> int:
    n = (period_end.year - start.year) * 12 + period_end.month - start.month + 1
    return max(0, min(months, n))


def schedule_rows(workspace: Path, rel: str, period_end: date) -> list[dict[str, Any]]:
    """Straight-line schedule: amount due through period_end minus booked to date."""
    rows = read_rows(workspace, rel, ["item_id", "type", "description", "pl_account", "balance_account",
                                      "total_amount", "start_date", "months", "booked_to_date", "support"])
    out = []
    for r in rows:
        kind = r["type"].lower()
        if kind not in ("prepaid", "accrual", "depreciation", "deferred_revenue"):
            raise ToolError(f"{rel}: item {r['item_id']} has unknown type {r['type']!r}")
        total, booked = money(r["total_amount"]), money(r["booked_to_date"])
        months = int(r["months"] or 1)
        if months < 1:
            raise ToolError(f"{rel}: item {r['item_id']} needs months >= 1")
        elapsed = months_elapsed(parse_date(r["start_date"]), period_end, months)
        due = total if elapsed == months else (total * elapsed / months).quantize(CENT, rounding=ROUND_HALF_UP)
        out.append({**{k: r[k] for k in ("item_id", "type", "description", "pl_account",
                                         "balance_account", "support")},
                    "total_amount": fmt(total), "months": months, "months_elapsed": elapsed,
                    "due_through_period": fmt(due), "booked_to_date": fmt(booked),
                    "this_period": fmt(due - booked), "remaining_after": fmt(total - due)})
    return out


def statements(balances: dict[str, Decimal], coa: dict[str, dict[str, str]]) -> dict[str, Any]:
    """P&L and balance sheet from net balances; revenue, liabilities and
    equity are shown credit-positive."""
    unknown = sorted(a for a in balances if a not in coa)
    if unknown:
        raise ToolError(f"accounts missing from the chart of accounts: {', '.join(unknown)}")
    lines: dict[str, list[dict[str, str]]] = {t: [] for t in ACCOUNT_TYPES}
    totals = {t: ZERO for t in ACCOUNT_TYPES}
    for account in sorted(balances):
        kind = coa[account]["type"]
        amount = balances[account] if kind in ("asset", "expense") else -balances[account]
        if amount == 0:
            continue
        lines[kind].append({"account": account, "name": coa[account]["name"], "amount": fmt(amount)})
        totals[kind] += amount
    net_income = totals["revenue"] - totals["expense"]
    equity_total = totals["equity"] + net_income
    return {
        "income_statement": {"revenue": lines["revenue"], "expenses": lines["expense"],
                             "total_revenue": fmt(totals["revenue"]),
                             "total_expenses": fmt(totals["expense"]), "net_income": fmt(net_income)},
        "balance_sheet": {"assets": lines["asset"], "liabilities": lines["liability"],
                          "equity": lines["equity"], "total_assets": fmt(totals["asset"]),
                          "total_liabilities": fmt(totals["liability"]),
                          "total_equity_including_net_income": fmt(equity_total),
                          "difference": fmt(totals["asset"] - totals["liability"] - equity_total)},
    }


def period_bounds(period: str) -> tuple[date, date]:
    match = re.fullmatch(r"(\d{4})-(\d{2})", str(period or "").strip())
    if not match:
        raise ToolError("period must be YYYY-MM")
    year, month = int(match[1]), int(match[2])
    start = date(year, month, 1)
    end = date(year + (month == 12), month % 12 + 1, 1)
    return start, date.fromordinal(end.toordinal() - 1)


# --- tools ----------------------------------------------------------------------

def parse_bank_statement(workspace: Path, *, fetch=None, run=None, path: str = "inputs/bank_statement.csv",
                         **_: Any) -> dict:
    s = load_statement(Path(workspace), path)
    return {"rows": len(s["lines"]), "first_date": s["start"].isoformat(), "last_date": s["end"].isoformat(),
            "opening_balance": fmt(s["opening"]) if s["opening"] is not None else None,
            "ending_balance": fmt(s["ending"]) if s["ending"] is not None else None,
            "total_deposits": fmt(s["deposits"]), "total_withdrawals": fmt(s["withdrawals"]),
            "net_activity": fmt(s["net"]), "running_balance_breaks": s["breaks"]}


def categorize_transactions(workspace: Path, *, fetch=None, run=None,
                            statement: str = "inputs/bank_statement.csv",
                            rules: str = "inputs/categorization_rules.csv",
                            output: str = "deliverables/m2-period-close/categorized.csv",
                            min_confidence: float = 0.8, **_: Any) -> dict:
    workspace = Path(workspace)
    s = load_statement(workspace, statement)
    rows = categorize(s["lines"], load_rules(workspace, rules), float(min_confidence))
    write_rows(workspace, output, ["row", "date", "description", "amount", "account", "confidence",
                                   "rule", "rationale", "needs_review"], rows)
    review = [r for r in rows if r["needs_review"] == "yes"]
    return {"output": output, "rows": len(rows), "total": fmt(s["net"]),
            "needs_review": len(review), "uncategorized": sum(1 for r in rows if not r["account"]),
            "review_rows": [{"row": r["row"], "description": r["description"], "amount": r["amount"]}
                            for r in review]}


def find_exceptions(workspace: Path, *, fetch=None, run=None,
                    categorized: str = "deliverables/m2-period-close/categorized.csv",
                    gl: str = "inputs/gl_detail.csv", chart_of_accounts: str = "inputs/chart_of_accounts.csv",
                    output: str = "deliverables/m2-period-close/exceptions.csv",
                    round_dollar_min: str = "1000", duplicate_days: int = 3, **_: Any) -> dict:
    """Exceptions queue: items needing review, likely duplicates, large
    round-dollar movements and every GL line sitting in a suspense account."""
    workspace = Path(workspace)
    cat = read_rows(workspace, categorized, ["row", "date", "description", "amount", "account", "needs_review"])
    coa = load_coa(workspace, chart_of_accounts)
    suspense = {a for a, info in coa.items() if SUSPENSE_NAME_RE.search(info["name"])}
    round_min = money(round_dollar_min)
    items: list[dict[str, str]] = []

    def add(source, ref, day, desc, amount, account, reason):
        items.append({"id": f"EX-{len(items) + 1:03d}", "source": source, "ref": ref, "date": day,
                      "description": desc, "amount": amount, "account": account, "reason": reason,
                      "resolution": ""})

    for r in cat:
        amount = money(r["amount"])
        if r["needs_review"].lower() == "yes" or not r["account"] or r["account"] in suspense:
            add("bank", f"row {r['row']}", r["date"], r["description"], fmt(amount), r["account"],
                "uncategorized or low confidence")
        elif abs(amount) >= round_min and amount == amount.to_integral_value() and amount % 100 == 0:
            add("bank", f"row {r['row']}", r["date"], r["description"], fmt(amount), r["account"],
                "large round-dollar amount; confirm nature")
    for i, a in enumerate(cat):
        for b in cat[i + 1:]:
            same = money(a["amount"]) == money(b["amount"]) and a["description"].lower() == b["description"].lower()
            if same and abs((parse_date(a["date"]) - parse_date(b["date"])).days) <= int(duplicate_days):
                add("bank", f"rows {a['row']},{b['row']}", b["date"], b["description"], fmt(money(b["amount"])),
                    b["account"], "possible duplicate")
    for line in load_gl(workspace, gl):
        if line.account in suspense:
            add("gl", line.entry_id, line.date.isoformat(), line.description, fmt(line.net), line.account,
                f"balance in suspense account {line.account} ({coa[line.account]['name']})")
    write_rows(workspace, output, ["id", "source", "ref", "date", "description", "amount", "account",
                                   "reason", "resolution"], items)
    return {"output": output, "exceptions": len(items), "suspense_accounts": sorted(suspense),
            "items": items[:50]}


def reconcile_bank(workspace: Path, *, fetch=None, run=None, statement: str = "inputs/bank_statement.csv",
                   gl: str = "inputs/gl_detail.csv", cash_account: str = "", period: str = "",
                   date_window: int = 5,
                   output: str = "deliverables/m2-period-close/bank_reconciliation.json", **_: Any) -> dict:
    workspace = Path(workspace)
    if not cash_account:
        raise ToolError("cash_account is required (the GL account for this bank account)")
    s = load_statement(workspace, statement)
    start, end = period_bounds(period) if period else (s["start"], s["end"])
    rec = reconcile(s, load_gl(workspace, gl), str(cash_account), period_start=start, period_end=end,
                    date_window=int(date_window))
    rec["date_window_days"] = int(date_window)
    write_json(workspace, output, rec)
    return {"output": output, **{k: rec[k] for k in (
        "statement_ending_balance", "gl_ending_balance", "matched_count", "total_outstanding",
        "total_unrecorded", "adjusted_bank_balance", "adjusted_book_balance", "unexplained_difference")},
        "outstanding_items": rec["outstanding_items"], "unrecorded_items": rec["unrecorded_items"],
        "statement_breaks": rec["statement_breaks"]}


def _append_entry(workspace: Path, output: str, rows: list[dict[str, Any]]) -> None:
    path = resolve(workspace, output, write=True)
    existing = read_rows(workspace, output, JE_COLUMNS) if path.is_file() else []
    ids = {r["entry_id"] for r in existing}
    if rows[0]["entry_id"] in ids:
        raise ToolError(f"entry {rows[0]['entry_id']} already exists in {output}")
    write_rows(workspace, output, JE_COLUMNS, existing + rows)


def draft_journal_entry(workspace: Path, *, fetch=None, run=None, entry_id: str = "", date: str = "",
                        description: str = "", lines: list | None = None, support: str = "",
                        output: str = "deliverables/m2-period-close/journal_entries.csv",
                        chart_of_accounts: str = "inputs/chart_of_accounts.csv", **_: Any) -> dict:
    """Append one balanced draft entry. Refuses unbalanced entries, entries
    without support, suspense accounts and plug-like descriptions."""
    workspace = Path(workspace)
    if not entry_id or not description or not lines:
        raise ToolError("entry_id, description and at least two lines are required")
    if not str(support).strip():
        raise ToolError("support is required: cite the document, schedule row or GL query behind the entry")
    if PLUG_WORDS_RE.search(description):
        raise ToolError("description reads like a plug; draft only entries you can support")
    day = parse_date(date).isoformat()
    coa = load_coa(workspace, chart_of_accounts)
    out, debits, credits = [], ZERO, ZERO
    for line in lines:
        account = str(line.get("account", ""))
        if account not in coa:
            raise ToolError(f"unknown account {account!r}")
        if SUSPENSE_NAME_RE.search(coa[account]["name"]):
            raise ToolError(f"do not draft entries into suspense account {account}; queue an exception instead")
        debit, credit = money(line.get("debit", "")), money(line.get("credit", ""))
        if debit < 0 or credit < 0 or (debit and credit) or not (debit or credit):
            raise ToolError("each line needs exactly one positive debit or credit")
        debits, credits = debits + debit, credits + credit
        out.append({"entry_id": entry_id, "date": day, "account": account,
                    "description": line.get("description") or description,
                    "debit": fmt(debit) if debit else "", "credit": fmt(credit) if credit else "",
                    "support": support, "status": DRAFT_STATUS})
    if len(out) < 2 or debits != credits:
        raise ToolError(f"entry does not balance: debits {fmt(debits)} vs credits {fmt(credits)}")
    _append_entry(workspace, output, out)
    return {"output": output, "entry_id": entry_id, "lines": len(out), "amount": fmt(debits),
            "status": DRAFT_STATUS}


def build_accrual_schedule(workspace: Path, *, fetch=None, run=None,
                           schedule: str = "inputs/accrual_schedule.csv", period: str = "",
                           output: str = "deliverables/m2-period-close/accrual_schedule.csv",
                           journal_output: str = "deliverables/m2-period-close/journal_entries.csv",
                           draft_entries: bool = True, **_: Any) -> dict:
    """Roll the prepaid / accrual / deferred revenue / depreciation schedule
    to period end and draft one entry per item with a this-period amount."""
    workspace = Path(workspace)
    _, end = period_bounds(period)
    rows = schedule_rows(workspace, schedule, end)
    write_rows(workspace, output, ["item_id", "type", "description", "pl_account", "balance_account",
                                   "total_amount", "months", "months_elapsed", "due_through_period",
                                   "booked_to_date", "this_period", "remaining_after", "support"], rows)
    drafted = []
    if draft_entries:
        for r in rows:
            amount = money(r["this_period"])
            if amount == 0:
                continue
            # Revenue deferrals release the liability; everything else
            # recognizes expense against the balance-sheet account.
            dr, cr = ((r["balance_account"], r["pl_account"]) if r["type"] == "deferred_revenue"
                      else (r["pl_account"], r["balance_account"]))
            if amount < 0:
                dr, cr, amount = cr, dr, -amount
            entry_id = f"ADJ-{end:%Y%m}-{r['item_id']}"
            draft_journal_entry(workspace, entry_id=entry_id, date=end.isoformat(),
                                description=f"{r['type'].replace('_', ' ')}: {r['description']}",
                                lines=[{"account": dr, "debit": fmt(amount)},
                                       {"account": cr, "credit": fmt(amount)}],
                                support=f"{schedule}#{r['item_id']}; {r['support']}".strip("; "),
                                output=journal_output)
            drafted.append({"entry_id": entry_id, "amount": fmt(amount)})
    return {"output": output, "items": len(rows), "journal_output": journal_output,
            "drafted": drafted, "rows": rows}


def build_trial_balance(workspace: Path, *, fetch=None, run=None, gl: str = "inputs/gl_detail.csv",
                        journal_entries: str | None = "deliverables/m2-period-close/journal_entries.csv",
                        chart_of_accounts: str = "inputs/chart_of_accounts.csv", as_of: str = "",
                        output: str = "deliverables/m2-period-close/trial_balance.csv", **_: Any) -> dict:
    workspace = Path(workspace)
    gl_lines = load_gl(workspace, gl)
    journal = load_journal(workspace, journal_entries)
    coa = load_coa(workspace, chart_of_accounts)
    bad = unbalanced_entries([(g.entry_id, g.debit, g.credit) for g in gl_lines] +
                             [(r["entry_id"], r["debit_d"], r["credit_d"]) for r in journal])
    balances = balances_from(gl_lines, journal, as_of=parse_date(as_of) if as_of else None)
    rows = [{"account": a, "name": coa.get(a, {}).get("name", "UNKNOWN ACCOUNT"),
             "debit": fmt(b) if b > 0 else "", "credit": fmt(-b) if b < 0 else ""}
            for a, b in sorted(balances.items()) if b != 0]
    debits = sum((money(r["debit"]) for r in rows), ZERO)
    credits = sum((money(r["credit"]) for r in rows), ZERO)
    write_rows(workspace, output, TB_COLUMNS, rows)
    return {"output": output, "accounts": len(rows), "total_debits": fmt(debits), "total_credits": fmt(credits),
            "balanced": debits == credits, "unbalanced_entries": {k: fmt(v) for k, v in bad.items()},
            "unknown_accounts": sorted(a for a in balances if a not in coa)}


def flux_analysis(workspace: Path, *, fetch=None, run=None,
                  current: str = "deliverables/m2-period-close/trial_balance.csv",
                  prior: Any = ("inputs/prior_trial_balance.csv", "inputs/prior_month_pl.csv"),
                  threshold_abs: str = "1000", threshold_pct: str = "10",
                  output: str = "deliverables/m3-close-package/flux.csv", **_: Any) -> dict:
    """Variance table with an empty commentary column for the model to fill."""
    workspace = Path(workspace)
    rows = flux(load_balances(workspace, current), load_balances(workspace, prior),
                threshold_abs=money(threshold_abs), threshold_pct=Decimal(str(threshold_pct)))
    for r in rows:
        r["commentary"] = ""
    write_rows(workspace, output, ["account", "prior", "current", "change", "change_pct", "flagged",
                                   "commentary"], rows)
    flagged = [r for r in rows if r["flagged"] == "yes"]
    return {"output": output, "accounts": len(rows), "flagged": len(flagged),
            "flagged_rows": flagged, "note": "write commentary for every flagged row; do not change amounts"}


def tie_opening_balances(workspace: Path, *, fetch=None, run=None, gl: str = "inputs/gl_detail.csv",
                         prior_trial_balance: str = "inputs/prior_trial_balance.csv", period: str = "",
                         tolerance: str = "1.00",
                         output: str = "deliverables/m1-onboarding/opening_balance_tieout.csv",
                         **_: Any) -> dict:
    workspace = Path(workspace)
    start, _end = period_bounds(period)
    opening = balances_from(load_gl(workspace, gl), before=start)
    prior = load_balances(workspace, prior_trial_balance)
    tol = money(tolerance)
    rows = []
    for account in sorted(set(opening) | set(prior)):
        diff = opening.get(account, ZERO) - prior.get(account, ZERO)
        rows.append({"account": account, "gl_opening": fmt(opening.get(account, ZERO)),
                     "prior_tb": fmt(prior.get(account, ZERO)), "difference": fmt(diff),
                     "within_tolerance": "yes" if abs(diff) <= tol else "no", "explanation": ""})
    write_rows(workspace, output, ["account", "gl_opening", "prior_tb", "difference", "within_tolerance",
                                   "explanation"], rows)
    differences = [r for r in rows if r["within_tolerance"] == "no"]
    return {"output": output, "accounts": len(rows), "differences": differences,
            "note": "explain every difference outside tolerance in the explanation column"}


def build_account_map(workspace: Path, *, fetch=None, run=None,
                      chart_of_accounts: str = "inputs/chart_of_accounts.csv", gl: str = "inputs/gl_detail.csv",
                      output: str = "deliverables/m1-onboarding/account_map.csv", **_: Any) -> dict:
    """Map each account to its statement and a default line by type; the
    model may refine the line names afterwards."""
    workspace = Path(workspace)
    coa = load_coa(workspace, chart_of_accounts)
    used = {g.account for g in load_gl(workspace, gl)}
    default_line = {"asset": "Assets", "liability": "Liabilities", "equity": "Equity",
                    "revenue": "Revenue", "expense": "Operating expenses"}
    rows = [{"account": a, "name": info["name"], "type": info["type"],
             "statement": "balance_sheet" if info["type"] in ("asset", "liability", "equity") else "income_statement",
             "line": default_line[info["type"]], "used_in_gl": "yes" if a in used else "no",
             "suspense": "yes" if SUSPENSE_NAME_RE.search(info["name"]) else "no"}
            for a, info in sorted(coa.items())]
    write_rows(workspace, output, ["account", "name", "type", "statement", "line", "used_in_gl", "suspense"], rows)
    return {"output": output, "accounts": len(rows), "unmapped_gl_accounts": sorted(used - set(coa))}


def build_financial_statements(workspace: Path, *, fetch=None, run=None,
                               trial_balance: str = "deliverables/m2-period-close/trial_balance.csv",
                               chart_of_accounts: str = "inputs/chart_of_accounts.csv",
                               output: str = "deliverables/m3-close-package/financial_statements.json",
                               **_: Any) -> dict:
    workspace = Path(workspace)
    data = statements(load_balances(workspace, trial_balance), load_coa(workspace, chart_of_accounts))
    data["source"] = trial_balance
    write_json(workspace, output, data)
    return {"output": output, "net_income": data["income_statement"]["net_income"],
            "total_assets": data["balance_sheet"]["total_assets"],
            "difference": data["balance_sheet"]["difference"]}


# --- tool definitions ---------------------------------------------------------------

def _schema(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required or []}


_PATH = {"type": "string", "description": "workspace-relative path"}
_OUT = {"type": "string", "description": "workspace-relative output path under deliverables/"}
_PERIOD = {"type": "string", "description": "period being closed, YYYY-MM"}

TOOL_DEFS: list[dict[str, Any]] = [
    {"name": "parse_bank_statement", "risk": "read", "function": parse_bank_statement,
     "description": "Summarize a bank statement CSV: row count, opening and ending balance, deposits, "
                    "withdrawals and any rows where the running balance does not follow from the activity.",
     "input_schema": _schema({"path": _PATH})},
    {"name": "categorize_transactions", "risk": "write", "function": categorize_transactions,
     "description": "Apply the categorization rulebook (regex pattern -> account, confidence) to every bank "
                    "line and write categorized.csv. Lines with no rule or low confidence are marked "
                    "needs_review; decide those yourself or queue them, never guess silently.",
     "input_schema": _schema({"statement": _PATH, "rules": _PATH, "output": _OUT,
                              "min_confidence": {"type": "number"}})},
    {"name": "find_exceptions", "risk": "write", "function": find_exceptions,
     "description": "Build the exceptions queue: needs-review and uncategorized bank lines, likely "
                    "duplicates, large round-dollar amounts and every GL line in a suspense or Ask My "
                    "Accountant account. Fill the resolution column only with what the client confirmed.",
     "input_schema": _schema({"categorized": _PATH, "gl": _PATH, "chart_of_accounts": _PATH, "output": _OUT,
                              "round_dollar_min": {"type": "string"}, "duplicate_days": {"type": "integer"}})},
    {"name": "reconcile_bank", "risk": "write", "function": reconcile_bank,
     "description": "Reconcile the bank statement to the GL cash account for the period: match by amount "
                    "within a date window, list outstanding and unrecorded items and compute the "
                    "unexplained difference, which must be 0.00. Writes bank_reconciliation.json.",
     "input_schema": _schema({"statement": _PATH, "gl": _PATH, "cash_account": {"type": "string"},
                              "period": _PERIOD, "date_window": {"type": "integer"}, "output": _OUT},
                             ["cash_account", "period"])},
    {"name": "draft_journal_entry", "risk": "write", "function": draft_journal_entry,
     "description": "Append one balanced DRAFT journal entry (never posted) to journal_entries.csv. Each "
                    "line has an account and exactly one of debit or credit; support must cite the "
                    "document, schedule row or reconciliation item behind it. Suspense accounts and "
                    "balancing plugs are refused.",
     "input_schema": _schema({"entry_id": {"type": "string"}, "date": {"type": "string"},
                              "description": {"type": "string"}, "support": {"type": "string"},
                              "lines": {"type": "array", "items": {"type": "object", "properties": {
                                  "account": {"type": "string"}, "debit": {"type": "string"},
                                  "credit": {"type": "string"}, "description": {"type": "string"}},
                                  "required": ["account"]}},
                              "output": _OUT, "chart_of_accounts": _PATH},
                             ["entry_id", "date", "description", "lines", "support"])},
    {"name": "build_accrual_schedule", "risk": "write", "function": build_accrual_schedule,
     "description": "Roll the prepaid, accrual, deferred revenue and depreciation schedule to period end "
                    "(straight line: due through period minus booked to date) and draft one entry per "
                    "item with a this-period amount.",
     "input_schema": _schema({"schedule": _PATH, "period": _PERIOD, "output": _OUT, "journal_output": _OUT,
                              "draft_entries": {"type": "boolean"}}, ["period"])},
    {"name": "build_trial_balance", "risk": "write", "function": build_trial_balance,
     "description": "Build the trial balance from the GL export plus the draft entries (the adjusted TB), "
                    "report total debits and credits, unbalanced entries and unknown accounts.",
     "input_schema": _schema({"gl": _PATH, "journal_entries": _PATH, "chart_of_accounts": _PATH,
                              "as_of": {"type": "string"}, "output": _OUT})},
    {"name": "flux_analysis", "risk": "write", "function": flux_analysis,
     "description": "Compare current balances to prior ones and flag changes at or above both thresholds. "
                    "Writes flux.csv with an empty commentary column: explain every flagged row from the "
                    "underlying activity, without changing the amounts.",
     "input_schema": _schema({"current": _PATH, "prior": {"type": "array", "items": _PATH},
                              "threshold_abs": {"type": "string"}, "threshold_pct": {"type": "string"},
                              "output": _OUT})},
    {"name": "tie_opening_balances", "risk": "write", "function": tie_opening_balances,
     "description": "Compare GL balances before the period to the prior closing trial balance, account by "
                    "account, and write the tie-out with an explanation column for differences.",
     "input_schema": _schema({"gl": _PATH, "prior_trial_balance": _PATH, "period": _PERIOD,
                              "tolerance": {"type": "string"}, "output": _OUT}, ["period"])},
    {"name": "build_account_map", "risk": "write", "function": build_account_map,
     "description": "Map every chart-of-accounts account to its statement and a default line, flag "
                    "suspense accounts and list GL accounts missing from the chart.",
     "input_schema": _schema({"chart_of_accounts": _PATH, "gl": _PATH, "output": _OUT})},
    {"name": "build_financial_statements", "risk": "write", "function": build_financial_statements,
     "description": "Produce the income statement and balance sheet from the adjusted trial balance; the "
                    "balance sheet difference must be 0.00.",
     "input_schema": _schema({"trial_balance": _PATH, "chart_of_accounts": _PATH, "output": _OUT})},
]
