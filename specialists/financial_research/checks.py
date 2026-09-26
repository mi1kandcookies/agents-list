"""
specialists/financial_research/checks.py - acceptance checks for the
financial-research specialist.

Each check is `fn(workspace, params, *, run=None) -> {"passed", "details",
"score"}` and is listed in CHECK_DEFS. Checks never trust the agent's own
report: every figure is re-derived from the cached SEC JSON
(.agentkit/edgar/, falling back to inputs/edgar/), the client's market data,
the claim ledger and the data-room files themselves. The recomputation here
(fiscal years, debt, multiples) is written independently of tools.py so a
bug in one does not hide in the other.

Paths from params (which a brief may add to) and paths the agent wrote into
a deliverable are resolved with the kit's jail_path, which rejects escapes
lexically before touching the filesystem; a rejected path fails the check.

    xbrl_tieout               every facts.csv row is a real XBRL fact
    comps_tie_to_xbrl         every sourced comps.csv figure ties to an XBRL fact;
                              nothing the SEC reports is left blank; every
                              company in scope has a row
    comps_recompute           derived comps columns recompute from their inputs
    memo_figures_match        memo figures cite and match comps.csv / the ledger
    no_recommendation_language no buy/sell/hold ratings, price targets or advice
    red_flag_checklist        checklist complete; filing-level hits not missed
    source_inventory_resolves every listed source resolves (accession / hash / ledger)
    dataroom_index_complete   the index covers 100% of data-room files
"""
from __future__ import annotations

import csv
import functools
import hashlib
import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from agentkit.errors import PolicyViolation
from agentkit.ledger import Ledger, normalize_text
from agentkit.policy import jail_path
from agentkit.tools.documents import TEXT_SUFFIXES as KIT_TEXT_SUFFIXES

SPREADS_PATH = "deliverables/m2-spreads-comps/facts.csv"
COMPS_PATH = "deliverables/m2-spreads-comps/comps.csv"
MARKET_DATA_PATH = "inputs/market_data.csv"
MEMO_PATH = "deliverables/m3-diligence-memo/memo.md"
RED_FLAGS_PATH = "deliverables/m3-diligence-memo/red_flags.md"
SOURCES_PATH = "deliverables/m1-plan-sources/source_inventory.csv"
DATAROOM_INDEX_PATH = "deliverables/m1-plan-sources/dataroom_index.csv"
ANNUAL_FORMS = ("10-K", "10-K/A")

# Accepted tags per standard metric: (duration | instant, unit, tags). A row
# using any other tag must carry a note explaining the choice. Kept in step
# with tools.METRICS on purpose, but declared here so the check does not
# depend on the tool's selection logic.
METRIC_TAGS: dict[str, tuple[str, str, list[str]]] = {
    "revenue": ("duration", "USD", ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                                    "RevenueFromContractWithCustomerIncludingAssessedTax",
                                    "SalesRevenueNet"]),
    "cost_of_revenue": ("duration", "USD", ["CostOfRevenue", "CostOfGoodsAndServicesSold"]),
    "gross_profit": ("duration", "USD", ["GrossProfit"]),
    "operating_income": ("duration", "USD", ["OperatingIncomeLoss"]),
    "net_income": ("duration", "USD", ["NetIncomeLoss", "ProfitLoss"]),
    "d_and_a": ("duration", "USD", ["DepreciationDepletionAndAmortization",
                                    "DepreciationAndAmortization",
                                    "DepreciationAmortizationAndAccretionNet"]),
    "operating_cash_flow": ("duration", "USD", ["NetCashProvidedByUsedInOperatingActivities"]),
    "capex": ("duration", "USD", ["PaymentsToAcquirePropertyPlantAndEquipment"]),
    "shares_diluted": ("duration", "shares", ["WeightedAverageNumberOfDilutedSharesOutstanding"]),
    "eps_diluted": ("duration", "USD/shares", ["EarningsPerShareDiluted"]),
    "cash": ("instant", "USD", ["CashAndCashEquivalentsAtCarryingValue",
                                "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"]),
    "long_term_debt_noncurrent": ("instant", "USD", ["LongTermDebtNoncurrent"]),
    "long_term_debt_current": ("instant", "USD", ["LongTermDebtCurrent"]),
    "long_term_debt_total": ("instant", "USD", ["LongTermDebt"]),
    "debt_current": ("instant", "USD", ["DebtCurrent"]),
    "short_term_borrowings": ("instant", "USD", ["ShortTermBorrowings", "CommercialPaper"]),
    "total_assets": ("instant", "USD", ["Assets"]),
    "total_equity": ("instant", "USD", ["StockholdersEquity",
                                        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"]),
}

# comps.csv column -> (metric in facts.csv, fiscal-year offset)
COMPS_SOURCED = {"revenue": ("revenue", 0), "revenue_prior": ("revenue", -1),
                 "gross_profit": ("gross_profit", 0), "operating_income": ("operating_income", 0),
                 "net_income": ("net_income", 0), "d_and_a": ("d_and_a", 0), "cash": ("cash", 0),
                 "long_term_debt_noncurrent": ("long_term_debt_noncurrent", 0),
                 "long_term_debt_current": ("long_term_debt_current", 0),
                 "long_term_debt_total": ("long_term_debt_total", 0),
                 "debt_current": ("debt_current", 0),
                 "short_term_borrowings": ("short_term_borrowings", 0),
                 "shares_diluted": ("shares_diluted", 0)}
MARKET_COLUMNS = ["price", "price_as_of", "shares_outstanding", "minority_interest", "preferred"]
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
INDEX_PAGE_RE = re.compile(r"CIK\d{10}-submissions-\d{3}\.json")
INPUT_REF_RE = re.compile(r"inputs/[^\s|,;()\[\]<>`'\"]+")

ACCESSION_RE = re.compile(r"\b\d{10}-\d{2}-\d{6}\b")
# One or more ledger claims in a bracket: [C1], [C1, C2], [C3; C4].
CLAIM_GROUP_RE = re.compile(r"\[\s*(C\d+(?:\s*[,;]\s*C\d+)*)\s*\]")
FIGURE_REF_RE = re.compile(
    r"(?P<neg>[-−])?(?P<cur>\$)?\s?(?P<num>\d[\d,]*(?:\.\d+)?)\s?"
    r"(?P<suf>%|percent|x|×|times|bn|billion|mm|mn|million|m|k|thousand|b)?"
    r"\s*\[F:(?P<cik>\d+):(?P<fy>\d{4}):(?P<col>[a-z_]+)\]", re.I)
# Any amount in prose: optional sign and $, a number, an optional unit.
AMOUNT_RE = re.compile(
    r"(?<![\w.,])(?P<neg>[-−])?(?P<cur>\$)?\s?(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?:\s?(?P<suf>%|per\s?cent\b|pct\b|bps\b|basis\s+points?\b|x(?![A-Za-z])|×|times\b|"
    r"turns\b|bn\b|billion\b|b\b|mm\b|mn\b|million\b|m\b|thousand\b|k\b))?", re.I)
SCALES = {"bn": 10**9, "billion": 10**9, "b": 10**9, "mm": 10**6, "mn": 10**6,
          "million": 10**6, "m": 10**6, "thousand": 10**3, "k": 10**3}
# Text that is a reference, not a figure: accessions, CIKs, URLs, file paths.
REFERENCE_RE = re.compile(r"\b\d{10}-\d{2}-\d{6}\b|\bCIK\s*#?\s*\d+|https?://\S+|"
                          r"(?:inputs|deliverables|\.agentkit)/\S+|\[F:[^\]]*\]", re.I)
ISO_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
LIST_ITEM_RE = re.compile(r"^(?:[-*+]|\d{1,3}[.)])\s+")
# A change word right before an unsigned figure makes it negative ("declined
# 4.5%", "a net loss of $12.3 million"); "fell to 38.1%" states a level.
NEGATIVE_BEFORE_RE = re.compile(
    r"\b(?:declin\w*|decreas\w*|fell|falls?|falling|dropp?\w*|down|lower|contract(?:ed|ing|ion)|"
    r"shr[iau]nk\w*|loss(?:es)?|negative|deficit)"
    r"(?:\s+(?:by|of|was|were|is|about|around|roughly|approximately|nearly|some|an?))*\s*$", re.I)

# Rating words; the broad set only where the context already says "rating".
_CALL = (r"(?:strong\s+)?(?:buy|sell|hold|outperform|underperform|overweight|underweight|"
         r"accumulate|market\s+perform)")
_RATING = rf"(?:{_CALL}|reduce|neutral|sector\s+perform)"
_PER_SHARE = r"\$\s?\d[\d,.]*\s*(?:per|a|/)\s*share\b"
RECOMMENDATION_PATTERNS = [
    rf"\b{_RATING}\s+rating\b",
    r"\bstrong\s+(?:buy|sell)\b",
    rf"\b(?:rating|recommendation)\s*[:=]\s*{_RATING}\b",
    # "Coldharbor: Buy (12-month)." as a line of its own
    r"(?m):\s*(?:strong\s+)?(?:buy|sell|hold|outperform|underperform|overweight|underweight)"
    r"\s*(?:\([^)\n]*\))?\s*[.;,|]?\s*$",
    r"\brat(?:e|es|ed|ing)\s+(?:the\s+(?:stock|shares|company)|it|(?:its\s+)?shares)\s+"
    r"(?:as\s+)?(?:an?\s+)?(?:strong\s+)?"
    r"(?:buy|sell|hold|outperform|underperform|overweight|underweight)\b",
    # "We rate Coldharbor a Buy", "rated the company as Overweight"
    rf"\brat(?:e|ed)\s+(?:\S+\s+){{0,4}}?(?:as\s+(?:an?\s+)?|an?\s+){_CALL}\b(?!-)",
    r"\bprice\s+target\b|\btarget\s+price\b",
    rf"\btarget\s+(?:of|at)\s+{_PER_SHARE}",
    r"\b(?:we|i)\s+(?:would\s+)?(?:recommend|advise|suggest|urge)\s+(?:that\s+\w+\s+)?"
    r"(?:buy|buying|sell|selling|purchas\w*|short\w*|invest\w*|accumulat\w*|exit\w*)\b",
    r"\b(?:investors?|clients?|you|readers?|shareholders?|holders?|we|one)\s+"
    r"(?:should|must|ought\s+to)\s+(?:consider\s+)?"
    r"(?:buy\w*|sell\w*|own|short\w*|accumulat\w*|purchas\w*|exit\w*|add\s+to)\b",
    r"\b(?:initiat\w+\s+coverage|upgrade\w*\s+to\s+(?:buy|outperform|overweight)|"
    r"downgrade\w*\s+to\s+(?:sell|underperform|underweight))\b",
    r"\b(?:is|looks|appears)\s+(?:like\s+)?(?:a\s+)?(?:compelling\s+|clear\s+)?(?:buy|sell)\b(?!-)",
    # a view on what the shares are worth; accounting fair value ("the fair
    # value of the warrants") is ordinary prose
    r"\b(?:fair|intrinsic)\s+value\s+(?:estimate|target)\b",
    r"\b(?:our|my)\s+(?:own\s+)?(?:fair|intrinsic)\s+value\b",
    r"\bwe\s+(?:estimate|derive|see|put|set|arrive\s+at)\s+(?:an?\s+|the\s+)?(?:fair|intrinsic)\s+value\b",
    rf"\b(?:fair|intrinsic)\s+value\s+(?:of|at|is|to)\s+(?:about\s+|roughly\s+)?{_PER_SHARE}",
    rf"\bworth\s+(?:about\s+|roughly\s+)?{_PER_SHARE}",
    r"\bvalue\s+(?:the\s+)?(?:shares|stock|equity)\s+at\s+\$",
    r"\b(?:upside|downside)\s+(?:potential\s+|risk\s+)?to\s+(?:\$|our\b|a\s+(?:target|value))",
]


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


def _source_file(workspace: Path, rel: str) -> Any:
    """SEC JSON as fetched by the tools (.agentkit/edgar/) or supplied (inputs/edgar/)."""
    for base in (".agentkit/edgar", "inputs/edgar"):
        path = Path(workspace) / base / rel
        if path.is_file():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                return None
    return None


def _source_json(workspace: Path, kind: str, cik: Any) -> dict | None:
    c = _cik10(cik)
    return _source_file(workspace, f"{kind}/CIK{c}.json") if c else None


def _num(value: Any) -> Decimal | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "").replace("$", "").replace("−", "-")
    if text == "":
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _ws(workspace: Path, rel: str) -> Path:
    """A workspace path from check params; PolicyViolation if it escapes."""
    return jail_path(Path(workspace), rel)


def _inside(workspace: Path, rel: str) -> Path | None:
    """An agent-written path inside the workspace, or None (checked lexically
    first, so a UNC or device path is never opened)."""
    try:
        target = jail_path(Path(workspace), rel)
    except PolicyViolation:
        return None
    return target if target != Path(workspace).resolve() else None


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ledger(workspace: Path) -> Ledger | None:
    try:
        return Ledger(Path(workspace))
    except (OSError, ValueError, TypeError):
        return None


def _years_back(day: str, years: int) -> str:
    try:
        d = date.fromisoformat(day)
    except ValueError:
        return ""
    return (d.replace(year=d.year - years) if not (d.month == 2 and d.day == 29)
            else d.replace(year=d.year - years, day=28)).isoformat()


# --- the SEC source: fiscal years and facts --------------------------------------

def _full_year(r: dict) -> bool:
    try:
        days = (date.fromisoformat(r["end"]) - date.fromisoformat(r["start"])).days
    except (KeyError, TypeError, ValueError):
        return False
    return 350 <= days <= 380


def _fy_ends(facts: dict) -> dict[int, str]:
    """Fiscal year -> year-end, re-derived from the SEC source: each 10-K's
    year is the latest full-year period it reports; the original 10-K's fy
    labels that year-end when fy is its calendar year or the one before,
    else the calendar year does. On a clash the year-end falling in the
    labelled calendar year keeps the label. Comparative years inside a 10-K
    (one full year apart) count down from its own label."""
    by_filing: dict[str, list[dict]] = {}
    for concept in ((facts.get("facts") or {}).get("us-gaap") or {}).values():
        for recs in ((concept or {}).get("units") or {}).values():
            for r in recs:
                if r.get("form") in ANNUAL_FORMS and r.get("accn") and _full_year(r):
                    by_filing.setdefault(r["accn"], []).append(r)
    filings = []
    for recs in by_filing.values():
        ends = sorted({r["end"] for r in recs}, reverse=True)
        top = next(r for r in recs if r["end"] == ends[0])
        filings.append(((top.get("form") != "10-K", str(top.get("filed", ""))), ends, top.get("fy")))
    filings.sort(key=lambda f: f[0])
    owner: dict[str, Any] = {}
    for _, ends, fy in filings:
        owner.setdefault(ends[0], fy)
    out: dict[int, str] = {}
    for end in sorted(owner):
        cal, fy = int(end[:4]), owner[end]
        label = fy if isinstance(fy, int) and cal - 1 <= fy <= cal else cal
        if label not in out or (int(out[label][:4]) != label and cal == label):
            out[label] = end
    for _, ends, _ in filings:
        label = next((y for y, e in out.items() if e == ends[0]), None)
        if label is None:
            continue
        later = ends[0]
        for end in ends[1:]:
            gap = (date.fromisoformat(later) - date.fromisoformat(end)).days
            if 350 <= gap <= 380:
                label, later = label - 1, end
                if label not in out and end not in out.values():
                    out[label] = end
    return dict(sorted(out.items()))


def _company(workspace: Path, cik: Any, cache: dict) -> tuple[dict | None, dict[int, str]]:
    """(companyfacts JSON or None, fiscal year-ends) for one company, cached."""
    c = _cik10(cik)
    if c not in cache:
        facts = _source_json(workspace, "companyfacts", c)
        cache[c] = (facts, _fy_ends(facts) if facts else {})
    return cache[c]


def _source_reports(facts: dict, ends: dict[int, str], metric: str, fiscal_year: int) -> bool:
    """Whether the SEC source has an annual fact for a standard metric and year."""
    end, spec = ends.get(fiscal_year), METRIC_TAGS.get(metric)
    if end is None or spec is None:
        return False
    kind, unit, tags = spec
    gaap = (facts.get("facts") or {}).get("us-gaap") or {}
    for tag in tags:
        for r in ((gaap.get(tag) or {}).get("units") or {}).get(unit) or []:
            if r.get("form") not in ANNUAL_FORMS or r.get("end") != end:
                continue
            if (kind == "duration" and _full_year(r)) or (kind == "instant" and "start" not in r):
                return True
    return False


def _find_fact(facts: dict, row: dict[str, str], tolerance: Decimal) -> str:
    """'' when the spreads row is a real XBRL fact, else the reason it is not."""
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


def _shape_problem(row: dict[str, str], ends: dict[int, str]) -> str:
    """Spreads-row sanity the fact alone cannot tell us: the period is the
    fiscal year's own year-end (anchored to its 10-K), annual, right tag."""
    metric = row.get("metric", "")
    kind, _, tags = METRIC_TAGS.get(metric, ("", "", []))
    end = row.get("period_end", "")
    try:
        fy = int(row.get("fiscal_year", ""))
    except ValueError:
        return "fiscal_year is not a year"
    year_end = ends.get(fy)
    if year_end is None:
        return f"no 10-K in the SEC source covers fiscal year {fy}"
    if end != year_end:
        return f"period_end {end or '(blank)'} is not the FY{fy} year-end {year_end}"
    try:
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


def _row_problem(workspace: Path, row: dict[str, str], cache: dict, tolerance: Decimal) -> str:
    facts, ends = _company(workspace, row.get("cik"), cache)
    if facts is None:
        return f"no companyfacts source for CIK {row.get('cik')}"
    return _shape_problem(row, ends) or _find_fact(facts, row, tolerance)


# --- M2: spreads and comps -------------------------------------------------------

def xbrl_tieout(workspace: Path, params: dict, *, run=None) -> dict:
    """Every facts.csv row matches an XBRL fact (tag, unit, period, accession,
    value) and its fiscal_year is the one the SEC source gives that period.

    params: path (default facts.csv), min_match_ratio (default 1.0; rows with a
    written `note` may be unmatched, e.g. a disclosed restatement), tolerance
    (absolute, default 0.5 for rounding).
    """
    path = _ws(workspace, params.get("path", SPREADS_PATH))
    if not path.is_file():
        return _result(False, f"{params.get('path', SPREADS_PATH)} not found", 0.0)
    rows = _read_csv(path)
    if not rows:
        return _result(False, "spreads file has no rows", 0.0)
    tolerance = Decimal(str(params.get("tolerance", "0.5")))
    cache: dict = {}
    matched, explained, failures = 0, 0, []
    for i, row in enumerate(rows, start=2):
        problem = _row_problem(workspace, row, cache, tolerance)
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
    """Every sourced figure in comps.csv ties to an XBRL fact, nothing the
    SEC reports is left blank, and every company in scope has a row.

    Each XBRL column must match its facts.csv row, and that row must itself
    be a real fact in the SEC source. A blank XBRL column fails when facts.csv
    has that metric or the SEC source reports it under a standard tag. Market
    columns must equal the client's market data row, and stay blank for a
    company with no row (or with no market data file at all; set
    require_market_data to fail instead). Companies in scope (params.ciks,
    else every CIK in facts.csv and comps.csv) need a row for each comps
    fiscal year unless the SEC source has no revenue for it.

    params: comps, spreads, market_data, ciks, require_market_data,
    required (columns that may never be blank; default revenue,
    operating_income, net_income, shares_diluted), tolerance.
    """
    comps_rel = params.get("comps", COMPS_PATH)
    spreads_rel = params.get("spreads", SPREADS_PATH)
    market_rel = params.get("market_data", MARKET_DATA_PATH)
    comps_path, spreads_path, market_path = (_ws(workspace, r) for r in (comps_rel, spreads_rel,
                                                                         market_rel))
    for rel, p in ((comps_rel, comps_path), (spreads_rel, spreads_path)):
        if not p.is_file():
            return _result(False, f"{rel} not found", 0.0)
    has_market = market_path.is_file()
    if not has_market and params.get("require_market_data"):
        return _result(False, f"{market_rel} not found and market data is required", 0.0)
    required = set(params.get("required", ["revenue", "operating_income", "net_income",
                                            "shares_diluted"]))
    spreads_rows = _read_csv(spreads_path)
    spreads = {(_cik_short(r.get("cik")), r.get("fiscal_year", ""), r.get("metric", "")): r
               for r in spreads_rows}
    market = {_cik_short(r.get("cik")): r for r in _read_csv(market_path)} if has_market else {}
    comps = _read_csv(comps_path)
    if not comps:
        return _result(False, "comps.csv has no rows", 0.0)
    cache: dict = {}
    tolerance = Decimal(str(params.get("tolerance", "0.5")))
    checked, failures, notes, covered = 0, [], [], set()
    for row in comps:
        cik = _cik_short(row.get("cik"))
        try:
            fy = int(row.get("fiscal_year", ""))
        except ValueError:
            failures.append(f"CIK {cik}: bad fiscal_year {row.get('fiscal_year')!r}")
            continue
        covered.add((cik, fy))
        facts, ends = _company(workspace, cik, cache)
        for col, (metric, offset) in COMPS_SOURCED.items():
            reported = _num(row.get(col))
            year = fy + offset
            checked += 1
            if reported is None:
                if col in required:
                    failures.append(f"CIK {cik} {col}: blank but required")
                elif (cik, str(year), metric) in spreads:
                    failures.append(f"CIK {cik} {col}: blank but facts.csv has {metric} FY{year}")
                elif facts is not None and _source_reports(facts, ends, metric, year):
                    failures.append(f"CIK {cik} {col}: blank but the SEC source reports "
                                    f"{metric} for FY{year}")
                continue
            src = spreads.get((cik, str(year), metric))
            if src is None:
                failures.append(f"CIK {cik} {col}: no facts.csv row for {metric} FY{year}")
                continue
            if _num(src.get("value")) != reported:
                failures.append(f"CIK {cik} {col}: {row.get(col)} != facts.csv {src.get('value')}")
                continue
            problem = _row_problem(workspace, src, cache, tolerance)
            if problem:
                failures.append(f"CIK {cik} {col}: {problem}")
        m = market.get(cik)
        if m is None:
            notes.append(f"CIK {cik}: no market data")
        for col in MARKET_COLUMNS:
            checked += 1
            a = (row.get(col) or "").strip()
            if m is None:
                if a:
                    failures.append(f"CIK {cik} {col}: {a!r} without a client market data row")
                continue
            b = (m.get(col) or "").strip()
            if col == "price_as_of":
                ok = a == b
            elif col == "shares_outstanding":
                ok = _num(a) == _num(b)
            else:
                ok = (_num(a) or Decimal(0)) == (_num(b) or Decimal(0))
            if not ok:
                failures.append(f"CIK {cik} {col}: {a!r} != market data {b!r}")
    in_scope = [c for c in (_cik_short(x) for x in params.get("ciks") or []) if c]
    if not in_scope:
        in_scope = sorted({_cik_short(r.get("cik")) for r in spreads_rows + comps} - {""}, key=int)
    for cik in in_scope:
        for fy in sorted({fy for _, fy in covered}):
            if (cik, fy) in covered:
                continue
            checked += 1
            facts, ends = _company(workspace, cik, cache)
            if facts is None:
                failures.append(f"CIK {cik}: no FY{fy} comps row and no companyfacts source")
            elif _source_reports(facts, ends, "revenue", fy):
                failures.append(f"CIK {cik}: no FY{fy} comps row although the SEC source "
                                "reports its revenue")
            else:
                notes.append(f"CIK {cik}: no FY{fy} revenue in XBRL (not comparable)")
    score = (checked - len(failures)) / checked if checked else 0.0
    details = f"{checked} sourced figures checked across {len(comps)} companies"
    if notes:
        details += "; " + "; ".join(notes[:10])
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

    # Each borrowing counted once: all current debt with the noncurrent part;
    # else its pieces; else the long-term total (which already holds the
    # current portion) plus short-term borrowings.
    ncur, cur = f("long_term_debt_noncurrent"), f("debt_current")
    ltdc, stb, ltdt = f("long_term_debt_current"), f("short_term_borrowings"), f("long_term_debt_total")
    if ncur is not None and cur is not None:
        debt = ncur + cur
    elif ncur is not None:
        debt = ncur + (ltdc or 0.0) + (stb or 0.0)
    elif ltdt is not None:
        debt = ltdt + (stb or 0.0)
    elif cur is not None:
        debt = cur
    elif ltdc is not None or stb is not None:
        debt = (ltdc or 0.0) + (stb or 0.0)
    else:
        debt = None
    oi, da, rev = f("operating_income"), f("d_and_a"), f("revenue")
    ebitda = oi + da if oi is not None and da is not None else None
    price = f("price")
    shares = f("shares_outstanding") if f("shares_outstanding") is not None else f("shares_diluted")
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
    """Derived comps columns (debt, EV bridge, multiples, margins, growth)
    recompute from the row's own inputs; no hardcoded outputs. params: comps,
    ratio_tolerance (default 0.0006 for 4-place rounding)."""
    path = _ws(workspace, params.get("comps", COMPS_PATH))
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
    """Sentences, list items, table rows and headings of a markdown memo
    (code fences skipped), each checked on its own."""
    out: list[str] = []
    para: list[str] = []
    fenced = False

    def flush() -> None:
        # Wrapped prose lines are joined first so a figure and its tag may
        # sit on different lines of one sentence.
        joined = " ".join(para)
        para.clear()
        out.extend(s for s in re.split(r"(?<=[.!?])\s+(?=[A-Z(\[])", joined) if s)

    for line in text.splitlines():
        s = line.strip()
        if s.startswith("```"):
            flush()
            fenced = not fenced
            continue
        if fenced:
            continue
        if not s:
            flush()
        elif s.startswith("#"):
            flush()
            out.append(s.lstrip("#").strip())
        elif s.startswith("|"):
            flush()
            if not re.fullmatch(r"[|\s:-]+", s):
                out.append(s)
        elif LIST_ITEM_RE.match(s):
            flush()        # each bullet or numbered item is its own claim
            para.append(s)
        else:
            para.append(s)
    flush()
    return out


def _is_figure(m: re.Match) -> bool:
    """An amount that states a figure: money, a unit, or a large bare number
    (a four-digit number from 1900 to 2099 alone is read as a year)."""
    if m["cur"] or m["suf"]:
        return True
    digits = m["num"].replace(",", "").split(".")[0]
    return "," in m["num"] or len(digits) >= 5 or (len(digits) == 4 and not 1900 <= int(digits) <= 2099)


Amount = tuple[Decimal, Decimal, Decimal, str]


def _amount(m: re.Match) -> Amount:
    """(absolute number as written, scaled to units, half a unit of the last
    digit shown, kind: % | x | bps | '' for money and plain numbers)."""
    num = m["num"].replace(",", "")
    raw = abs(Decimal(num))
    suffix = re.sub(r"\s+", "", (m["suf"] or "").lower())
    scale = SCALES.get(suffix, 1)
    places = len(num.split(".")[1]) if "." in num else 0
    kind = ("%" if suffix in ("%", "percent", "pct") else "x" if suffix in ("x", "×", "times", "turns")
            else "bps" if suffix in ("bps", "basispoint", "basispoints") else "")
    return raw, raw * scale, Decimal(5).scaleb(-(places + 1)) * scale, kind


def _in_quotes(amount: Amount, quoted: list[Amount]) -> bool:
    """The quote states the figure: same kind, and the same number or one the
    memo's figure rounds ("$150 million" for "$150,000,000")."""
    raw, scaled, half, kind = amount
    return any(q_kind == kind and (q_raw == raw or abs(q_scaled - scaled) <= half)
               for q_raw, q_scaled, _, q_kind in quoted)


def _claim_amounts(workspace: Path) -> dict[str, list[Amount]]:
    """Claim id -> the amounts in its verbatim quote (dates left out)."""
    ledger = _ledger(workspace)
    if ledger is None:
        return {}
    return {c.id: [_amount(m) for m in AMOUNT_RE.finditer(ISO_DATE_RE.sub(" ", normalize_text(c.quote)))]
            for c in ledger.claims}


def _figure_ok(match: re.Match, comps: dict, before: str) -> str:
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
    text = match.group(0).split("[")[0].strip()
    if match["neg"]:
        shown = -shown
    elif shown > 0 and col not in MULTIPLE_COLUMNS and NEGATIVE_BEFORE_RE.search(before):
        shown = -shown       # "declined 4.5%", "a net loss of $12.3 million"
        text = f"-{text} (by its wording)"
    digits = match["num"].replace(",", "").lstrip("-−")
    places = len(digits.split(".")[1]) if "." in digits else 0
    half = Decimal(5).scaleb(-(places + 1))
    suffix = re.sub(r"\s+", "", (match["suf"] or "").lower())
    scale = Decimal(1)
    if col in PERCENT_COLUMNS:
        if suffix not in ("%", "percent"):
            return f"{col} must be shown as a percentage"
        expected = value * 100
    elif col in MULTIPLE_COLUMNS:
        if suffix not in ("x", "×", "times", ""):
            return f"{col} must be shown as a multiple (x)"
        expected = value
    else:
        if suffix not in SCALES and suffix != "":
            return f"unit {suffix!r} does not fit {col}"
        scale = Decimal(SCALES.get(suffix, 1))
        expected = value / scale
    if abs(shown - expected) > half + Decimal("1e-9"):
        return f"shows {text} but comps {col} = {row.get(col)}"
    if shown != expected:
        # The precision the playbook asks for: percentages and multiples with
        # a decimal; money to three significant figures or $ millions with
        # one decimal.
        if col in PERCENT_COLUMNS | MULTIPLE_COLUMNS:
            if places < 1:
                return f"{text} is too rounded: show {col} with at least one decimal"
        else:
            allowance = abs(value) * Decimal("0.005")
            if scale >= 10**6:
                allowance = max(allowance, Decimal(50_000))
            if half * scale > allowance:
                return (f"{text} is too rounded for comps {col} = {row.get(col)} (show three "
                        "significant figures, or $ millions with one decimal)")
    return ""


def memo_figures_match(workspace: Path, params: dict, *, run=None) -> dict:
    """Memo numbers are cited and match their source.

    Every figure ($, %, x, times, million/billion, M/B, bps, or a bare number
    of four or more digits other than a year) is either followed by its own
    figure tag `[F:<cik>:<fiscal_year>:<comps column>]` or sits in a
    sentence, list item or table row citing ledger claims `[C#]` whose
    verbatim quotes contain it. A figure tag must match the comps.csv value
    at the precision shown, with the sign its wording implies ("declined
    4.5%" is -4.5%), and show the playbook's minimum precision. params: path
    (memo), comps, require_citation (default true).
    """
    memo_rel = params.get("path", MEMO_PATH)
    memo = _ws(workspace, memo_rel)
    comps_path = _ws(workspace, params.get("comps", COMPS_PATH))
    if not memo.is_file():
        return _result(False, f"{memo_rel} not found", 0.0)
    comps = {}
    if comps_path.is_file():
        comps = {(_cik_short(r.get("cik")), r.get("fiscal_year", "")): r
                 for r in _read_csv(comps_path)}
    text = memo.read_text(encoding="utf-8")
    failures, tagged, prev_end = [], 0, 0
    for m in FIGURE_REF_RE.finditer(text):
        tagged += 1
        # the words since the previous figure, within the same sentence or cell
        before = re.split(r"[.!?;]\s|\||\n\s*\n", text[prev_end:m.start()])[-1]
        prev_end = m.end()
        problem = _figure_ok(m, comps, before)
        if problem:
            failures.append(f"[F:{m['cik']}:{m['fy']}:{m['col']}] {problem}")
    uncited = 0
    if params.get("require_citation", True):
        claims = _claim_amounts(workspace)
        for seg in _prose_segments(text):
            # A figure-tagged number is cited by its own tag; any other figure
            # needs a ledger claim whose quote contains it.
            rest = REFERENCE_RE.sub(" ", FIGURE_REF_RE.sub(" ", seg))
            cited = {c for g in CLAIM_GROUP_RE.findall(rest) for c in re.findall(r"C\d+", g)}
            figures = [m for m in AMOUNT_RE.finditer(CLAIM_GROUP_RE.sub(" ", rest)) if _is_figure(m)]
            if not figures:
                continue
            if not cited:
                uncited += 1
                failures.append(f"uncited figure: {seg[:90]!r}")
                continue
            quoted = [a for c in sorted(cited) for a in claims.get(c, [])]
            unsupported = [m.group(0).strip() for m in figures if not _in_quotes(_amount(m), quoted)]
            if unsupported:
                uncited += 1
                failures.append(f"{', '.join(unsupported[:3])} not in the quote of "
                                f"{', '.join(sorted(cited))}: {seg[:70]!r}")
    total = tagged + uncited
    score = (total - len(failures)) / total if total else 1.0
    details = f"{tagged} figure tags checked, {uncited} unsupported numeric statements"
    if failures:
        details += ": " + "; ".join(failures[:10])
    return _result(not failures, details, round(max(score, 0.0), 4))


@functools.lru_cache(maxsize=1)
def _manifest_disclaimer() -> str:
    try:
        from agentkit.manifest import load_manifest
        return load_manifest(Path(__file__).with_name("agent.yaml")).human_gate.disclaimer
    except Exception:       # a broken manifest fails elsewhere; scan everything
        return ""


def _without_disclaimer(text: str, disclaimer: str) -> str:
    """The text with the verbatim disclaimer removed (line breaks kept)."""
    words = normalize_text(disclaimer).split()
    if not words:
        return text
    pattern = re.compile(r"\s+".join(re.escape(w) for w in words), re.I)
    return pattern.sub(lambda m: "\n" * m.group(0).count("\n"), text)


def no_recommendation_language(workspace: Path, params: dict, *, run=None) -> dict:
    """No buy/sell/hold ratings, price targets, fair-value-per-share or
    upside calls, or advice to trade. Only the manifest's disclaimer, as
    written there, is exempt. params: paths (default memo.md), disclaimer
    (default the manifest's)."""
    paths = params.get("paths") or [MEMO_PATH]
    disclaimer = params.get("disclaimer") or _manifest_disclaimer()
    patterns = [re.compile(p, re.I) for p in RECOMMENDATION_PATTERNS]
    hits, missing = [], []
    for rel in paths:
        path = _ws(workspace, rel)
        if not path.is_file():
            missing.append(rel)
            continue
        text = _without_disclaimer(path.read_text(encoding="utf-8"), disclaimer)
        found = sorted((m.start(), m.group(0)) for p in patterns for m in p.finditer(text))
        for start, phrase in found:
            line = text.count("\n", 0, start) + 1
            hits.append(f"{rel}:{line}: {' '.join(phrase.split())!r}")
    if missing:
        return _result(False, f"missing: {', '.join(missing)}", 0.0)
    if hits:
        return _result(False, "recommendation language: " + "; ".join(dict.fromkeys(hits[:10])), 0.0)
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


def _filing_index(workspace: Path, cik: str) -> tuple[list[dict[str, str]] | None, list[dict]]:
    """(the SEC filing index: filings.recent plus cached older pages, or None;
    older pages SEC lists that are not cached)."""
    subs = _source_json(workspace, "submissions", cik)
    if subs is None:
        return None, []
    filings = subs.get("filings") or {}
    blocks, uncached = [filings.get("recent") or {}], []
    for page in filings.get("files") or []:
        name = str((page or {}).get("name", ""))
        if not INDEX_PAGE_RE.fullmatch(name):
            continue
        data = _source_file(workspace, f"submissions/{name}")
        if isinstance(data, dict):
            blocks.append(data)
        else:
            uncached.append(page)
    index = []
    for block in blocks:
        def at(key: str, i: int) -> str:
            values = block.get(key) or []
            return str(values[i] or "") if i < len(values) else ""
        for i, acc in enumerate(block.get("accessionNumber") or []):
            index.append({"accession": acc, "form": at("form", i), "filed": at("filingDate", i),
                          "items": at("items", i)})
    return index, uncached


def red_flag_checklist(workspace: Path, params: dict, *, run=None) -> dict:
    """The red-flag checklist is complete and does not miss filing-level events.

    For each company in scope (params.ciks, else every CIK in facts.csv and
    comps.csv) a section '## <name> (CIK n)' must list RF01-RF10 with a
    status in found / not_found / not_applicable (open only if
    params.allow_open). A `found` row needs an accession, a [C#] claim or an
    inputs/ file as evidence; every accession cited must exist in that
    company's filing index and every inputs/ path must exist. Items RF01-RF04
    and RF09 are recomputed from the SEC filing index over the window from
    params.since, else params.lookback_years before the latest filing: a
    hit there must be marked found and cite one of the hit accessions, and
    the index must reach back to the window's start.
    """
    rel = params.get("path", RED_FLAGS_PATH)
    path = _ws(workspace, rel)
    if not path.is_file():
        return _result(False, f"{rel} not found", 0.0)
    ciks = [c for c in (_cik_short(x) for x in params.get("ciks") or []) if c]
    if not ciks:
        found: set[str] = set()
        for key, default in (("comps", COMPS_PATH), ("spreads", SPREADS_PATH)):
            p = _ws(workspace, params.get(key, default))
            if p.is_file():
                found |= {_cik_short(r.get("cik")) for r in _read_csv(p)}
        ciks = sorted(found - {""}, key=int)
    if not ciks:
        return _result(False, "no companies to check (give params.ciks or build comps.csv)", 0.0)
    tables = _checklist_tables(path.read_text(encoding="utf-8"))
    lookback = params.get("lookback_years")
    allowed = RED_FLAG_STATUSES if params.get("allow_open") else RED_FLAG_STATUSES - {"open"}
    failures, checked = [], 0
    for cik in ciks:
        table = tables.get(cik)
        if table is None:
            failures.append(f"no section for CIK {cik}")
            continue
        index, uncached = _filing_index(workspace, cik)
        since = params.get("since", "")
        if index is None:
            failures.append(f"CIK {cik}: no filing index source to verify against")
            index = []
        else:
            latest = max((f["filed"] for f in index if f["filed"]), default="")
            if not since and lookback and latest:
                since = _years_back(latest, int(lookback))
            short = [str(p.get("name")) for p in uncached
                     if not since or str(p.get("filingTo", "")) >= since]
            if short:
                failures.append(f"CIK {cik}: filing index not loaded back to {since or 'the start'} "
                                f"({', '.join(short)}); call edgar_submissions with since")
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
            forged = cited - known if index else set()
            if forged:
                failures.append(f"CIK {cik} {item}: accession not in filing index: "
                                f"{', '.join(sorted(forged))}")
            files = [p.rstrip(".") for p in INPUT_REF_RE.findall(evidence)]
            absent = [p for p in files if (t := _inside(workspace, p)) is None or not t.is_file()]
            if absent:
                failures.append(f"CIK {cik} {item}: no such input file: {', '.join(absent)}")
            if status == "found" and not (cited or CLAIM_GROUP_RE.search(evidence) or files):
                failures.append(f"CIK {cik} {item}: found without evidence")
            if item in RED_FLAG_AUTO and index:
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
      web           uri is a page the engagement fetched: a web source in the
                    claim ledger with that uri
    """
    rel = params.get("path", SOURCES_PATH)
    path = _ws(workspace, rel)
    if not path.is_file():
        return _result(False, f"{rel} not found", 0.0)
    rows = _read_csv(path)
    if not rows:
        return _result(False, "source inventory is empty", 0.0)
    failures, seen, index_cache = [], set(), {}
    web_uris: set[str] | None = None
    for row in rows:
        sid, kind, uri = row.get("source_id", ""), row.get("kind", ""), row.get("uri", "")
        if not sid or sid in seen:
            failures.append(f"{sid or '(blank)'}: missing or duplicate source_id")
            continue
        seen.add(sid)
        cik = _cik_short(row.get("cik"))
        if kind == "edgar_filing":
            if cik not in index_cache:
                index_cache[cik] = _filing_index(workspace, cik)[0] if cik else None
            index = index_cache[cik]
            if index is None:
                failures.append(f"{sid}: no filing index for CIK {cik}")
                continue
            acc = row.get("accession", "")
            filing = next((f for f in index if f["accession"] == acc), None)
            if filing is None:
                failures.append(f"{sid}: accession {acc} not filed by CIK {cik}")
                continue
            if filing["form"] != row.get("form") or filing["filed"] != row.get("filed"):
                failures.append(f"{sid}: form/date {row.get('form')}/{row.get('filed')} != "
                                f"{filing['form']}/{filing['filed']}")
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
            target = _inside(workspace, uri) if uri.startswith("inputs/") else None
            if target is None or not target.is_file():
                failures.append(f"{sid}: {uri} is not a file under inputs/")
            elif _sha256(target) != row.get("sha256", "").lower():
                failures.append(f"{sid}: sha256 does not match {uri}")
        elif kind == "web":
            if web_uris is None:
                ledger = _ledger(workspace)
                web_uris = {s.uri for s in ledger.sources if s.kind == "web"} if ledger else set()
            if not re.match(r"https?://", uri):
                failures.append(f"{sid}: web source without an http(s) uri")
            elif uri not in web_uris:
                failures.append(f"{sid}: {uri} was never retrieved in this engagement "
                                "(no web source in the claim ledger)")
        else:
            failures.append(f"{sid}: unknown kind {kind!r}")
    score = (len(rows) - len(failures)) / len(rows)
    details = f"{len(rows) - len(failures)}/{len(rows)} sources resolve"
    if failures:
        details += ": " + "; ".join(failures[:10])
    return _result(not failures, details, round(max(score, 0.0), 4))


# File types the kit's read_document turns into text (agentkit/tools/documents.py).
READABLE_SUFFIXES = set(KIT_TEXT_SUFFIXES) | {".html", ".htm", ".json", ".docx", ""}


def dataroom_index_complete(workspace: Path, params: dict, *, run=None) -> dict:
    """The data-room index covers 100% of files under params.root (default
    inputs/dataroom) with matching sha256 and size, lists nothing that is not
    there, marks readable=yes only what the kit can read, and every other
    file (readable != yes) carries a note."""
    rel = params.get("path", DATAROOM_INDEX_PATH)
    root_rel = params.get("root", "inputs/dataroom")
    path = _ws(workspace, rel)
    root = _ws(workspace, root_rel)
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
        readable = row.get("readable", "").strip().lower()
        if readable == "yes" and p.suffix.lower() not in READABLE_SUFFIXES:
            failures.append(f"marked readable but no reader for {p.suffix.lower()}: {rel_file}")
        if readable != "yes" and not row.get("note", "").strip():
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
