"""
specialists/financial_research/checks.py - acceptance checks for the
financial-research specialist.

Each check is `fn(workspace, params, *, run=None) -> {"passed", "details",
"score"}` and is listed in CHECK_DEFS. Checks never trust the agent's own
report: every figure is re-derived from the cached SEC JSON
(.agentkit/edgar/, falling back to inputs/edgar/), the client's market data
and the data-room files themselves. The recomputation here is written
independently of tools.py so a bug in one does not hide in the other.

    xbrl_tieout               every facts.csv row is a real XBRL fact
    comps_tie_to_xbrl         every sourced comps.csv figure ties to an XBRL fact
    comps_recompute           derived comps columns recompute from their inputs
    memo_figures_match        memo figures cite and match comps.csv / the ledger
    no_recommendation_language no buy/sell/hold ratings or price targets
    red_flag_checklist        checklist complete; filing-level hits not missed
    source_inventory_resolves every listed source resolves (accession / hash)
    dataroom_index_complete   the index covers 100% of data-room files
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

SPREADS_PATH = "deliverables/m2-spreads-comps/facts.csv"
COMPS_PATH = "deliverables/m2-spreads-comps/comps.csv"
MARKET_DATA_PATH = "inputs/market_data.csv"
MEMO_PATH = "deliverables/m3-diligence-memo/memo.md"
RED_FLAGS_PATH = "deliverables/m3-diligence-memo/red_flags.md"
SOURCES_PATH = "deliverables/m1-plan-sources/source_inventory.csv"
DATAROOM_INDEX_PATH = "deliverables/m1-plan-sources/dataroom_index.csv"

# Accepted tags per standard metric; a row using any other tag must carry a
# note explaining the choice. Kept in step with tools.METRICS on purpose, but
# declared here so the check does not depend on the tool's selection logic.
METRIC_TAGS: dict[str, tuple[str, list[str]]] = {
    "revenue": ("duration", ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                             "RevenueFromContractWithCustomerIncludingAssessedTax",
                             "SalesRevenueNet"]),
    "cost_of_revenue": ("duration", ["CostOfRevenue", "CostOfGoodsAndServicesSold"]),
    "gross_profit": ("duration", ["GrossProfit"]),
    "operating_income": ("duration", ["OperatingIncomeLoss"]),
    "net_income": ("duration", ["NetIncomeLoss", "ProfitLoss"]),
    "d_and_a": ("duration", ["DepreciationDepletionAndAmortization",
                             "DepreciationAndAmortization",
                             "DepreciationAmortizationAndAccretionNet"]),
    "operating_cash_flow": ("duration", ["NetCashProvidedByUsedInOperatingActivities"]),
    "capex": ("duration", ["PaymentsToAcquirePropertyPlantAndEquipment"]),
    "shares_diluted": ("duration", ["WeightedAverageNumberOfDilutedSharesOutstanding"]),
    "eps_diluted": ("duration", ["EarningsPerShareDiluted"]),
    "cash": ("instant", ["CashAndCashEquivalentsAtCarryingValue",
                         "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"]),
    "long_term_debt": ("instant", ["LongTermDebtNoncurrent", "LongTermDebt"]),
    "short_term_debt": ("instant", ["LongTermDebtCurrent", "DebtCurrent", "ShortTermBorrowings"]),
    "total_assets": ("instant", ["Assets"]),
    "total_equity": ("instant", ["StockholdersEquity",
                                 "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"]),
}

# comps.csv column -> (metric in facts.csv, fiscal-year offset)
COMPS_SOURCED = {"revenue": ("revenue", 0), "revenue_prior": ("revenue", -1),
                 "gross_profit": ("gross_profit", 0), "operating_income": ("operating_income", 0),
                 "net_income": ("net_income", 0), "d_and_a": ("d_and_a", 0), "cash": ("cash", 0),
                 "long_term_debt": ("long_term_debt", 0),
                 "short_term_debt": ("short_term_debt", 0),
                 "shares_diluted": ("shares_diluted", 0)}
MARKET_COLUMNS = ["price", "price_as_of", "minority_interest", "preferred"]
PERCENT_COLUMNS = {"gross_margin", "operating_margin", "ebitda_margin", "revenue_growth"}
MULTIPLE_COLUMNS = {"ev_revenue", "ev_ebitda", "pe"}

RED_FLAG_ITEMS: dict[str, str] = {
    "RF01": "Change in certifying accountant (8-K Item 4.01)",
    "RF02": "Non-reliance on previously issued financial statements (8-K Item 4.02)",
    "RF03": "Amended periodic reports (10-K/A, 10-Q/A)",
    "RF04": "Late filing notices (NT 10-K, NT 10-Q)",
    "RF05": "Going-concern doubt in the audit opinion or notes",
    "RF06": "Material weakness in internal control over financial reporting",
    "RF07": "Related-party transactions",
    "RF08": "Customer or supplier concentration",
    "RF09": "Debt acceleration, default or covenant trigger (8-K Item 2.04)",
    "RF10": "Material litigation or regulatory proceedings",
}
# Items derivable from the filing index alone: (8-K items, forms)
RED_FLAG_AUTO: dict[str, tuple[set[str], set[str]]] = {
    "RF01": ({"4.01"}, set()), "RF02": ({"4.02"}, set()),
    "RF03": (set(), {"10-K/A", "10-Q/A"}), "RF04": (set(), {"NT 10-K", "NT 10-Q"}),
    "RF09": ({"2.04"}, set()),
}
RED_FLAG_STATUSES = {"found", "not_found", "not_applicable", "open"}

ACCESSION_RE = re.compile(r"\b\d{10}-\d{2}-\d{6}\b")
CLAIM_RE = re.compile(r"\[C\d+\]")
FIGURE_REF_RE = re.compile(
    r"(?P<cur>\$)?\s?(?P<num>-?\d[\d,]*(?:\.\d+)?)\s?(?P<suf>%|x|×|bn|billion|mm|million|m|k|"
    r"thousand|b)?\s*\[F:(?P<cik>\d+):(?P<fy>\d{4}):(?P<col>[a-z_]+)\]", re.I)
NUMERIC_CLAIM_RE = re.compile(
    r"\$\s?\d|\d(?:\.\d+)?\s?%|\b\d+(?:\.\d+)?\s?[x×](?![A-Za-z])|"
    r"\b\d[\d,]*(?:\.\d+)?\s?(?:million|billion|bn|mm)\b", re.I)
RECOMMENDATION_PATTERNS = [
    r"\b(?:strong\s+)?(?:buy|sell|hold|accumulate|reduce)\s+rating\b",
    r"\brat(?:e|es|ed|ing)\s+(?:the\s+(?:stock|shares|company)|it|(?:its\s+)?shares)\s+"
    r"(?:as\s+)?(?:an?\s+)?(?:strong\s+)?"
    r"(?:buy|sell|hold|outperform|underperform|overweight|underweight)\b",
    r"\b(?:outperform|underperform|overweight|underweight|market\s+perform)\s+rating\b",
    r"\bprice\s+target\b|\btarget\s+price\b",
    r"\b(?:we|i)\s+(?:would\s+)?(?:recommend|advise|suggest|urge)\s+(?:that\s+\w+\s+)?"
    r"(?:buy|buying|sell|selling|purchas\w*|short\w*|invest\w*|accumulat\w*|exit\w*)\b",
    r"\b(?:initiat\w+\s+coverage|upgrade\w*\s+to\s+(?:buy|outperform|overweight)|"
    r"downgrade\w*\s+to\s+(?:sell|underperform|underweight))\b",
    r"\b(?:is|looks|appears)\s+(?:like\s+)?(?:a\s+)?(?:compelling\s+|clear\s+)?(?:buy|sell)\b(?!-)",
]
DISCLAIMER_MARKERS = ("not investment advice", "not a recommendation", "does not constitute")


def _result(passed: bool | None, details: str, score: float | None = None) -> dict[str, Any]:
    return {"passed": passed, "details": details, "score": score}


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _cik10(value: Any) -> str:
    digits = re.sub(r"\D", "", str(value))
    return digits.zfill(10) if digits and len(digits) <= 10 else ""


def _cik_short(value: Any) -> str:
    c = _cik10(value)
    return str(int(c)) if c else ""


def _source_json(workspace: Path, kind: str, cik: Any) -> dict | None:
    """SEC JSON as fetched by the tools (.agentkit/edgar/) or supplied (inputs/edgar/)."""
    c = _cik10(cik)
    if not c:
        return None
    for base in (".agentkit/edgar", "inputs/edgar"):
        path = Path(workspace) / base / kind / f"CIK{c}.json"
        if path.is_file():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                return None
    return None


def _num(value: Any) -> Decimal | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return Decimal(str(value).strip().replace(",", ""))
    except InvalidOperation:
        return None


def _inside(workspace: Path, rel: str) -> Path | None:
    root = Path(workspace).resolve()
    target = (root / rel).resolve()
    return target if root in target.parents else None


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _find_fact(workspace: Path, row: dict[str, str], cache: dict, tolerance: Decimal) -> str:
    """'' when the spreads row is a real XBRL fact, else the reason it is not."""
    cik = _cik10(row.get("cik"))
    if cik not in cache:
        cache[cik] = _source_json(workspace, "companyfacts", cik)
    facts = cache[cik]
    if facts is None:
        return f"no companyfacts source for CIK {row.get('cik')}"
    taxonomy = row.get("taxonomy") or "us-gaap"
    records = ((((facts.get("facts") or {}).get(taxonomy) or {}).get(row.get("tag", "")) or {})
               .get("units") or {}).get(row.get("unit", ""))
    if not records:
        return f"tag {taxonomy}:{row.get('tag')} [{row.get('unit')}] not reported"
    value = _num(row.get("value"))
    if value is None:
        return "value is not a number"
    start = row.get("period_start") or None
    same_period = [r for r in records if r.get("end") == row.get("period_end")
                   and r.get("start") == start]
    if not same_period:
        return f"no fact for period {start or ''}..{row.get('period_end')}"
    same_filing = [r for r in same_period if r.get("accn") == row.get("accession")]
    if not same_filing:
        return f"period exists but not in accession {row.get('accession')}"
    if not any(abs(Decimal(str(r.get("val"))) - value) <= tolerance for r in same_filing):
        reported = ", ".join(str(r.get("val")) for r in same_filing)
        return f"value {row.get('value')} != reported {reported}"
    return ""


def _shape_problem(row: dict[str, str]) -> str:
    """Spreads-row sanity that the source cannot tell us: annual period, right tag."""
    metric = row.get("metric", "")
    kind, tags = METRIC_TAGS.get(metric, ("", []))
    end = row.get("period_end", "")
    try:
        if str(int(row.get("fiscal_year", "0"))) != end[:4]:
            return "fiscal_year is not the calendar year of period_end"
        if kind == "duration":
            days = (date.fromisoformat(end) - date.fromisoformat(row.get("period_start", ""))).days
            if not 350 <= days <= 380:
                return f"duration of {days} days is not a fiscal year"
        elif kind == "instant" and row.get("period_start"):
            return "balance-sheet metric must be an instant (no period_start)"
    except ValueError:
        return "bad period dates"
    if tags and row.get("tag") not in tags and not row.get("note", "").strip():
        return f"tag {row.get('tag')} is not a standard tag for {metric} and has no note"
    return ""


# --- M2: spreads and comps -------------------------------------------------------

def xbrl_tieout(workspace: Path, params: dict, *, run=None) -> dict:
    """Every facts.csv row matches an XBRL fact (tag, unit, period, accession, value).

    params: path (default facts.csv), min_match_ratio (default 1.0; rows with a
    written `note` may be unmatched, e.g. a disclosed restatement), tolerance
    (absolute, default 0.5 for rounding).
    """
    path = Path(workspace) / params.get("path", SPREADS_PATH)
    if not path.is_file():
        return _result(False, f"{params.get('path', SPREADS_PATH)} not found", 0.0)
    rows = _read_csv(path)
    if not rows:
        return _result(False, "spreads file has no rows", 0.0)
    tolerance = Decimal(str(params.get("tolerance", "0.5")))
    cache: dict = {}
    matched, explained, failures = 0, 0, []
    for i, row in enumerate(rows, start=2):
        problem = _shape_problem(row) or _find_fact(workspace, row, cache, tolerance)
        if not problem:
            matched += 1
        elif row.get("note", "").strip():
            explained += 1
        else:
            failures.append(f"line {i} ({row.get('cik')} {row.get('metric')} "
                            f"FY{row.get('fiscal_year')}): {problem}")
    ratio = matched / len(rows)
    passed = not failures and ratio >= float(params.get("min_match_ratio", 1.0))
    details = (f"{matched}/{len(rows)} rows tie to XBRL; {explained} unmatched with notes; "
               f"{len(failures)} unexplained")
    if failures:
        details += ": " + "; ".join(failures[:10])
    return _result(passed, details, round(ratio, 4))


def comps_tie_to_xbrl(workspace: Path, params: dict, *, run=None) -> dict:
    """Every sourced figure in comps.csv ties to an XBRL fact (tag, period, value).

    Each XBRL column must match its facts.csv row, and that row must itself be
    a real fact in the SEC source; market columns must match the client's
    market data file. params: comps, spreads, market_data, required (columns
    that may not be blank; default revenue, operating_income, net_income,
    shares_diluted).
    """
    comps_rel = params.get("comps", COMPS_PATH)
    comps_path = Path(workspace) / comps_rel
    spreads_path = Path(workspace) / params.get("spreads", SPREADS_PATH)
    market_path = Path(workspace) / params.get("market_data", MARKET_DATA_PATH)
    for p in (comps_path, spreads_path, market_path):
        if not p.is_file():
            return _result(False, f"{p.relative_to(workspace).as_posix()} not found", 0.0)
    required = set(params.get("required", ["revenue", "operating_income", "net_income",
                                            "shares_diluted"]))
    spreads = {(_cik_short(r.get("cik")), r.get("fiscal_year", ""), r.get("metric", "")): r
               for r in _read_csv(spreads_path)}
    market = {_cik_short(r.get("cik")): r for r in _read_csv(market_path)}
    comps = _read_csv(comps_path)
    if not comps:
        return _result(False, "comps.csv has no rows", 0.0)
    cache: dict = {}
    tolerance = Decimal(str(params.get("tolerance", "0.5")))
    checked, failures, blanks = 0, [], []
    for row in comps:
        cik = _cik_short(row.get("cik"))
        try:
            fy = int(row.get("fiscal_year", ""))
        except ValueError:
            failures.append(f"CIK {cik}: bad fiscal_year {row.get('fiscal_year')!r}")
            continue
        for col, (metric, offset) in COMPS_SOURCED.items():
            reported = _num(row.get(col))
            if reported is None:
                if col in required:
                    failures.append(f"CIK {cik} {col}: blank but required")
                else:
                    blanks.append(f"{cik}:{col}")
                continue
            checked += 1
            src = spreads.get((cik, str(fy + offset), metric))
            if src is None:
                failures.append(f"CIK {cik} {col}: no facts.csv row for {metric} FY{fy + offset}")
                continue
            if _num(src.get("value")) != reported:
                failures.append(f"CIK {cik} {col}: {row.get(col)} != facts.csv {src.get('value')}")
                continue
            problem = _shape_problem(src) or _find_fact(workspace, src, cache, tolerance)
            if problem:
                failures.append(f"CIK {cik} {col}: {problem}")
        m = market.get(cik)
        if m is None:
            failures.append(f"CIK {cik}: not in market data")
            continue
        for col in MARKET_COLUMNS:
            checked += 1
            a, b = row.get(col, "").strip(), (m.get(col) or "").strip()
            if col == "price_as_of":
                ok = a == b
            else:
                ok = (_num(a) or Decimal(0)) == (_num(b) or Decimal(0))
            if not ok:
                failures.append(f"CIK {cik} {col}: {a!r} != market data {b!r}")
    score = (checked - len(failures)) / checked if checked else 0.0
    details = f"{checked} sourced figures checked across {len(comps)} companies"
    if blanks:
        details += f"; blank optional: {', '.join(blanks[:10])}"
    if failures:
        details += f"; {len(failures)} failures: " + "; ".join(failures[:10])
    return _result(not failures, details, round(max(score, 0.0), 4))


def _expected_derived(row: dict[str, str]) -> dict[str, float | None]:
    def f(col: str) -> float | None:
        v = _num(row.get(col))
        return float(v) if v is not None else None

    def div(a, b, positive=False):
        if a is None or b is None or b == 0 or (positive and (a <= 0 or b <= 0)):
            return None
        return a / b

    ltd, std = f("long_term_debt"), f("short_term_debt")
    debt = None if ltd is None and std is None else (ltd or 0.0) + (std or 0.0)
    oi, da, rev = f("operating_income"), f("d_and_a"), f("revenue")
    ebitda = oi + da if oi is not None and da is not None else None
    price, shares = f("price"), f("shares_diluted")
    mcap = price * shares if price is not None and shares is not None else None
    ev = None
    if mcap is not None:
        ev = mcap + (debt or 0.0) - (f("cash") or 0.0) + (f("minority_interest") or 0.0) \
            + (f("preferred") or 0.0)
    growth = div(rev, f("revenue_prior"))
    return {"total_debt": debt, "ebitda": ebitda, "market_cap": mcap, "enterprise_value": ev,
            "ev_revenue": div(ev, rev), "ev_ebitda": div(ev, ebitda, True),
            "pe": div(mcap, f("net_income"), True), "gross_margin": div(f("gross_profit"), rev),
            "operating_margin": div(oi, rev), "ebitda_margin": div(ebitda, rev),
            "revenue_growth": growth - 1 if growth is not None else None}


def comps_recompute(workspace: Path, params: dict, *, run=None) -> dict:
    """Derived comps columns (EV bridge, multiples, margins, growth) recompute
    from the row's own inputs; no hardcoded outputs. params: comps, ratio_tolerance
    (default 0.0006 for 4-place rounding)."""
    path = Path(workspace) / params.get("comps", COMPS_PATH)
    if not path.is_file():
        return _result(False, f"{params.get('comps', COMPS_PATH)} not found", 0.0)
    rows = _read_csv(path)
    if not rows:
        return _result(False, "comps.csv has no rows", 0.0)
    ratio_tol = float(params.get("ratio_tolerance", 0.0006))
    checked, failures = 0, []
    for row in rows:
        for col, expected in _expected_derived(row).items():
            checked += 1
            reported = _num(row.get(col))
            if expected is None or reported is None:
                if (expected is None) != (reported is None):
                    failures.append(f"CIK {row.get('cik')} {col}: reported {row.get(col)!r}, "
                                    f"expected {'blank' if expected is None else expected}")
                continue
            tol = ratio_tol if col in PERCENT_COLUMNS | MULTIPLE_COLUMNS \
                else max(1.0, abs(expected) * 1e-9)
            if abs(float(reported) - expected) > tol:
                failures.append(f"CIK {row.get('cik')} {col}: {row.get(col)} != recomputed "
                                f"{expected:.6g}")
    details = f"{checked - len(failures)}/{checked} derived figures recompute"
    if failures:
        details += ": " + "; ".join(failures[:10])
    return _result(not failures, details, round((checked - len(failures)) / checked, 4))


# --- M3: memo --------------------------------------------------------------------

def _prose_segments(text: str) -> list[str]:
    """Lines/sentences of a markdown memo that make claims (skips headings,
    code, the disclaimer and reference lists)."""
    out: list[str] = []
    para: list[str] = []
    fenced = False

    def flush() -> None:
        # Prose lines are joined into a paragraph first so a figure and its
        # tag may sit on different lines of a wrapped sentence.
        joined = " ".join(para)
        para.clear()
        for sentence in re.split(r"(?<=[.!?])\s+(?=[A-Z(\[])", joined):
            if sentence and not any(m in sentence.lower() for m in DISCLAIMER_MARKERS):
                out.append(sentence)

    for line in text.splitlines():
        s = line.strip()
        if s.startswith("```"):
            flush()
            fenced = not fenced
            continue
        if fenced:
            continue
        if not s or s.startswith("#"):
            flush()
            continue
        if s.startswith("|"):
            flush()
            if not re.fullmatch(r"[|\s:-]+", s):
                out.append(s)
            continue
        para.append(s)
    flush()
    return out


def _figure_ok(match: re.Match, comps: dict) -> str:
    cik, fy, col = str(int(match["cik"])), match["fy"], match["col"].lower()
    row = comps.get((cik, fy))
    if row is None:
        return f"no comps row for CIK {cik} FY{fy}"
    if col not in row:
        return f"comps.csv has no column {col}"
    value = _num(row.get(col))
    if value is None:
        return f"comps {col} is blank (write n/m without a figure tag)"
    shown = _num(match["num"])
    if shown is None:
        return "unreadable figure"
    places = len(match["num"].split(".")[1]) if "." in match["num"] else 0
    half = Decimal(5).scaleb(-(places + 1))
    suffix = (match["suf"] or "").lower()
    if col in PERCENT_COLUMNS:
        if suffix != "%":
            return f"{col} must be shown as a percentage"
        expected = value * 100
    elif col in MULTIPLE_COLUMNS:
        if suffix not in ("x", "×", ""):
            return f"{col} must be shown as a multiple (x)"
        expected = value
    else:
        scale = {"": 1, "k": 10**3, "thousand": 10**3, "m": 10**6, "mm": 10**6,
                 "million": 10**6, "b": 10**9, "bn": 10**9, "billion": 10**9}.get(suffix)
        if scale is None:
            return f"unit {suffix!r} does not fit {col}"
        expected = value / scale
    if abs(shown - expected) > half + Decimal("1e-9"):
        return f"shows {match.group(0).split('[')[0].strip()} but comps {col} = {row.get(col)}"
    return ""


def memo_figures_match(workspace: Path, params: dict, *, run=None) -> dict:
    """Memo numbers are cited and match the workbook.

    Every figure ($, %, multiple, million/billion) is either followed by its
    own figure tag `[F:<cik>:<fiscal_year>:<comps column>]` or sits in a
    sentence / table row that cites a ledger claim `[C#]`. Each figure tag must
    match the comps.csv value at the precision shown. params: path (memo), comps, require_citation (default true).
    """
    memo_rel = params.get("path", MEMO_PATH)
    memo = Path(workspace) / memo_rel
    comps_path = Path(workspace) / params.get("comps", COMPS_PATH)
    if not memo.is_file():
        return _result(False, f"{memo_rel} not found", 0.0)
    comps = {}
    if comps_path.is_file():
        comps = {(_cik_short(r.get("cik")), r.get("fiscal_year", "")): r
                 for r in _read_csv(comps_path)}
    text = memo.read_text(encoding="utf-8")
    failures, tagged = [], 0
    for m in FIGURE_REF_RE.finditer(text):
        tagged += 1
        problem = _figure_ok(m, comps)
        if problem:
            failures.append(f"[F:{m['cik']}:{m['fy']}:{m['col']}] {problem}")
    uncited = 0
    if params.get("require_citation", True):
        for seg in _prose_segments(text):
            # A figure-tagged number is cited by its own tag; any other figure
            # in the sentence or table row needs a ledger claim [C#].
            rest = FIGURE_REF_RE.sub(" ", seg)
            if NUMERIC_CLAIM_RE.search(rest) and not CLAIM_RE.search(rest):
                uncited += 1
                failures.append(f"uncited figure: {seg[:90]!r}")
    total = tagged + uncited
    score = (total - len(failures)) / total if total else 1.0
    details = f"{tagged} figure tags checked, {uncited} uncited numeric statements"
    if failures:
        details += ": " + "; ".join(failures[:10])
    return _result(not failures, details, round(max(score, 0.0), 4))


def no_recommendation_language(workspace: Path, params: dict, *, run=None) -> dict:
    """No buy/sell/hold ratings, price targets or advice to trade. Disclaimer
    lines are exempt. params: paths (default memo.md)."""
    paths = params.get("paths") or [MEMO_PATH]
    patterns = [re.compile(p, re.I) for p in RECOMMENDATION_PATTERNS]
    hits, missing = [], []
    for rel in paths:
        path = Path(workspace) / rel
        if not path.is_file():
            missing.append(rel)
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if any(m in line.lower() for m in DISCLAIMER_MARKERS):
                continue
            for p in patterns:
                found = p.search(line)
                if found:
                    hits.append(f"{rel}:{n}: {found.group(0)!r}")
                    break
    if missing:
        return _result(False, f"missing: {', '.join(missing)}", 0.0)
    if hits:
        return _result(False, "recommendation language: " + "; ".join(hits[:10]), 0.0)
    return _result(True, f"no recommendation language in {len(paths)} file(s)", 1.0)


def _checklist_tables(text: str) -> dict[str, dict[str, dict[str, str]]]:
    """{cik: {RFxx: {status, evidence}}} from '## Name (CIK n)' sections."""
    out: dict[str, dict[str, dict[str, str]]] = {}
    current = None
    for line in text.splitlines():
        head = re.match(r"^#{2,3}\s.*\bCIK\s*(\d+)", line.strip())
        if head:
            current = str(int(head.group(1)))
            out.setdefault(current, {})
            continue
        if current and line.strip().startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if cells and re.fullmatch(r"RF\d{2}", cells[0]) and len(cells) >= 4:
                out[current][cells[0]] = {"status": cells[2].strip("`* ").lower(),
                                          "evidence": " | ".join(cells[3:])}
    return out


def red_flag_checklist(workspace: Path, params: dict, *, run=None) -> dict:
    """The red-flag checklist is complete and does not miss filing-level events.

    For each company (params.ciks, else every CIK in comps.csv) a section
    '## <name> (CIK n)' must list RF01-RF10 with a status in found / not_found
    / not_applicable (open only if params.allow_open). A `found` row needs an
    accession, a [C#] claim or an inputs/ file as evidence, and any accession
    cited must exist in that company's filing index. Items RF01-RF04 and RF09
    are recomputed from the SEC filing index (filed on/after params.since):
    a hit there must be marked found and cite one of the hit accessions.
    """
    rel = params.get("path", RED_FLAGS_PATH)
    path = Path(workspace) / rel
    if not path.is_file():
        return _result(False, f"{rel} not found", 0.0)
    ciks = [_cik_short(c) for c in params.get("ciks") or []]
    if not ciks:
        comps_path = Path(workspace) / params.get("comps", COMPS_PATH)
        if comps_path.is_file():
            ciks = sorted({_cik_short(r.get("cik")) for r in _read_csv(comps_path)}, key=int)
    if not ciks:
        return _result(False, "no companies to check (give params.ciks or build comps.csv)", 0.0)
    tables = _checklist_tables(path.read_text(encoding="utf-8"))
    since = params.get("since", "")
    allowed = RED_FLAG_STATUSES if params.get("allow_open") else RED_FLAG_STATUSES - {"open"}
    failures, checked = [], 0
    for cik in ciks:
        table = tables.get(cik)
        if table is None:
            failures.append(f"no section for CIK {cik}")
            continue
        subs = _source_json(workspace, "submissions", cik)
        index = []
        if subs is None:
            failures.append(f"CIK {cik}: no filing index source to verify against")
        else:
            recent = (subs.get("filings") or {}).get("recent") or {}
            for i, acc in enumerate(recent.get("accessionNumber") or []):
                index.append({"accession": acc, "form": (recent.get("form") or [""])[i],
                              "filed": (recent.get("filingDate") or [""])[i],
                              "items": (recent.get("items") or [""] * (i + 1))[i] or ""})
        known = {f["accession"] for f in index}
        for item in RED_FLAG_ITEMS:
            checked += 1
            row = table.get(item)
            if row is None:
                failures.append(f"CIK {cik} {item}: missing")
                continue
            status, evidence = row["status"], row["evidence"]
            if status not in allowed:
                failures.append(f"CIK {cik} {item}: status {status!r}")
                continue
            cited = set(ACCESSION_RE.findall(evidence))
            forged = cited - known if subs is not None else set()
            if forged:
                failures.append(f"CIK {cik} {item}: accession not in filing index: "
                                f"{', '.join(sorted(forged))}")
            if status == "found" and not (cited or CLAIM_RE.search(evidence)
                                          or "inputs/" in evidence):
                failures.append(f"CIK {cik} {item}: found without evidence")
            if item in RED_FLAG_AUTO and subs is not None:
                items, forms = RED_FLAG_AUTO[item]
                hits = {f["accession"] for f in index
                        if (not since or f["filed"] >= since)
                        and ((f["form"].startswith("8-K")
                              and items & {x.strip() for x in f["items"].split(",")})
                             or f["form"].upper() in forms)}
                if hits and status != "found":
                    failures.append(f"CIK {cik} {item}: filing index shows "
                                    f"{', '.join(sorted(hits))} but status is {status}")
                elif hits and not cited & hits:
                    failures.append(f"CIK {cik} {item}: evidence cites none of "
                                    f"{', '.join(sorted(hits))}")
    score = (checked - len(failures)) / checked if checked else 0.0
    details = f"{len(ciks)} companies x {len(RED_FLAG_ITEMS)} items"
    if failures:
        details += f"; {len(failures)} problems: " + "; ".join(failures[:10])
    return _result(not failures, details, round(max(score, 0.0), 4))


# --- M1: sources and data room ----------------------------------------------------

def source_inventory_resolves(workspace: Path, params: dict, *, run=None) -> dict:
    """Every source in the inventory resolves.

    Columns: source_id, kind, company, cik, form, accession, filed, uri, sha256.
      edgar_filing  accession is in the company's SEC filing index with the same
                    form and filing date; uri is under its Archives folder
      edgar_api     uri is the data.sec.gov submissions/companyfacts URL for the
                    CIK and the JSON is cached
      dataroom / customer  uri is an inputs/ path that exists and hashes to sha256
      web           uri is http(s) (content is verified by the claim ledger)
    """
    rel = params.get("path", SOURCES_PATH)
    path = Path(workspace) / rel
    if not path.is_file():
        return _result(False, f"{rel} not found", 0.0)
    rows = _read_csv(path)
    if not rows:
        return _result(False, "source inventory is empty", 0.0)
    failures, seen, subs_cache = [], set(), {}
    for row in rows:
        sid, kind, uri = row.get("source_id", ""), row.get("kind", ""), row.get("uri", "")
        if not sid or sid in seen:
            failures.append(f"{sid or '(blank)'}: missing or duplicate source_id")
            continue
        seen.add(sid)
        cik = _cik_short(row.get("cik"))
        if kind == "edgar_filing":
            if cik not in subs_cache:
                subs_cache[cik] = _source_json(workspace, "submissions", cik)
            subs = subs_cache[cik]
            if subs is None:
                failures.append(f"{sid}: no filing index for CIK {cik}")
                continue
            recent = (subs.get("filings") or {}).get("recent") or {}
            accs = recent.get("accessionNumber") or []
            acc = row.get("accession", "")
            if acc not in accs:
                failures.append(f"{sid}: accession {acc} not filed by CIK {cik}")
                continue
            i = accs.index(acc)
            if recent["form"][i] != row.get("form") or recent["filingDate"][i] != row.get("filed"):
                failures.append(f"{sid}: form/date {row.get('form')}/{row.get('filed')} != "
                                f"{recent['form'][i]}/{recent['filingDate'][i]}")
            folder = f"/Archives/edgar/data/{cik}/{acc.replace('-', '')}/"
            if not uri.startswith("https://www.sec.gov" + folder):
                failures.append(f"{sid}: uri is not under {folder}")
        elif kind == "edgar_api":
            m = re.fullmatch(r"https://data\.sec\.gov/(?:submissions/|api/xbrl/companyfacts/)"
                             r"CIK(\d{10})\.json", uri)
            if not m or _cik_short(m.group(1)) != cik:
                failures.append(f"{sid}: not a data.sec.gov URL for CIK {cik}")
            elif _source_json(workspace, "submissions" if "submissions" in uri else "companyfacts",
                              cik) is None:
                failures.append(f"{sid}: {uri} was never retrieved")
        elif kind in ("dataroom", "customer"):
            target = _inside(workspace, uri)
            if not uri.startswith("inputs/") or target is None or not target.is_file():
                failures.append(f"{sid}: {uri} is not a file under inputs/")
            elif _sha256(target) != row.get("sha256", "").lower():
                failures.append(f"{sid}: sha256 does not match {uri}")
        elif kind == "web":
            if not re.match(r"https?://", uri):
                failures.append(f"{sid}: web source without an http(s) uri")
        else:
            failures.append(f"{sid}: unknown kind {kind!r}")
    score = (len(rows) - len(failures)) / len(rows)
    details = f"{len(rows) - len(failures)}/{len(rows)} sources resolve"
    if failures:
        details += ": " + "; ".join(failures[:10])
    return _result(not failures, details, round(max(score, 0.0), 4))


def dataroom_index_complete(workspace: Path, params: dict, *, run=None) -> dict:
    """The data-room index covers 100% of files under params.root (default
    inputs/dataroom) with matching sha256 and size, lists nothing that is not
    there, and every unreadable file (readable != yes) carries a note."""
    rel = params.get("path", DATAROOM_INDEX_PATH)
    root_rel = params.get("root", "inputs/dataroom")
    path = Path(workspace) / rel
    root = Path(workspace) / root_rel
    if not path.is_file():
        return _result(False, f"{rel} not found", 0.0)
    ws = Path(workspace).resolve()
    actual = {p.resolve().relative_to(ws).as_posix(): p for p in root.rglob("*") if p.is_file()} \
        if root.is_dir() else {}
    rows = {r.get("path", ""): r for r in _read_csv(path)}
    failures = []
    for rel_file, p in sorted(actual.items()):
        row = rows.get(rel_file)
        if row is None:
            failures.append(f"not indexed: {rel_file}")
            continue
        if row.get("sha256", "").lower() != _sha256(p):
            failures.append(f"sha256 mismatch: {rel_file}")
        if str(row.get("bytes", "")) != str(p.stat().st_size):
            failures.append(f"size mismatch: {rel_file}")
        if row.get("readable", "").lower() != "yes" and not row.get("note", "").strip():
            failures.append(f"unreadable without a note: {rel_file}")
    for extra in sorted(set(rows) - set(actual)):
        failures.append(f"indexed but absent: {extra}")
    covered = len([f for f in actual if f in rows])
    score = covered / len(actual) if actual else 1.0
    details = f"{covered}/{len(actual)} data-room files indexed"
    if failures:
        details += ": " + "; ".join(failures[:10])
    return _result(not failures, details, round(score, 4))


CHECK_DEFS = {
    "xbrl_tieout": xbrl_tieout,
    "comps_tie_to_xbrl": comps_tie_to_xbrl,
    "comps_recompute": comps_recompute,
    "memo_figures_match": memo_figures_match,
    "no_recommendation_language": no_recommendation_language,
    "red_flag_checklist": red_flag_checklist,
    "source_inventory_resolves": source_inventory_resolves,
    "dataroom_index_complete": dataroom_index_complete,
}
