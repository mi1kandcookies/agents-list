"""
specialists/bookkeeping_close/tools.py - deterministic accounting engine and
domain tools for the month-end close specialist.

All arithmetic lives here, in Decimal, so the model never adds numbers: it
decides what an ambiguous transaction is, explains variances and writes the
memo. Tools are plain functions `fn(workspace, *, fetch=None, run=None,
resolve_path=None, **args) -> dict` listed in TOOL_DEFS; the kit wraps them
(tools_from_defs) and passes resolve_path, its PolicyGate-checked path
helper. Every model-supplied path goes through it before a file is touched,
and every output is recorded as agent-authored in the claim ledger, so a
tool-written file can never be cited as a source.

File formats (CSV with a header row; amounts may use $, commas and
parentheses for negatives):

    bank statement   date, description, amount (+ deposit / - withdrawal), balance
                     (oldest or newest first)
    GL detail        entry_id, date, account, description, debit, credit
    chart of accts   account, name, type (asset|liability|equity|revenue|expense)
    trial balance    account, name, debit, credit
    journal entries  entry_id, date, account, description, debit, credit, support, status
    rules            pattern, account, confidence
    prior bank rec   the previous period's bank_reconciliation.json, or a CSV
                     of its outstanding items (date, description, amount)

The period, cash account, flux thresholds and matching window default to
inputs/close_parameters.json (written from the intake). The close is cut off
at period end: GL lines, draft entries and bank lines dated after it belong
to the next close.

Paths are workspace-relative and checked lexically before the filesystem is
touched (the kit's jail_path); .agentkit/ belongs to the kit and is refused.
Outputs may only be written under deliverables/; inputs/ is the customer's
and is never modified. Nothing here posts to a ledger: journal entries are
drafts with status "draft - do not post".
"""
from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from itertools import combinations
from pathlib import Path
from typing import Any, Callable

from agentkit.errors import PolicyViolation, ToolError
from agentkit.policy import INTERNAL_DIR, jail_path

CENT = Decimal("0.01")
ZERO = Decimal("0.00")
DRAFT_STATUS = "draft - do not post"
ACCOUNT_TYPES = ("asset", "liability", "equity", "revenue", "expense")
PL_TYPES = ("revenue", "expense")
CLOSE_PARAMETERS = "inputs/close_parameters.json"
PRIOR_RECONCILIATION = "inputs/prior_bank_reconciliation.json"
DEFAULT_DATE_WINDOW = 5
# Accounts whose balance means "someone still has to decide": they may carry
# a balance only when every item in them sits in the exceptions queue.
SUSPENSE_NAME_RE = re.compile(r"suspense|uncategori[sz]ed|ask my accountant|clearing - unknown|"
                              r"opening balance equity", re.I)
# Words that give away an entry made only to force a tie.
PLUG_WORDS_RE = re.compile(r"\bplug\b|\bforce[ds]?\b|to balance|balancing entry|unreconciled difference|"
                           r"rounding adjustment|misc(ellaneous)? adjustment", re.I)
# Support that cites the client's answer, the only basis for a reclass out of suspense.
CLIENT_ANSWER_RE = re.compile(r"\bclient (answer|confirm)", re.I)
# The rationales categorize_transactions writes; anything else is a decision.
TOOL_RATIONALE_RE = re.compile(r"matched rule /.*/|no rule matched")
# Batched deposits and split payouts: how many lines one line may clear, and
# how many of the nearest candidates are searched for them.
MAX_BATCH = 5
BATCH_POOL = 12

JE_COLUMNS = ["entry_id", "date", "account", "description", "debit", "credit", "support", "status"]
TB_COLUMNS = ["account", "name", "debit", "credit"]
CATEGORIZED_COLUMNS = ["row", "date", "description", "amount", "account", "confidence", "rule", "rationale",
                       "needs_review"]
EXCEPTION_COLUMNS = ["id", "source", "ref", "date", "description", "amount", "account", "reason", "resolution"]
SCHEDULE_COLUMNS = ["item_id", "type", "description", "pl_account", "balance_account", "total_amount", "months",
                    "months_elapsed", "due_through_period", "booked_to_date", "this_period", "remaining_after",
                    "support"]
FLUX_COLUMNS = ["account", "name", "statement", "basis", "prior", "current", "change", "change_pct", "flagged",
                "commentary"]


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


def percent(value: Any) -> Decimal:
    """Parse "10", "10%" or "7.5" into a Decimal percentage."""
    text = str(value if value is not None else "").strip().rstrip("%").strip()
    try:
        pct = Decimal(text)
    except InvalidOperation:
        raise ToolError(f"not a percentage: {value!r}") from None
    if not pct.is_finite() or pct < 0:
        raise ToolError(f"not a percentage: {value!r}")
    return pct


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
    """Workspace-relative path, jailed by the kit (lexical check first, so a
    UNC or device path never reaches the filesystem, then symlinks); never
    under .agentkit/; writes only under deliverables/."""
    if not rel or not isinstance(rel, str):
        raise ToolError("a workspace-relative path is required")
    root = Path(workspace).resolve()
    try:
        path = jail_path(root, rel)
    except PolicyViolation as exc:
        raise ToolError(str(exc)) from None
    parts = [part.lower() for part in path.relative_to(root).parts]
    if parts and parts[0] == INTERNAL_DIR:
        raise ToolError(f"path {rel!r} is internal to the kit")
    if write and (len(parts) < 2 or parts[0] != "deliverables"):
        raise ToolError(f"outputs must be written under deliverables/: {rel}")
    return path


def exists(workspace: Path, rel: str | None) -> bool:
    return bool(rel) and resolve(workspace, rel).is_file()


def gate(workspace: Path, resolve_path: Any, *, reads: Any = (), writes: Any = ()) -> None:
    """Check a tool's model-supplied paths before any file is touched.

    resolve_path is the kit's helper (None when a tool is called directly):
    it applies the PolicyGate, so a refused path is reported as a policy
    denial, and with write=True it records the output as agent-authored.
    Outputs are first held to the deliverables/ rule, so nothing outside it
    is ever marked authored."""
    for rel in as_list(reads):
        if rel and resolve_path is not None:
            resolve_path(rel)
    for rel in as_list(writes):
        resolve(workspace, rel, write=True)
        if resolve_path is not None:
            resolve_path(rel, write=True)


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


def existing_rows(workspace: Path, rel: str, required: list[str]) -> list[dict[str, str]]:
    """A tool's earlier output, so a re-run keeps the model's work; an
    unreadable one is simply replaced."""
    try:
        return read_rows(workspace, rel, required) if exists(workspace, rel) else []
    except ToolError:
        return []


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


def close_settings(workspace: Path) -> dict[str, Any]:
    """inputs/close_parameters.json (the client's, or written from the intake), or {}."""
    path = resolve(workspace, CLOSE_PARAMETERS)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ToolError(f"{CLOSE_PARAMETERS} is not valid JSON: {exc}") from None
    return data if isinstance(data, dict) else {}


def setting(workspace: Path, value: Any, key: str, default: Any = None) -> Any:
    """An explicit argument, else close_parameters.json, else the default."""
    if value not in (None, ""):
        return value
    found = close_settings(workspace).get(key)
    return found if found not in (None, "") else default


# --- engine ---------------------------------------------------------------------

@dataclass
class BankLine:
    index: int
    date: date
    description: str
    amount: Decimal
    balance: Decimal | None


def _running_breaks(lines: list[BankLine]) -> list[str]:
    running = lines[0].balance - lines[0].amount
    breaks = []
    for line in lines:
        running += line.amount
        if running != line.balance:
            breaks.append(f"row {line.index} ({line.date}): running balance {fmt(line.balance)} "
                          f"but opening + activity = {fmt(running)}")
            running = line.balance
    return breaks


def load_statement(workspace: Path, rel: str) -> dict[str, Any]:
    """Bank lines in date order plus opening/ending balances and running-
    balance breaks. Newest-first exports are read in reverse; `index` stays
    the row number in the file."""
    rows = read_rows(workspace, rel, ["date", "description", "amount"])
    if not rows:
        raise ToolError(f"{rel}: no transactions")
    lines = [BankLine(i, parse_date(r["date"]), r["description"], money(r["amount"]),
                      money(r["balance"]) if r.get("balance") else None)
             for i, r in enumerate(rows, start=1)]
    has_balance = all(line.balance is not None for line in lines)
    newest_first = lines[0].date > lines[-1].date or (
        lines[0].date == lines[-1].date and has_balance and len(lines) > 1
        and bool(_running_breaks(lines)) and not _running_breaks(lines[::-1]))
    if newest_first:
        lines.reverse()
    breaks = _running_breaks(lines) if has_balance else []
    opening = lines[0].balance - lines[0].amount if has_balance else None
    ending = lines[-1].balance if has_balance else None
    deposits = sum((ln.amount for ln in lines if ln.amount > 0), ZERO)
    withdrawals = sum((ln.amount for ln in lines if ln.amount < 0), ZERO)
    return {"lines": lines, "opening": opening, "ending": ending, "deposits": deposits,
            "withdrawals": withdrawals, "net": deposits + withdrawals, "breaks": breaks,
            "newest_first": newest_first,
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
    if not exists(workspace, rel):
        return []
    rows = read_rows(workspace, rel, ["entry_id", "date", "account", "debit", "credit"])
    for r in rows:
        r["debit_d"], r["credit_d"] = money(r["debit"]), money(r["credit"])
        r["date_d"] = parse_date(r["date"])
    return rows


def journal_as_gl(journal: list[dict[str, Any]]) -> list[GLLine]:
    return [GLLine(r["entry_id"], r["date_d"], r["account"], r.get("description", ""),
                   r["debit_d"], r["credit_d"]) for r in journal]


def load_coa(workspace: Path, rel: str) -> dict[str, dict[str, str]]:
    rows = read_rows(workspace, rel, ["account", "name", "type"])
    coa = {}
    for r in rows:
        kind = r["type"].lower()
        if kind not in ACCOUNT_TYPES:
            raise ToolError(f"{rel}: account {r['account']} has unknown type {r['type']!r}")
        coa[r["account"]] = {"name": r["name"], "type": kind}
    return coa


def suspense_accounts(coa: dict[str, dict[str, str]]) -> set[str]:
    return {a for a, info in coa.items() if SUSPENSE_NAME_RE.search(info["name"])}


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
    for line in [*gl, *journal_as_gl(journal)]:
        if (as_of and line.date > as_of) or (before and line.date >= before):
            continue
        out[line.account] = out.get(line.account, ZERO) + line.net
    return out


def unbalanced_entries(lines: list[tuple[str, Decimal, Decimal]]) -> dict[str, Decimal]:
    """entry_id -> (debits - credits) for entries that do not balance."""
    totals: dict[str, Decimal] = {}
    for entry_id, debit, credit in lines:
        totals[entry_id] = totals.get(entry_id, ZERO) + debit - credit
    return {k: v for k, v in totals.items() if v != 0}


def suspense_problems(balances: dict[str, Decimal], journal: list[dict[str, Any]],
                      suspense: set[str]) -> list[tuple[list[str], str]]:
    """Draft lines may touch a suspense account only to reclass out of it
    what the client explained: every such entry cites the client's answer in
    its support, and together they move the balance toward zero, never past
    it. Returns (entry ids, problem) pairs."""
    problems = []
    for account in sorted({r["account"] for r in journal} & suspense):
        lines = [r for r in journal if r["account"] == account]
        uncited = [r for r in lines if not CLIENT_ANSWER_RE.search(r.get("support", ""))]
        balance = balances.get(account, ZERO)
        change = sum((r["debit_d"] - r["credit_d"] for r in lines), ZERO)
        toward_zero = balance != 0 and change != 0 and (change > 0) != (balance > 0) and abs(change) <= abs(balance)
        if uncited or not toward_zero:
            problems.append((sorted({r["entry_id"] for r in lines}),
                             f"posts to suspense account {account} (balance {fmt(balance)}): only a reclass out "
                             "of suspense that cites the client's answer is allowed; queue an exception instead"))
    return problems


@dataclass
class BookItem:
    """A cash movement in the books: a GL or draft line dated in the period,
    or an item left outstanding at the prior close (prior=True)."""
    entry_id: str
    date: date
    description: str
    amount: Decimal
    prior: bool = False


def prior_reconciliation_file(workspace: Path, rel: str | None) -> str | None:
    """rel, or the file beside it with the other extension (.json / .csv), or None."""
    if not rel:
        return None
    if exists(workspace, rel):
        return rel
    stem, _, ext = rel.rpartition(".")
    other = f"{stem}.{'csv' if ext.lower() == 'json' else 'json'}" if stem else ""
    return other if other and exists(workspace, other) else None


def load_prior_outstanding(workspace: Path, rel: str | None) -> list[BookItem]:
    """Items outstanding at the prior close, which may clear this period:
    the previous period's bank_reconciliation.json (its outstanding_items) or
    a CSV of them (date, description, amount, optional entry_id), found at
    rel or beside it with the other extension. No file, none outstanding."""
    rel = prior_reconciliation_file(workspace, rel)
    if rel is None:
        return []
    if rel.lower().endswith(".json"):
        try:
            data = json.loads(resolve(workspace, rel).read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise ToolError(f"{rel} is not valid JSON: {exc}") from None
        items = data.get("outstanding_items") if isinstance(data, dict) else data
        if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
            raise ToolError(f"{rel}: expected an outstanding_items list")
    else:
        items = read_rows(workspace, rel, ["date", "description", "amount"])
    return [BookItem(str(i.get("entry_id") or f"prior-{n}"), parse_date(i.get("date")),
                     str(i.get("description", "")), money(i.get("amount")), prior=True)
            for n, i in enumerate(items, start=1)]


def _batch(target: Decimal, pool: list[int], amount_of: Callable[[int], Decimal],
           gap_of: Callable[[int], int]) -> list[int] | None:
    """The fewest lines (2..MAX_BATCH) among the nearest BATCH_POOL, all with
    the target's sign, that sum exactly to it; ties go to the closest dates."""
    pool = sorted((i for i in pool if amount_of(i) != 0 and (amount_of(i) > 0) == (target > 0)),
                  key=gap_of)[:BATCH_POOL]
    for size in range(2, min(MAX_BATCH, len(pool)) + 1):
        best = None
        for combo in combinations(pool, size):
            if sum((amount_of(i) for i in combo), ZERO) == target:
                score = sum(gap_of(i) for i in combo)
                if best is None or score < best[0]:
                    best = (score, combo)
        if best:
            return list(best[1])
    return None


def reconcile(statement: dict[str, Any], gl: list[GLLine], cash_account: str, *,
              period_start: date, period_end: date, date_window: int = DEFAULT_DATE_WINDOW,
              prior_outstanding: list[BookItem] = ()) -> dict[str, Any]:
    """Match the period's bank lines to book cash movements and compute the
    adjusted bank and book balances.

    Book items are GL cash lines dated in the period plus the items left
    outstanding at the prior close. A bank line clears a book item of the
    same amount (closest date within the window; a prior item may clear any
    time after its date), then a batch of book items that sum to it (one
    deposit of several receipts), and a book item may be cleared by a batch
    of bank lines from one payer (one entry for several payouts). Unmatched book items are
    outstanding (deposits in transit / outstanding payments); unmatched bank
    lines are unrecorded in the books (fees, interest) and need an entry.
    Bank lines outside the period belong to another close.
    """
    cash = [ln for ln in gl if ln.account == cash_account]
    if not cash:
        raise ToolError(f"no GL lines for cash account {cash_account}")
    if statement["ending"] is None:
        raise ToolError("statement needs a running balance column to reconcile")
    bank = [b for b in statement["lines"] if period_start <= b.date <= period_end]
    if not bank:
        raise ToolError(f"statement has no lines between {period_start} and {period_end}")
    for p in prior_outstanding:
        if p.date >= period_start:
            raise ToolError(f"prior outstanding item {p.entry_id} is dated {p.date}, inside the period")
    book = [BookItem(g.entry_id, g.date, g.description, g.net)
            for g in cash if period_start <= g.date <= period_end] + list(prior_outstanding)
    window = timedelta(days=int(date_window))

    def fits(b: BankLine, k: BookItem) -> bool:
        return b.date >= k.date - window if k.prior else abs(k.date - b.date) <= window

    def gap(b: BankLine, k: BookItem) -> int:
        return abs((k.date - b.date).days)

    bank_used: set[int] = set()
    book_used: set[int] = set()
    groups: list[tuple[list[int], list[int]]] = []
    for bi, b in enumerate(bank):
        best = min((ki for ki, k in enumerate(book)
                    if ki not in book_used and k.amount == b.amount and fits(b, k)),
                   key=lambda ki: gap(b, book[ki]), default=None)
        if best is not None:
            bank_used.add(bi)
            book_used.add(best)
            groups.append(([bi], [best]))
    for bi, b in enumerate(bank):
        if bi in bank_used:
            continue
        found = _batch(b.amount, [ki for ki, k in enumerate(book) if ki not in book_used and fits(b, k)],
                       lambda ki: book[ki].amount, lambda ki: gap(b, book[ki]))
        if found:
            bank_used.add(bi)
            book_used.update(found)
            groups.append(([bi], found))
    for ki, k in enumerate(book):
        if ki in book_used:
            continue
        # Split payouts come from one payer, so the bank lines share their
        # first word; unrelated fees never add up to a recorded payment.
        payers: dict[str, list[int]] = {}
        for bi, b in enumerate(bank):
            if bi not in bank_used and fits(b, k):
                payers.setdefault((b.description.split() or [""])[0].lower(), []).append(bi)
        for pool in payers.values():
            found = _batch(k.amount, pool, lambda bi: bank[bi].amount, lambda bi: gap(bank[bi], k))
            if found:
                book_used.add(ki)
                bank_used.update(found)
                groups.append((found, [ki]))
                break

    gl_only = [k for ki, k in enumerate(book) if ki not in book_used]
    bank_only = [b for bi, b in enumerate(bank) if bi not in bank_used]
    statement_opening = bank[0].balance - bank[0].amount
    statement_ending = bank[-1].balance
    gl_opening = sum((ln.net for ln in cash if ln.date < period_start), ZERO)
    gl_ending = sum((ln.net for ln in cash if ln.date <= period_end), ZERO)
    prior_total = sum((p.amount for p in prior_outstanding), ZERO)
    outstanding = sum((k.amount for k in gl_only), ZERO)
    unrecorded = sum((b.amount for b in bank_only), ZERO)
    adjusted_bank = statement_ending + outstanding
    adjusted_book = gl_ending + unrecorded
    return {
        "account": cash_account,
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "statement_opening_balance": fmt(statement_opening),
        "statement_ending_balance": fmt(statement_ending),
        "gl_opening_balance": fmt(gl_opening),
        "prior_outstanding_total": fmt(prior_total),
        # statement opening + items outstanding at the prior close - GL opening:
        # non-zero means the prior reconciliation did not carry forward cleanly
        "opening_difference": fmt(statement_opening + prior_total - gl_opening),
        "gl_ending_balance": fmt(gl_ending),
        "matched_count": len(bank_used),
        "batched_matches": [
            {"bank_rows": [bank[i].index for i in b_idx], "entries": [book[i].entry_id for i in k_idx],
             "amount": fmt(sum((bank[i].amount for i in b_idx), ZERO))}
            for b_idx, k_idx in groups if len(b_idx) > 1 or len(k_idx) > 1],
        "prior_items_cleared": [
            {"entry_id": book[k_idx[0]].entry_id, "date": book[k_idx[0]].date.isoformat(),
             "amount": fmt(book[k_idx[0]].amount), "cleared_by_rows": [bank[i].index for i in b_idx]}
            for b_idx, k_idx in groups if len(k_idx) == 1 and book[k_idx[0]].prior],
        "outstanding_items": [
            {"entry_id": k.entry_id, "date": k.date.isoformat(), "description": k.description,
             "amount": fmt(k.amount), "type": "deposit_in_transit" if k.amount > 0 else "outstanding_payment",
             "age_days": (period_end - k.date).days, "carried_from_prior": k.prior}
            for k in gl_only],
        "unrecorded_items": [
            {"row": b.index, "date": b.date.isoformat(), "description": b.description,
             "amount": fmt(b.amount), "type": "bank_credit" if b.amount > 0 else "bank_charge"}
            for b in bank_only],
        "total_outstanding": fmt(outstanding),
        "total_unrecorded": fmt(unrecorded),
        "adjusted_bank_balance": fmt(adjusted_bank),
        "adjusted_book_balance": fmt(adjusted_book),
        "unexplained_difference": fmt(adjusted_bank - adjusted_book),
        "statement_lines_outside_period": len(statement["lines"]) - len(bank),
        "statement_breaks": statement["breaks"],
    }


def load_rules(workspace: Path, rel: str) -> list[dict[str, Any]]:
    rules = []
    for r in read_rows(workspace, rel, ["pattern", "account", "confidence"]):
        try:
            regex = re.compile(r["pattern"], re.I)
        except re.error as exc:
            raise ToolError(f"{rel}: bad pattern {r['pattern']!r}: {exc}") from None
        try:
            confidence = float(r["confidence"] or 0)
        except ValueError:
            raise ToolError(f"{rel}: confidence {r['confidence']!r} is not a number") from None
        rules.append({"pattern": r["pattern"], "regex": regex, "account": r["account"],
                      "confidence": confidence})
    return rules


def categorize(lines: list[BankLine], rules: list[dict[str, Any]], min_confidence: float) -> list[dict[str, Any]]:
    """First matching rule wins; no match -> uncategorized, confidence 0."""
    out = []
    for line in lines:
        rule = next((r for r in rules if r["regex"].search(line.description)), None)
        confidence = rule["confidence"] if rule else 0.0
        out.append({
            "row": str(line.index), "date": line.date.isoformat(), "description": line.description,
            "amount": fmt(line.amount), "account": rule["account"] if rule else "",
            "confidence": f"{confidence:.2f}", "rule": rule["pattern"] if rule else "",
            "rationale": f"matched rule /{rule['pattern']}/" if rule else "no rule matched",
            "needs_review": "yes" if confidence < min_confidence else "no",
        })
    return out


def flux(current: dict[str, Decimal], prior: dict[str, Decimal], *, threshold_abs: Decimal,
         threshold_pct: Decimal, rule: str = "all") -> list[dict[str, Any]]:
    """Variance per account. With rule "all" a change is flagged when it is at
    least threshold_abs and at least threshold_pct (a change from zero counts
    as any percentage); with "any", when it passes either threshold."""
    rows = []
    for account in sorted(set(current) | set(prior)):
        cur, pri = current.get(account, ZERO), prior.get(account, ZERO)
        change = cur - pri
        pct = None if pri == 0 else (change / abs(pri) * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
        big = abs(change) >= threshold_abs
        if rule == "any":
            flagged = big or (pct is not None and abs(pct) >= threshold_pct)
        else:
            flagged = big and (pct is None or abs(pct) >= threshold_pct)
        rows.append({"account": account, "prior": fmt(pri), "current": fmt(cur), "change": fmt(change),
                     "change_pct": "" if pct is None else f"{pct}", "flagged": "yes" if flagged else "no"})
    return rows


def flux_table(workspace: Path, *, current: str, prior_trial_balance: str, prior_pl: str | None,
               gl: str, journal_entries: str | None, chart_of_accounts: str, period: str,
               threshold_abs: Decimal, threshold_pct: Decimal, rule: str = "all") -> dict[str, Any]:
    """Flux rows that compare like with like. Balance-sheet accounts: the
    period-end balance in the current trial balance against the prior
    closing trial balance. Income-statement accounts: this period's activity
    (GL lines and draft entries dated in the period) against the prior
    month's P&L, never cumulative balances, so a mid-year month is not
    compared with year-to-date figures. Without a prior-month P&L the
    income statement is not compared, and the notes say so."""
    coa = load_coa(workspace, chart_of_accounts)
    start, end = period_bounds(period)
    current_tb = load_balances(workspace, current)
    prior_tb = load_balances(workspace, prior_trial_balance)
    have_pl = exists(workspace, prior_pl)
    prior_month = load_balances(workspace, prior_pl) if have_pl else {}
    unknown = sorted((set(current_tb) | set(prior_tb) | set(prior_month)) - set(coa))
    if unknown:
        raise ToolError(f"accounts missing from the chart of accounts: {', '.join(unknown)}")

    def is_pl(account: str) -> bool:
        return coa[account]["type"] in PL_TYPES

    def labelled(rows: list[dict[str, Any]], statement: str, basis: str) -> list[dict[str, Any]]:
        return [{**r, "name": coa[r["account"]]["name"], "statement": statement, "basis": basis} for r in rows]

    thresholds = {"threshold_abs": threshold_abs, "threshold_pct": threshold_pct, "rule": rule}
    rows = labelled(flux({a: b for a, b in current_tb.items() if not is_pl(a)},
                         {a: b for a, b in prior_tb.items() if not is_pl(a)}, **thresholds),
                    "balance_sheet", "balance at period end")
    notes = []
    if have_pl:
        activity: dict[str, Decimal] = {}
        for line in [*load_gl(workspace, gl), *journal_as_gl(load_journal(workspace, journal_entries))]:
            if start <= line.date <= end and line.account in coa and is_pl(line.account):
                activity[line.account] = activity.get(line.account, ZERO) + line.net
        rows += labelled(flux(activity, {a: b for a, b in prior_month.items() if is_pl(a)}, **thresholds),
                         "income_statement", "activity for the month")
    else:
        notes.append(f"no prior-month P&L at {prior_pl}: income-statement accounts were not compared; "
                     "ask the client for last month's P&L (that month only, not year to date)")
    return {"rows": rows, "notes": notes}


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
        try:
            months = int(r["months"] or 1)
        except ValueError:
            raise ToolError(f"{rel}: item {r['item_id']} months {r['months']!r} is not a whole number") from None
        if months < 1:
            raise ToolError(f"{rel}: item {r['item_id']} needs months >= 1")
        elapsed = months_elapsed(parse_date(r["start_date"]), period_end, months)
        due = total if elapsed == months else (total * elapsed / months).quantize(CENT, rounding=ROUND_HALF_UP)
        out.append({**{k: r[k] for k in ("item_id", "description", "pl_account",
                                         "balance_account", "support")},
                    "type": kind, "total_amount": fmt(total), "months": months, "months_elapsed": elapsed,
                    "due_through_period": fmt(due), "booked_to_date": fmt(booked),
                    "this_period": fmt(due - booked), "remaining_after": fmt(total - due)})
    return out


def schedule_entry(row: dict[str, Any]) -> tuple[str, str, Decimal] | None:
    """(debit account, credit account, amount) of the entry a schedule row
    needs this period, or None when nothing is due. Revenue deferrals
    release the liability; everything else recognizes expense against the
    balance-sheet account; a negative amount reverses the direction."""
    amount = money(row["this_period"])
    if amount == 0:
        return None
    dr, cr = ((row["balance_account"], row["pl_account"]) if row["type"] == "deferred_revenue"
              else (row["pl_account"], row["balance_account"]))
    return (cr, dr, -amount) if amount < 0 else (dr, cr, amount)


def cites(support: str, item_id: str) -> bool:
    """Whether support text names a schedule item (PRE-001, not PRE-0010)."""
    return re.search(rf"(?<![\w-]){re.escape(item_id)}(?![\w-])", support or "") is not None


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
    if not match or not 1 <= int(match[2]) <= 12:
        raise ToolError("period must be YYYY-MM")
    year, month = int(match[1]), int(match[2])
    start = date(year, month, 1)
    end = date(year + (month == 12), month % 12 + 1, 1)
    return start, date.fromordinal(end.toordinal() - 1)


def period_end_of(workspace: Path, period: str = "") -> date | None:
    """End of the period given, else of close_parameters.json's, else None."""
    chosen = setting(workspace, period, "period")
    return period_bounds(chosen)[1] if chosen else None


# --- tools ----------------------------------------------------------------------

def parse_bank_statement(workspace: Path, *, fetch=None, run=None, resolve_path=None,
                         path: str = "inputs/bank_statement.csv", **_: Any) -> dict:
    gate(workspace, resolve_path, reads=[path])
    s = load_statement(Path(workspace), path)
    return {"rows": len(s["lines"]), "first_date": s["start"].isoformat(), "last_date": s["end"].isoformat(),
            "order": "newest first (read in reverse)" if s["newest_first"] else "oldest first",
            "opening_balance": fmt(s["opening"]) if s["opening"] is not None else None,
            "ending_balance": fmt(s["ending"]) if s["ending"] is not None else None,
            "total_deposits": fmt(s["deposits"]), "total_withdrawals": fmt(s["withdrawals"]),
            "net_activity": fmt(s["net"]), "running_balance_breaks": s["breaks"]}


def categorize_transactions(workspace: Path, *, fetch=None, run=None, resolve_path=None,
                            statement: str = "inputs/bank_statement.csv",
                            rules: str = "inputs/categorization_rules.csv",
                            output: str = "deliverables/m2-period-close/categorized.csv",
                            min_confidence: float = 0.8, **_: Any) -> dict:
    """Rules first. A re-run keeps every line already decided by hand (its
    rationale is not one this tool writes)."""
    workspace = Path(workspace)
    gate(workspace, resolve_path, reads=[statement, rules], writes=[output])
    s = load_statement(workspace, statement)
    have_rules = exists(workspace, rules)
    rows = categorize(s["lines"], load_rules(workspace, rules) if have_rules else [], float(min_confidence))
    decided = {(r["row"], r["date"], r["description"], r["amount"]): r
               for r in existing_rows(workspace, output, CATEGORIZED_COLUMNS)
               if r["rationale"] and not TOOL_RATIONALE_RE.fullmatch(r["rationale"])}
    kept = 0
    for i, r in enumerate(rows):
        old = decided.get((r["row"], r["date"], r["description"], r["amount"]))
        if old:
            rows[i] = {k: old.get(k, v) for k, v in r.items()}
            kept += 1
    write_rows(workspace, output, CATEGORIZED_COLUMNS, rows)
    review = [r for r in rows if r["needs_review"] == "yes"]
    out = {"output": output, "rows": len(rows), "total": fmt(s["net"]), "kept_decisions": kept,
           "needs_review": len(review), "uncategorized": sum(1 for r in rows if not r["account"]),
           "review_rows": [{"row": r["row"], "description": r["description"], "amount": r["amount"]}
                           for r in review]}
    if not have_rules:
        out["note"] = f"no rulebook at {rules}: decide every line yourself or queue it"
    return out


def find_exceptions(workspace: Path, *, fetch=None, run=None, resolve_path=None,
                    categorized: str = "deliverables/m2-period-close/categorized.csv",
                    gl: str = "inputs/gl_detail.csv", chart_of_accounts: str = "inputs/chart_of_accounts.csv",
                    output: str = "deliverables/m2-period-close/exceptions.csv", period: str = "",
                    round_dollar_min: str = "1000", duplicate_days: int = 3, **_: Any) -> dict:
    """Exceptions queue: items needing review, likely duplicates, large
    round-dollar movements and every GL line through period end sitting in a
    suspense account. A re-run keeps each resolution and every row added by
    hand, under the same ids."""
    workspace = Path(workspace)
    gate(workspace, resolve_path, reads=[categorized, gl, chart_of_accounts], writes=[output])
    cat = read_rows(workspace, categorized, ["row", "date", "description", "amount", "account", "needs_review"])
    coa = load_coa(workspace, chart_of_accounts)
    suspense = suspense_accounts(coa)
    cutoff = period_end_of(workspace, period)
    round_min = money(round_dollar_min)
    items: list[dict[str, str]] = []

    def add(source, ref, day, desc, amount, account, reason):
        items.append({"source": source, "ref": ref, "date": day, "description": desc, "amount": amount,
                      "account": account, "reason": reason, "resolution": ""})

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
        if line.account in suspense and (cutoff is None or line.date <= cutoff):
            add("gl", line.entry_id, line.date.isoformat(), line.description, fmt(line.net), line.account,
                f"balance in suspense account {line.account} ({coa[line.account]['name']})")

    def key(row: dict[str, str]) -> tuple[str, ...]:
        return row["source"], row["ref"], row["account"], row["amount"], row["reason"]

    earlier = existing_rows(workspace, output, EXCEPTION_COLUMNS)
    fresh: dict[tuple[str, ...], list[dict[str, str]]] = {}
    for item in items:
        fresh.setdefault(key(item), []).append(item)
    numbers = [int(m[1]) for e in earlier if (m := re.fullmatch(r"EX-(\d+)", e["id"]))]
    next_id = max(numbers, default=0) + 1
    merged = []
    for old in earlier:
        same = fresh.get(key(old))
        merged.append({**same.pop(0), "id": old["id"], "resolution": old["resolution"]} if same else old)
    for item in (i for group in fresh.values() for i in group):
        merged.append({**item, "id": f"EX-{next_id:03d}"})
        next_id += 1
    write_rows(workspace, output, EXCEPTION_COLUMNS, merged)
    return {"output": output, "exceptions": len(merged), "suspense_accounts": sorted(suspense),
            "kept_from_earlier_run": len(earlier), "items": merged[:50]}


def reconcile_bank(workspace: Path, *, fetch=None, run=None, resolve_path=None,
                   statement: str = "inputs/bank_statement.csv",
                   gl: str = "inputs/gl_detail.csv", cash_account: str = "", period: str = "",
                   prior_reconciliation: str = PRIOR_RECONCILIATION, date_window: int | None = None,
                   output: str = "deliverables/m2-period-close/bank_reconciliation.json", **_: Any) -> dict:
    workspace = Path(workspace)
    gate(workspace, resolve_path, reads=[statement, gl, prior_reconciliation], writes=[output])
    cash_account = str(setting(workspace, cash_account, "cash_account", ""))
    if not cash_account:
        raise ToolError("cash_account is required (the GL account for this bank account)")
    s = load_statement(workspace, statement)
    period = setting(workspace, period, "period")
    start, end = period_bounds(period) if period else (s["start"], s["end"])
    window = int(setting(workspace, date_window, "date_window", DEFAULT_DATE_WINDOW))
    prior = load_prior_outstanding(workspace, prior_reconciliation)
    rec = reconcile(s, load_gl(workspace, gl), cash_account, period_start=start, period_end=end,
                    date_window=window, prior_outstanding=prior)
    rec["date_window_days"] = window
    rec["prior_reconciliation"] = prior_reconciliation_file(workspace, prior_reconciliation)
    write_json(workspace, output, rec)
    return {"output": output, **{k: rec[k] for k in (
        "statement_ending_balance", "gl_ending_balance", "matched_count", "total_outstanding",
        "total_unrecorded", "adjusted_bank_balance", "adjusted_book_balance", "unexplained_difference",
        "opening_difference", "prior_reconciliation", "statement_lines_outside_period")},
        "outstanding_items": rec["outstanding_items"], "unrecorded_items": rec["unrecorded_items"],
        "batched_matches": rec["batched_matches"], "prior_items_cleared": rec["prior_items_cleared"],
        "statement_breaks": rec["statement_breaks"]}


def _append_entry(workspace: Path, output: str, rows: list[dict[str, Any]]) -> None:
    path = resolve(workspace, output, write=True)
    existing = read_rows(workspace, output, JE_COLUMNS) if path.is_file() else []
    ids = {r["entry_id"] for r in existing}
    if rows[0]["entry_id"] in ids:
        raise ToolError(f"entry {rows[0]['entry_id']} already exists in {output}")
    write_rows(workspace, output, JE_COLUMNS, existing + rows)


def draft_journal_entry(workspace: Path, *, fetch=None, run=None, resolve_path=None, entry_id: str = "",
                        date: str = "", description: str = "", lines: list | None = None, support: str = "",
                        output: str = "deliverables/m2-period-close/journal_entries.csv",
                        chart_of_accounts: str = "inputs/chart_of_accounts.csv",
                        gl: str = "inputs/gl_detail.csv", **_: Any) -> dict:
    """Append one balanced draft entry. Refuses unbalanced entries, entries
    without support, dates outside the period being closed, plug-like
    descriptions and suspense accounts (except a reclass out of suspense
    that cites the client's answer)."""
    workspace = Path(workspace)
    gate(workspace, resolve_path, reads=[chart_of_accounts, gl], writes=[output])
    if not entry_id or not description or not lines:
        raise ToolError("entry_id, description and at least two lines are required")
    if not str(support).strip():
        raise ToolError("support is required: cite the document, schedule row or GL query behind the entry")
    if PLUG_WORDS_RE.search(description):
        raise ToolError("description reads like a plug; draft only entries you can support")
    day = parse_date(date)
    period = setting(workspace, "", "period")
    if period:
        start, end = period_bounds(period)
        if not start <= day <= end:
            raise ToolError(f"date {day} is outside the period being closed ({period}); "
                            "entries for another period belong to that close")
    coa = load_coa(workspace, chart_of_accounts)
    out, debits, credits = [], ZERO, ZERO
    for line in lines:
        if not isinstance(line, dict):
            raise ToolError("each line needs an account and exactly one of debit or credit")
        account = str(line.get("account", ""))
        if account not in coa:
            raise ToolError(f"unknown account {account!r}")
        debit, credit = money(line.get("debit", "")), money(line.get("credit", ""))
        if debit < 0 or credit < 0 or (debit and credit) or not (debit or credit):
            raise ToolError("each line needs exactly one positive debit or credit")
        debits, credits = debits + debit, credits + credit
        out.append({"entry_id": entry_id, "date": day.isoformat(), "account": account,
                    "description": line.get("description") or description,
                    "debit": fmt(debit) if debit else "", "credit": fmt(credit) if credit else "",
                    "support": support, "status": DRAFT_STATUS})
    if len(out) < 2 or debits != credits:
        raise ToolError(f"entry does not balance: debits {fmt(debits)} vs credits {fmt(credits)}")
    suspense = suspense_accounts(coa)
    if suspense & {r["account"] for r in out}:
        journal = load_journal(workspace, output) + [
            {**r, "debit_d": money(r["debit"]), "credit_d": money(r["credit"]), "date_d": day} for r in out]
        cutoff = period_bounds(period)[1] if period else None
        problems = suspense_problems(balances_from(load_gl(workspace, gl), as_of=cutoff), journal, suspense)
        if problems:
            raise ToolError("; ".join(f"{', '.join(ids)} {problem}" for ids, problem in problems))
    _append_entry(workspace, output, out)
    return {"output": output, "entry_id": entry_id, "lines": len(out), "amount": fmt(debits),
            "status": DRAFT_STATUS}


def build_accrual_schedule(workspace: Path, *, fetch=None, run=None, resolve_path=None,
                           schedule: str = "inputs/accrual_schedule.csv", period: str = "",
                           output: str = "deliverables/m2-period-close/accrual_schedule.csv",
                           journal_output: str = "deliverables/m2-period-close/journal_entries.csv",
                           draft_entries: bool = True, **_: Any) -> dict:
    """Roll the prepaid / accrual / deferred revenue / depreciation schedule
    to period end and draft one entry per item with a this-period amount.
    Re-running it leaves entries it already drafted alone; with no schedule
    from the client it writes an empty one."""
    workspace = Path(workspace)
    gate(workspace, resolve_path, reads=[schedule],
         writes=[output, journal_output] if draft_entries else [output])
    _, end = period_bounds(setting(workspace, period, "period"))
    if not exists(workspace, schedule):
        write_rows(workspace, output, SCHEDULE_COLUMNS, [])
        return {"output": output, "items": 0, "journal_output": journal_output, "drafted": [],
                "note": f"no schedule at {schedule}: nothing to roll forward; ask the client whether "
                        "they have prepaids, accruals, deferred revenue or fixed assets"}
    rows = schedule_rows(workspace, schedule, end)
    write_rows(workspace, output, SCHEDULE_COLUMNS, rows)
    drafted, already = [], []
    existing: dict[str, list[tuple[str, str, str]]] = {}
    for r in load_journal(workspace, journal_output) if draft_entries else []:
        existing.setdefault(r["entry_id"], []).append((r["account"], fmt(r["debit_d"]), fmt(r["credit_d"])))
    for r in rows if draft_entries else []:
        spec = schedule_entry(r)
        if spec is None:
            continue
        dr, cr, amount = spec
        entry_id = f"ADJ-{end:%Y%m}-{r['item_id']}"
        if entry_id in existing:
            if sorted(existing[entry_id]) != sorted([(dr, fmt(amount), "0.00"), (cr, "0.00", fmt(amount))]):
                raise ToolError(f"{entry_id} already exists in {journal_output} with different lines; "
                                "correct or remove it before re-running")
            already.append(entry_id)
            continue
        draft_journal_entry(workspace, entry_id=entry_id, date=end.isoformat(),
                            description=f"{r['type'].replace('_', ' ')}: {r['description']}",
                            lines=[{"account": dr, "debit": fmt(amount)}, {"account": cr, "credit": fmt(amount)}],
                            support=f"{schedule}#{r['item_id']}; {r['support']}".strip("; "),
                            output=journal_output)
        drafted.append({"entry_id": entry_id, "amount": fmt(amount)})
    return {"output": output, "items": len(rows), "journal_output": journal_output,
            "drafted": drafted, "already_drafted": already, "rows": rows}


def build_trial_balance(workspace: Path, *, fetch=None, run=None, resolve_path=None,
                        gl: str = "inputs/gl_detail.csv",
                        journal_entries: str | None = "deliverables/m2-period-close/journal_entries.csv",
                        chart_of_accounts: str = "inputs/chart_of_accounts.csv", as_of: str = "",
                        period: str = "",
                        output: str = "deliverables/m2-period-close/trial_balance.csv", **_: Any) -> dict:
    """Adjusted trial balance as of period end (as_of, else the end of the
    period being closed): later GL lines and drafts belong to the next close."""
    workspace = Path(workspace)
    gate(workspace, resolve_path, reads=[gl, journal_entries, chart_of_accounts], writes=[output])
    gl_lines = load_gl(workspace, gl)
    journal = load_journal(workspace, journal_entries)
    coa = load_coa(workspace, chart_of_accounts)
    cutoff = parse_date(as_of) if as_of else period_end_of(workspace, period)
    bad = unbalanced_entries([(g.entry_id, g.debit, g.credit) for g in gl_lines] +
                             [(r["entry_id"], r["debit_d"], r["credit_d"]) for r in journal])
    balances = balances_from(gl_lines, journal, as_of=cutoff)
    rows = [{"account": a, "name": coa.get(a, {}).get("name", "UNKNOWN ACCOUNT"),
             "debit": fmt(b) if b > 0 else "", "credit": fmt(-b) if b < 0 else ""}
            for a, b in sorted(balances.items()) if b != 0]
    debits = sum((money(r["debit"]) for r in rows), ZERO)
    credits = sum((money(r["credit"]) for r in rows), ZERO)
    write_rows(workspace, output, TB_COLUMNS, rows)
    later = sorted({ln.entry_id for ln in [*gl_lines, *journal_as_gl(journal)] if cutoff and ln.date > cutoff})
    return {"output": output, "as_of": cutoff.isoformat() if cutoff else None, "accounts": len(rows),
            "total_debits": fmt(debits), "total_credits": fmt(credits),
            "balanced": debits == credits, "unbalanced_entries": {k: fmt(v) for k, v in bad.items()},
            "unknown_accounts": sorted(a for a in balances if a not in coa),
            "entries_after_period_end": later}


def flux_analysis(workspace: Path, *, fetch=None, run=None, resolve_path=None,
                  current: str = "deliverables/m2-period-close/trial_balance.csv",
                  prior_trial_balance: str = "inputs/prior_trial_balance.csv",
                  prior_pl: str = "inputs/prior_month_pl.csv", gl: str = "inputs/gl_detail.csv",
                  journal_entries: str = "deliverables/m2-period-close/journal_entries.csv",
                  chart_of_accounts: str = "inputs/chart_of_accounts.csv", period: str = "",
                  threshold_abs: str = "", threshold_pct: str = "", rule: str = "",
                  output: str = "deliverables/m3-close-package/flux.csv", **_: Any) -> dict:
    """Variance table with a commentary column for the model to fill. A
    re-run keeps the commentary of every row whose amounts did not change."""
    workspace = Path(workspace)
    gate(workspace, resolve_path, reads=[current, prior_trial_balance, prior_pl, gl, journal_entries,
                                         chart_of_accounts], writes=[output])
    rule = str(setting(workspace, rule, "flux_threshold_rule", "all")).lower()
    table = flux_table(workspace, current=current, prior_trial_balance=prior_trial_balance, prior_pl=prior_pl,
                       gl=gl, journal_entries=journal_entries, chart_of_accounts=chart_of_accounts,
                       period=setting(workspace, period, "period"),
                       threshold_abs=money(setting(workspace, threshold_abs, "flux_threshold_abs", "1000")),
                       threshold_pct=percent(setting(workspace, threshold_pct, "flux_threshold_pct", "10")),
                       rule=rule)
    earlier = {(r["account"], r["prior"], r["current"]): r["commentary"]
               for r in existing_rows(workspace, output, ["account", "prior", "current", "commentary"])}
    rows = table["rows"]
    for r in rows:
        r["commentary"] = earlier.get((r["account"], r["prior"], r["current"]), "")
    write_rows(workspace, output, FLUX_COLUMNS, rows)
    flagged = [r for r in rows if r["flagged"] == "yes"]
    return {"output": output, "accounts": len(rows), "flagged": len(flagged), "flagged_rows": flagged,
            "notes": table["notes"], "note": "write commentary for every flagged row; do not change amounts"}


def tie_opening_balances(workspace: Path, *, fetch=None, run=None, resolve_path=None,
                         gl: str = "inputs/gl_detail.csv",
                         prior_trial_balance: str = "inputs/prior_trial_balance.csv", period: str = "",
                         tolerance: str = "1.00",
                         output: str = "deliverables/m1-onboarding/opening_balance_tieout.csv",
                         **_: Any) -> dict:
    workspace = Path(workspace)
    gate(workspace, resolve_path, reads=[gl, prior_trial_balance], writes=[output])
    start, _end = period_bounds(setting(workspace, period, "period"))
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


def build_account_map(workspace: Path, *, fetch=None, run=None, resolve_path=None,
                      chart_of_accounts: str = "inputs/chart_of_accounts.csv", gl: str = "inputs/gl_detail.csv",
                      output: str = "deliverables/m1-onboarding/account_map.csv", **_: Any) -> dict:
    """Map each account to its statement and a default line by type; the
    model may refine the line names afterwards."""
    workspace = Path(workspace)
    gate(workspace, resolve_path, reads=[chart_of_accounts, gl], writes=[output])
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


def build_financial_statements(workspace: Path, *, fetch=None, run=None, resolve_path=None,
                               trial_balance: str = "deliverables/m2-period-close/trial_balance.csv",
                               chart_of_accounts: str = "inputs/chart_of_accounts.csv", period: str = "",
                               output: str = "deliverables/m3-close-package/financial_statements.json",
                               **_: Any) -> dict:
    workspace = Path(workspace)
    gate(workspace, resolve_path, reads=[trial_balance, chart_of_accounts], writes=[output])
    data = statements(load_balances(workspace, trial_balance), load_coa(workspace, chart_of_accounts))
    period = setting(workspace, period, "period")
    if period:
        _, end = period_bounds(period)
        data["period"] = period
        data["balance_sheet"]["as_of"] = end.isoformat()
        data["income_statement"]["basis"] = (f"fiscal year to date through {end.isoformat()}: the P&L balances "
                                             "carried in the adjusted trial balance; the month's activity is "
                                             "in flux.csv")
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
_PERIOD = {"type": "string", "description": "period being closed, YYYY-MM (default: close_parameters.json)"}

TOOL_DEFS: list[dict[str, Any]] = [
    {"name": "parse_bank_statement", "risk": "read", "function": parse_bank_statement,
     "description": "Summarize a bank statement CSV (oldest or newest first): row count, opening and ending "
                    "balance, deposits, withdrawals and any rows where the running balance does not follow "
                    "from the activity.",
     "input_schema": _schema({"path": _PATH})},
    {"name": "categorize_transactions", "risk": "write", "function": categorize_transactions,
     "description": "Apply the categorization rulebook (regex pattern -> account, confidence) to every bank "
                    "line and write categorized.csv. Lines with no rule or low confidence are marked "
                    "needs_review; decide those yourself (write your own rationale) or queue them, never "
                    "guess silently. Re-running keeps the lines you decided.",
     "input_schema": _schema({"statement": _PATH, "rules": _PATH, "output": _OUT,
                              "min_confidence": {"type": "number"}})},
    {"name": "find_exceptions", "risk": "write", "function": find_exceptions,
     "description": "Build the exceptions queue: needs-review and uncategorized bank lines, likely "
                    "duplicates, large round-dollar amounts and every GL line in a suspense or Ask My "
                    "Accountant account. Fill the resolution column only with what the client confirmed. "
                    "Re-running keeps resolutions and rows you added.",
     "input_schema": _schema({"categorized": _PATH, "gl": _PATH, "chart_of_accounts": _PATH, "output": _OUT,
                              "period": _PERIOD, "round_dollar_min": {"type": "string"},
                              "duplicate_days": {"type": "integer"}})},
    {"name": "reconcile_bank", "risk": "write", "function": reconcile_bank,
     "description": "Reconcile the period's bank lines to the GL cash account: match by amount within a "
                    "date window (also batched deposits and split payouts), clear the items outstanding at "
                    "the prior close (from the prior reconciliation), list outstanding and unrecorded items "
                    "and compute the unexplained difference, which must be 0.00. Keep the default window "
                    "unless close_parameters.json sets date_window. Writes bank_reconciliation.json.",
     "input_schema": _schema({"statement": _PATH, "gl": _PATH, "cash_account": {"type": "string"},
                              "period": _PERIOD, "prior_reconciliation": _PATH,
                              "date_window": {"type": "integer"}, "output": _OUT},
                             ["cash_account", "period"])},
    {"name": "draft_journal_entry", "risk": "write", "function": draft_journal_entry,
     "description": "Append one balanced DRAFT journal entry (never posted) to journal_entries.csv, dated "
                    "inside the period. Each line has an account and exactly one of debit or credit; support "
                    "must cite the document, schedule row or reconciliation item behind it. Balancing plugs "
                    "are refused, and so are suspense accounts, except a reclass out of suspense whose "
                    "support cites the client's answer (for example 'client answer 2026-09-03').",
     "input_schema": _schema({"entry_id": {"type": "string"}, "date": {"type": "string"},
                              "description": {"type": "string"}, "support": {"type": "string"},
                              "lines": {"type": "array", "items": {"type": "object", "properties": {
                                  "account": {"type": "string"}, "debit": {"type": "string"},
                                  "credit": {"type": "string"}, "description": {"type": "string"}},
                                  "required": ["account"]}},
                              "output": _OUT, "chart_of_accounts": _PATH, "gl": _PATH},
                             ["entry_id", "date", "description", "lines", "support"])},
    {"name": "build_accrual_schedule", "risk": "write", "function": build_accrual_schedule,
     "description": "Roll the prepaid, accrual, deferred revenue and depreciation schedule to period end "
                    "(straight line: due through period minus booked to date) and draft one entry per "
                    "item with a this-period amount. Safe to re-run: existing entries are left alone.",
     "input_schema": _schema({"schedule": _PATH, "period": _PERIOD, "output": _OUT, "journal_output": _OUT,
                              "draft_entries": {"type": "boolean"}}, ["period"])},
    {"name": "build_trial_balance", "risk": "write", "function": build_trial_balance,
     "description": "Build the trial balance as of period end from the GL export plus the draft entries "
                    "(the adjusted TB), report total debits and credits, unbalanced entries, unknown "
                    "accounts and entries dated after period end (left out).",
     "input_schema": _schema({"gl": _PATH, "journal_entries": _PATH, "chart_of_accounts": _PATH,
                              "as_of": {"type": "string"}, "period": _PERIOD, "output": _OUT})},
    {"name": "flux_analysis", "risk": "write", "function": flux_analysis,
     "description": "Compare balance-sheet balances with the prior closing trial balance and the month's "
                    "income-statement activity with the prior month's P&L; flag changes over the client's "
                    "thresholds. Writes flux.csv with a commentary column: explain every flagged row from "
                    "the underlying activity, without changing the amounts. Re-running keeps commentary.",
     "input_schema": _schema({"current": _PATH, "prior_trial_balance": _PATH, "prior_pl": _PATH, "gl": _PATH,
                              "journal_entries": _PATH, "chart_of_accounts": _PATH, "period": _PERIOD,
                              "threshold_abs": {"type": "string"}, "threshold_pct": {"type": "string"},
                              "rule": {"type": "string", "enum": ["all", "any"]}, "output": _OUT})},
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
     "description": "Produce the income statement (fiscal year to date) and balance sheet (at period end) "
                    "from the adjusted trial balance; the balance sheet difference must be 0.00.",
     "input_schema": _schema({"trial_balance": _PATH, "chart_of_accounts": _PATH, "period": _PERIOD,
                              "output": _OUT})},
]
