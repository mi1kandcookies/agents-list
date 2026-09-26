"""
specialists/financial_research/checks.py - acceptance checks for the
financial-research specialist.

Each check is `fn(workspace, params, *, run=None) -> {"passed", "details",
"score"}` and is listed in CHECK_DEFS. Checks never trust the agent's own
report: every figure is checked against the cached SEC JSON
(.agentkit/edgar/, falling back to inputs/edgar/), the client's market data,
the claim ledger and the data-room files themselves. The recomputation here
(fiscal years, debt, multiples) is written independently of tools.py so a
bug in one does not hide in the other. The one shared piece is the pitch's
DCF formula, tools.dcf_value_per_share, so a tool and its check can never
disagree about the model itself; its inputs are re-derived here.

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
    pitch_valuation_recompute the pitch's price ties to its source, the reverse DCF
                              inputs to XBRL facts, and every value recomputes
    variant_view_grounded     the variant view differs from what is priced in, is
                              cited, falsifiable and dated, and the pitch agrees
"""
from __future__ import annotations

import csv
import functools
import hashlib
import json
import math
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from agentkit.errors import PolicyViolation
from agentkit.ledger import Ledger, normalize_text
from agentkit.policy import jail_path
from agentkit.tools.documents import TEXT_SUFFIXES as KIT_TEXT_SUFFIXES

from .tools import dcf_value_per_share

SPREADS_PATH = "deliverables/m2-spreads-comps/facts.csv"
COMPS_PATH = "deliverables/m2-spreads-comps/comps.csv"
MARKET_DATA_PATH = "inputs/market_data.csv"
MEMO_PATH = "deliverables/m3-diligence-memo/memo.md"
RED_FLAGS_PATH = "deliverables/m3-diligence-memo/red_flags.md"
SOURCES_PATH = "deliverables/m1-plan-sources/source_inventory.csv"
DATAROOM_INDEX_PATH = "deliverables/m1-plan-sources/dataroom_index.csv"
PITCH_PATH = "deliverables/m4-stock-pitch/pitch.md"
SNAPSHOT_PATH = "deliverables/m4-stock-pitch/market_snapshot.json"
IMPLIED_PATH = "deliverables/m4-stock-pitch/market_implied.json"
VALUATION_PATH = "deliverables/m4-stock-pitch/valuation.csv"
VARIANT_VIEW_PATH = "deliverables/m4-stock-pitch/variant_view.json"
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
DEBT_METRICS = ("long_term_debt_noncurrent", "long_term_debt_current", "long_term_debt_total",
                "debt_current", "short_term_borrowings")
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
# A minus sign counts only when attached to the figure ("-4.5%", "-$12.3"),
# so the dash of a bullet ("- 14.1% [F:...]") is not read as one.
FIGURE_REF_RE = re.compile(
    r"(?:(?<![\w.])(?P<neg>[-−])(?=[$\d]))?(?P<cur>\$)?\s?(?P<num>\d[\d,]*(?:\.\d+)?)\s?"
    r"(?P<suf>%|percent|x|×|times|bn|billion|mm|mn|million|m|k|thousand|b)?"
    r"\s*\[F:(?P<cik>\d+):(?P<fy>\d{4}):(?P<col>[a-z_]+)\]", re.I)
# Any amount in prose: optional sign and $, a number, an optional unit.
AMOUNT_RE = re.compile(
    r"(?<![\w.,])(?P<neg>[-−])?(?P<cur>\$)?\s?(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?:\s?(?P<suf>%|per\s?cent\b|pct\b|bps\b|basis\s+points?\b|pp\b|ppts?\b|"
    r"percentage\s+points?\b|x(?![A-Za-z])|×|times\b|"
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
    data = _source_file(workspace, f"{kind}/CIK{c}.json") if c else None
    return data if isinstance(data, dict) else None


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
    """Fiscal year -> year-end, worked out from the SEC source: each 10-K's
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


def _total_debt(values: dict[str, Any]) -> Any:
    """Each borrowing counted once: all current debt with the noncurrent part;
    else its pieces; else the long-term total (which already holds the
    current portion) plus short-term borrowings. None when nothing is reported."""
    ncur, cur = values.get("long_term_debt_noncurrent"), values.get("debt_current")
    ltdc, stb, ltdt = (values.get("long_term_debt_current"), values.get("short_term_borrowings"),
                       values.get("long_term_debt_total"))
    if ncur is not None and cur is not None:
        return ncur + cur
    if ncur is not None:
        return ncur + (ltdc or 0) + (stb or 0)
    if ltdt is not None:
        return ltdt + (stb or 0)
    if cur is not None:
        return cur
    if ltdc is not None or stb is not None:
        return (ltdc or 0) + (stb or 0)
    return None


def _expected_derived(row: dict[str, str]) -> dict[str, float | None]:
    def f(col: str) -> float | None:
        v = _num(row.get(col))
        return float(v) if v is not None else None

    def div(a, b, positive=False):
        if a is None or b is None or b == 0 or (positive and (a <= 0 or b <= 0)):
            return None
        return a / b

    debt = _total_debt({col: f(col) for col in DEBT_METRICS})
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
            else "bps" if suffix in ("bps", "basispoint", "basispoints")
            else "pp" if suffix in ("pp", "ppt", "ppts", "percentagepoint", "percentagepoints")
            else "")
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

    Every figure ($, %, x, times, million/billion, M/B, bps, pp, or a bare
    number of four or more digits other than a year) is either followed by its own
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


WINDOW_RE = re.compile(r"(?i)\bwindow\b\D*?(\d{4}-\d{2}-\d{2})\s*(?:to|through|-|–|\.\.)\s*"
                       r"(\d{4}-\d{2}-\d{2})")


def _checklist_tables(text: str) -> dict[str, dict[str, dict[str, str]]]:
    """{cik: {RFxx: {status, evidence}, "window": {from, to}}} from
    '## Name (CIK n)' sections ("Window: 2022-11-04 to 2025-11-04")."""
    out: dict[str, dict[str, dict[str, str]]] = {}
    current = None
    for line in text.splitlines():
        head = re.match(r"^#{2,3}\s.*\bCIK\s*(\d+)", line.strip())
        if head:
            current = str(int(head.group(1)))
            out.setdefault(current, {})
            continue
        window = WINDOW_RE.search(line) if current else None
        if window and "window" not in out[current]:
            out[current]["window"] = {"from": window.group(1), "to": window.group(2)}
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
    hit there must be marked found and cite one of the hit accessions, the
    index must reach back to the window's start, and each section must state
    a window ("Window: <from> to <to>") that starts no later.
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
            stated = table.get("window")
            if since and stated is None:
                failures.append(f"CIK {cik}: state the review window (Window: {since} to {latest})")
            elif since and stated["from"] > since:
                failures.append(f"CIK {cik}: stated window starts {stated['from']}, after {since}")
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


# --- M4: stock pitch ----------------------------------------------------------------

QUOTE_HOSTS = {"yahoo": ("query1.finance.yahoo.com", "query2.finance.yahoo.com"),
               "nasdaq": ("api.nasdaq.com",)}
PITCH_SCENARIOS = ["bear", "base", "bull"]
DCF_RATES = ("operating_margin", "tax_rate", "reinvestment_rate", "discount_rate", "terminal_growth")
# Tolerances: value_per_share within $0.01 or 0.01%, whichever is larger
# (it is written to two decimals); upside_pct within 0.01 points; the value
# at the implied CAGR within 0.01% of the price (the solver's stopping
# rule); a rate copied into variant_view.json within 0.0005 (0.05 points).
VALUE_TOLERANCE, VALUE_REL_TOLERANCE = 0.01, 1e-4
UPSIDE_TOLERANCE = 0.01
IMPLIED_PRICE_TOLERANCE = 1e-4
RATE_TOLERANCE = 0.0005
DIRECTION_RE = re.compile(r"(?i)\bdirection\b\s*(?:\*\*|__)?\s*:\s*(?:\*\*|__)?\s*(long|short|pass)\b")
PERCENT_RE = re.compile(r"(?<![\w.])[-−]?(\d+(?:\.\d+)?)\s?%")
HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
# A catalyst says when: a year (2026, FY2026, Q3 2026), FY26 or 3Q26.
DATED_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)|\bFY\s?\d{2}(?!\d)|\b[1-4]Q\s?\d{2}(?!\d)", re.I)
MIN_FALSIFIER_CHARS = 15


def _json_doc(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _float(value: Any) -> float | None:
    """A finite float from a JSON or CSV value, else None."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(str(value).strip().replace(",", "").replace("−", "-"))
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _tkey(ticker: Any) -> str:
    return re.sub(r"[./]", "-", str(ticker or "").strip().upper())


def _value_of(doc: dict, key: str) -> Any:
    """doc[key]["value"] of a provenance block in market_implied.json."""
    block = doc.get(key)
    return block.get("value") if isinstance(block, dict) else None


def _dcf_args(rates: dict, amounts: dict) -> dict[str, Any]:
    """dcf_value_per_share keyword arguments from parsed assumptions and the
    shared amounts; ValueError when one is missing or not a number."""
    args: dict[str, Any] = {k: _float(rates.get(k)) for k in DCF_RATES}
    args.update({k: _float(v) for k, v in amounts.items()})
    years = _float(rates.get("years"))
    bad = [k for k, v in args.items() if v is None]
    if years is None or not years.is_integer():
        bad.append("years")
    if bad:
        raise ValueError(f"missing or not a number: {', '.join(bad)}")
    return {**args, "years": int(years)}


def _quoted_price(source: str, text: str) -> tuple[Decimal | None, str]:
    """(price, symbol) a raw quote response states, read here apart from the tool."""
    try:
        data = json.loads(text)
        if source == "yahoo":
            meta = data["chart"]["result"][0]["meta"]
            return _num(meta["regularMarketPrice"]), str(meta.get("symbol") or "")
        body = data["data"]
        return _num(body["primaryData"]["lastSalePrice"]), str(body.get("symbol") or "")
    except (ValueError, KeyError, IndexError, TypeError):
        return None, ""


def _snapshot_problem(workspace: Path, snap: dict, market_rel: str) -> str:
    """'' when market_snapshot.json ties to its source: the client's market
    data row, or the quote response market_quote registered in the ledger."""
    ticker = str(snap.get("ticker") or "").strip().upper()
    price = _num(snap.get("price"))
    if not ticker or price is None or price <= 0:
        return "needs a ticker and a positive price"
    source, url = snap.get("source"), str(snap.get("url") or "")
    if source == "client":
        path = _ws(workspace, market_rel)
        rows = _read_csv(path) if path.is_file() else []
        row = next((r for r in rows if _tkey(r.get("ticker")) == _tkey(ticker)), None)
        if row is None:
            return f"its source is the client's market data, but {market_rel} has no row for {ticker}"
        if _num(row.get("price")) != price:
            return f"price {snap.get('price')} != {row.get('price')} in {market_rel}"
        if (row.get("price_as_of") or "").strip() != str(snap.get("as_of") or "").strip():
            return f"as_of {snap.get('as_of')!r} != price_as_of {row.get('price_as_of')!r} in {market_rel}"
        return ""
    if source not in QUOTE_HOSTS:
        return f"unknown quote source {source!r}"
    parts = urlsplit(url)
    if (parts.scheme != "https" or parts.hostname not in QUOTE_HOSTS[source]
            or _tkey(ticker) not in [_tkey(p) for p in parts.path.split("/")]):
        return f"{url or '(no url)'} is not a {source} quote URL for {ticker}"
    ledger = _ledger(workspace)
    src = ledger.source(str(snap.get("source_id") or "")) if ledger else None
    if src is None or src.uri != url or src.kind != "tool":
        src = next((s for s in reversed(ledger.sources) if s.uri == url and s.kind == "tool"),
                   None) if ledger else None
    if src is None:
        return f"no ledger source for {url}: a quote must come from market_quote"
    problem = ledger.snapshot_problem(src.id)
    if problem:
        return problem
    quoted, symbol = _quoted_price(source, ledger.snapshot(src.id))
    if quoted is None:
        return f"source {src.id} states no price"
    if symbol and _tkey(symbol) != _tkey(ticker):
        return f"source {src.id} quotes {symbol}, not {ticker}"
    if quoted != price:
        return f"price {snap.get('price')} != {quoted} in source {src.id}"
    return ""


def _fact_problem(workspace: Path, fact: Any, metric: str, fiscal_year: int | None, cik: str,
                  cache: dict, tolerance: Decimal) -> str:
    """'' when a provenance record in market_implied.json is the XBRL fact it
    says it is: metric, fiscal year, then tag, period, accession and value
    through the spreads tie-out (_row_problem)."""
    if not isinstance(fact, dict):
        return "missing"
    if fact.get("metric") != metric:
        return f"metric is {fact.get('metric')!r}, not {metric}"
    if str(fact.get("fiscal_year")) != str(fiscal_year):
        return f"fiscal year {fact.get('fiscal_year')} is not FY{fiscal_year}"
    row = {k: "" if v is None else v for k, v in fact.items()}
    return _row_problem(workspace, {**row, "cik": cik, "note": ""}, cache, tolerance)


def pitch_valuation_recompute(workspace: Path, params: dict, *, run=None) -> dict:
    """The pitch's price, reverse DCF and scenarios tie to their sources and
    recompute.

    market_snapshot.json ties to its source: the client's market data row
    (price and as-of date), or the quote response market_quote registered
    in the claim ledger (snapshot intact, a quote URL for the ticker, the
    same symbol and price). market_implied.json: same price and ticker, a
    ticker the CIK's filing index lists; revenue and diluted shares are
    XBRL facts (tag, period, accession, value) of the latest fiscal year the
    SEC source reports revenue for; every cash and debt component the source
    reports for that year is listed, each a fact, and net debt recomputes
    from them; the value at implied_revenue_cagr is within 0.01% of the
    price. valuation.csv: exactly one row per params.scenarios (default
    bear, base, bull), each with market_implied.json's base revenue, net
    debt and shares and the snapshot's price, value_per_share within $0.01
    (or 0.01%) and upside_pct within 0.01 points of the recomputation, and
    values ranking bear <= base <= bull.

    params: snapshot, implied, valuation, market_data, scenarios, tolerance
    (XBRL value tolerance, default 0.5).
    """
    rels = {"snapshot": params.get("snapshot", SNAPSHOT_PATH),
            "implied": params.get("implied", IMPLIED_PATH),
            "valuation": params.get("valuation", VALUATION_PATH)}
    paths = {key: _ws(workspace, rel) for key, rel in rels.items()}
    market_rel = params.get("market_data", MARKET_DATA_PATH)
    _ws(workspace, market_rel)
    missing = [rels[key] for key, p in paths.items() if not p.is_file()]
    if missing:
        return _result(False, f"missing: {', '.join(missing)}", 0.0)
    snap, implied = _json_doc(paths["snapshot"]), _json_doc(paths["implied"])
    if not isinstance(snap, dict) or not isinstance(implied, dict):
        return _result(False, f"{rels['snapshot']} and {rels['implied']} must hold JSON objects", 0.0)
    rows = _read_csv(paths["valuation"])
    tolerance = Decimal(str(params.get("tolerance", "0.5")))
    failures: list[str] = []
    checked = 0

    def check(problem: str, where: str) -> None:
        nonlocal checked
        checked += 1
        if problem:
            failures.append(f"{where}: {problem}")

    check(_snapshot_problem(workspace, snap, market_rel), "market_snapshot.json")
    price = _float(snap.get("price")) or 0.0
    ticker = str(snap.get("ticker") or "").strip().upper()
    implied_price = _float(implied.get("price"))
    check("" if implied_price is not None and math.isclose(implied_price, price, rel_tol=1e-9)
          else f"price {implied.get('price')} != market_snapshot.json {snap.get('price')}",
          "market_implied.json")
    check("" if _tkey(implied.get("ticker")) == _tkey(ticker)
          else f"ticker {implied.get('ticker')!r} != market_snapshot.json {ticker!r}", "market_implied.json")
    cik = _cik_short(implied.get("cik"))
    listed = [str(t) for t in (_source_json(workspace, "submissions", cik) or {}).get("tickers") or []] \
        if cik else []
    if listed:
        check("" if _tkey(ticker) in {_tkey(t) for t in listed}
              else f"CIK {cik} trades as {', '.join(listed)}, not {ticker}", "market_implied.json")

    cache: dict = {}
    facts, ends = _company(workspace, cik, cache) if cik else (None, {})
    try:
        fiscal_year: int | None = int(implied.get("fiscal_year"))
    except (TypeError, ValueError):
        fiscal_year = None
    if facts is None:
        check(f"no companyfacts source for CIK {cik or '(none)'}", "market_implied.json")
    else:
        latest = max((y for y in ends if _source_reports(facts, ends, "revenue", y)), default=None)
        check("" if fiscal_year is not None and fiscal_year == latest
              else f"fiscal_year {implied.get('fiscal_year')} is not the latest fiscal year the SEC "
                   f"source reports revenue for (FY{latest})", "market_implied.json")
        for key, metric in (("base_revenue", "revenue"), ("shares", "shares_diluted")):
            check(_fact_problem(workspace, implied.get(key), metric, fiscal_year, cik, cache,
                                tolerance), key)
        net = implied.get("net_debt") if isinstance(implied.get("net_debt"), dict) else {}
        components: dict[str, Decimal] = {}
        for comp in net.get("components") or []:
            metric = comp.get("metric") if isinstance(comp, dict) else None
            if metric not in ("cash", *DEBT_METRICS) or metric in components:
                check(f"unexpected or repeated component {metric!r}", "net_debt")
                continue
            problem = _fact_problem(workspace, comp, metric, fiscal_year, cik, cache, tolerance)
            check(problem, f"net_debt {metric}")
            if not problem:
                components[metric] = _num(comp.get("value"))
        for metric in ("cash", *DEBT_METRICS):
            if metric not in components and fiscal_year is not None \
                    and _source_reports(facts, ends, metric, fiscal_year):
                check(f"leaves out {metric}, which the SEC source reports for FY{fiscal_year}",
                      "net_debt")
        debt = _total_debt({m: v for m, v in components.items() if m != "cash"}) or Decimal(0)
        cash = components.get("cash") or Decimal(0)
        check("" if _num(net.get("value")) == debt - cash
              else f"net debt {net.get('value')} != debt {debt} - cash {cash}", "net_debt")

    amounts = {key: _value_of(implied, key) for key in ("base_revenue", "net_debt", "shares")}
    assumptions = implied.get("assumptions") if isinstance(implied.get("assumptions"), dict) else {}
    try:
        value = dcf_value_per_share(revenue_cagr=_float(implied.get("implied_revenue_cagr")),
                                    **_dcf_args(assumptions, amounts))
    except (TypeError, ValueError) as exc:
        check(f"cannot recompute the implied CAGR: {exc}", "market_implied.json")
    else:
        check("" if abs(value - price) <= IMPLIED_PRICE_TOLERANCE * price + 1e-9
              else f"at implied_revenue_cagr {implied.get('implied_revenue_cagr')} the DCF gives "
                   f"{value:.4f}, not the price {price:g}", "market_implied.json")
        reported = _float(implied.get("value_per_share_at_implied"))
        check("" if reported is not None
              and abs(reported - value) <= max(VALUE_TOLERANCE, VALUE_REL_TOLERANCE * abs(value))
              else f"value_per_share_at_implied {implied.get('value_per_share_at_implied')} != "
                   f"recomputed {value:.4f}", "market_implied.json")

    names = [(r.get("scenario") or "").strip().lower() for r in rows]
    for name in [str(s).lower() for s in params.get("scenarios") or PITCH_SCENARIOS]:
        check("" if names.count(name) == 1 else f"needs one {name} row, found {names.count(name)}",
              "valuation.csv")
    values: dict[str, float] = {}
    for line, (name, row) in enumerate(zip(names, rows), start=2):
        where = f"valuation.csv line {line} ({name or 'no scenario'})"
        for key in ("base_revenue", "net_debt", "shares"):
            check("" if _num(row.get(key)) is not None and _num(row.get(key)) == _num(amounts[key])
                  else f"{key} {row.get(key)!r} != market_implied.json {amounts[key]}", where)
        row_price = _float(row.get("price"))
        check("" if row_price is not None and math.isclose(row_price, price, rel_tol=1e-9)
              else f"price {row.get('price')!r} != market_snapshot.json {snap.get('price')}", where)
        try:
            value = dcf_value_per_share(revenue_cagr=_float(row.get("revenue_cagr")),
                                        **_dcf_args(row, {k: row.get(k) for k in amounts}))
        except (TypeError, ValueError) as exc:
            check(f"cannot recompute: {exc}", where)
            continue
        reported = _float(row.get("value_per_share"))
        check("" if reported is not None
              and abs(reported - value) <= max(VALUE_TOLERANCE, VALUE_REL_TOLERANCE * abs(value))
              else f"value_per_share {row.get('value_per_share')!r} != recomputed {value:.2f}", where)
        upside = (value / price - 1) * 100 if price else math.nan
        reported_upside = _float(row.get("upside_pct"))
        check("" if reported_upside is not None and abs(reported_upside - upside) <= UPSIDE_TOLERANCE + 1e-9
              else f"upside_pct {row.get('upside_pct')!r} != recomputed {upside:.2f}", where)
        values[name] = reported if reported is not None else value
    ranked = [values[n] for n in PITCH_SCENARIOS if n in values]
    if len(ranked) == len(PITCH_SCENARIOS):
        check("" if ranked == sorted(ranked) else
              "values per share do not rank bear <= base <= bull", "valuation.csv")
    score = (checked - len(failures)) / checked if checked else 0.0
    details = f"{checked - len(failures)}/{checked} pitch figures tie out"
    if failures:
        details += f"; {len(failures)} failed: " + "; ".join(failures[:10])
    return _result(not failures, details, round(max(score, 0.0), 4))


def _view_value(value: Any) -> tuple[float | None, str]:
    """(number, metric) of our_view: a number is a revenue CAGR, and so is an
    object whose metric names revenue growth ("revenue_cagr", "revenue CAGR")."""
    if not isinstance(value, dict):
        return _float(value), "revenue_cagr"
    metric = re.sub(r"[^a-z]+", "_", str(value.get("metric") or "revenue_cagr").lower())
    growth = "revenue" in metric and ("cagr" in metric or "growth" in metric)
    return _float(value.get("value")), "revenue_cagr" if growth else metric.strip("_")


def _item_text(item: Any) -> str:
    if isinstance(item, dict):
        return " ".join(str(v) for v in item.values()
                        if isinstance(v, (str, int, float)) and not isinstance(v, bool)).strip()
    return str(item).strip() if isinstance(item, (str, int, float)) else ""


def _evidence_claims(item: Any) -> set[str]:
    """Claim ids an evidence item cites: [C#] in its text, or a claims list."""
    listed: list[Any] = []
    if isinstance(item, dict):
        claims = item.get("claims")
        listed = claims if isinstance(claims, list) else [item.get("claim")]
    ids = {c for group in CLAIM_GROUP_RE.findall(_item_text(item)) for c in re.findall(r"C\d+", group)}
    return ids | {c for x in listed if isinstance(x, str) for c in re.findall(r"\bC\d+\b", x)}


def _section(text: str, title: str) -> str:
    """The text under the first heading containing `title` (lowercase), up
    to the next heading of the same or a higher level."""
    body: list[str] | None = None
    level = 0
    for line in text.splitlines():
        m = HEADING_RE.match(line)
        if m and body is not None and len(m.group(1)) <= level:
            break
        if m and body is None and title in " ".join(m.group(2).lower().split()):
            body, level = [], len(m.group(1))
            continue
        if body is not None:
            body.append(line)
    return "\n".join(body or [])


def _states_percent(text: str, target: float) -> bool:
    """Whether the text states `target` percent at the precision it shows
    (11.5% or 12% for 11.55; the sign may be in words)."""
    for m in PERCENT_RE.finditer(text):
        shown = m.group(1)
        places = len(shown.split(".")[1]) if "." in shown else 0
        if abs(float(shown) - abs(target)) <= 0.5 * 10 ** -places + 1e-9:
            return True
    return False


def variant_view_grounded(workspace: Path, params: dict, *, run=None) -> dict:
    """The variant view differs from what the price implies, is evidenced
    and falsifiable, and the pitch says the same.

    variant_view.json: market_implied equals market_implied.json's
    implied_revenue_cagr (to 0.0005); our_view, a revenue CAGR (a number or
    {"metric": "revenue_cagr", "value": ...}), is at least params.min_delta
    (default 0.02: two percentage points) away from it and is the base
    scenario's revenue_cagr in valuation.csv; delta = our_view -
    market_implied; direction is long when the base case's upside_pct is
    above params.long_above (10), short below params.short_below (-10),
    else pass; at least three distinct evidence items, each citing claims
    in the ledger ([C#] in its text, or "claims": ["C#"]); at least two
    falsifiers of 15 characters or more; at least two catalysts, each dated
    (a year, FY26 or 3Q26). pitch.md has a "Direction: <direction>" line
    (and no other direction) and states the implied CAGR as a percentage in
    its What the market is pricing in section.

    params: path, implied, valuation, pitch, min_delta, long_above,
    short_below, min_evidence, min_falsifiers, min_catalysts.
    """
    rels = {"view": params.get("path", VARIANT_VIEW_PATH), "implied": params.get("implied", IMPLIED_PATH),
            "valuation": params.get("valuation", VALUATION_PATH), "pitch": params.get("pitch", PITCH_PATH)}
    paths = {key: _ws(workspace, rel) for key, rel in rels.items()}
    missing = [rels[key] for key, p in paths.items() if not p.is_file()]
    if missing:
        return _result(False, f"missing: {', '.join(missing)}", 0.0)
    view, implied = _json_doc(paths["view"]), _json_doc(paths["implied"])
    if not isinstance(view, dict) or not isinstance(implied, dict):
        return _result(False, f"{rels['view']} and {rels['implied']} must hold JSON objects", 0.0)
    implied_cagr = _float(implied.get("implied_revenue_cagr"))
    if implied_cagr is None:
        return _result(False, f"{rels['implied']} has no implied_revenue_cagr", 0.0)
    scenarios = {(r.get("scenario") or "").strip().lower(): r for r in _read_csv(paths["valuation"])}
    base = scenarios.get("base") or {}
    pitch = paths["pitch"].read_text(encoding="utf-8")
    min_delta = float(params.get("min_delta", 0.02))
    long_above, short_below = float(params.get("long_above", 10)), float(params.get("short_below", -10))
    failures: list[str] = []
    checked = 0

    def check(problem: str, where: str) -> None:
        nonlocal checked
        checked += 1
        if problem:
            failures.append(f"{where}: {problem}")

    market = _float(view.get("market_implied"))
    check("" if market is not None and abs(market - implied_cagr) <= RATE_TOLERANCE
          else f"{view.get('market_implied')!r} != market_implied.json {implied_cagr:.4f} (a fraction: "
               "0.12 for 12%)", "market_implied")
    ours, metric = _view_value(view.get("our_view"))
    if ours is None or metric != "revenue_cagr":
        check("must be a revenue CAGR: a fraction, or {\"metric\": \"revenue_cagr\", \"value\": ...}",
              "our_view")
    else:
        delta = ours - implied_cagr
        check("" if abs(delta) >= min_delta - 1e-12
              else f"{ours:.4f} is within {min_delta * 100:g} points of the {implied_cagr:.4f} the price "
                   "implies: that is not a variant view", "our_view")
        reported_delta = _float(view.get("delta"))
        check("" if reported_delta is not None and abs(reported_delta - delta) <= RATE_TOLERANCE
              else f"{view.get('delta')!r} != our_view - market_implied = {delta:.4f}", "delta")
        base_cagr = _float(base.get("revenue_cagr"))
        check("" if base_cagr is not None and abs(base_cagr - ours) <= RATE_TOLERANCE
              else f"the base scenario's revenue_cagr ({base.get('revenue_cagr') or 'none'}) is not "
                   f"our_view {ours:.4f}", "our_view")
    direction = str(view.get("direction") or "").strip().lower()
    upside = _float(base.get("upside_pct"))
    if upside is None:
        check("valuation.csv has no base scenario upside_pct", "direction")
    else:
        expected = "long" if upside > long_above else "short" if upside < short_below else "pass"
        check("" if direction == expected
              else f"{direction or '(none)'!r}, but the base case upside of {upside:+.2f}% makes it "
                   f"{expected}", "direction")
    if view.get("ticker"):
        check("" if _tkey(view["ticker"]) == _tkey(implied.get("ticker"))
              else f"{view['ticker']!r} is not market_implied.json's {implied.get('ticker')!r}", "ticker")

    ledger = _ledger(workspace)
    known = {c.id for c in ledger.claims} if ledger else set()
    evidence = view.get("evidence") if isinstance(view.get("evidence"), list) else []
    need = int(params.get("min_evidence", 3))
    distinct = {json.dumps(e, sort_keys=True) for e in evidence}
    check("" if len(distinct) >= need else f"{len(distinct)} distinct items, need {need}", "evidence")
    for i, item in enumerate(evidence, start=1):
        ids = _evidence_claims(item)
        unknown = sorted(ids - known)
        check("cites no [C#] claim" if not ids else
              f"cites {', '.join(unknown)}, not in the claim ledger" if unknown else "", f"evidence {i}")
    falsifiers = view.get("falsifiers") if isinstance(view.get("falsifiers"), list) else []
    need = int(params.get("min_falsifiers", 2))
    stated = {t for t in map(_item_text, falsifiers) if len(t) >= MIN_FALSIFIER_CHARS}
    check("" if len(stated) >= need else
          f"{len(stated)} distinct falsifiers of {MIN_FALSIFIER_CHARS}+ characters, need {need}",
          "falsifiers")
    catalysts = view.get("catalysts") if isinstance(view.get("catalysts"), list) else []
    need = int(params.get("min_catalysts", 2))
    texts = [_item_text(c) for c in catalysts]
    check("" if len({t for t in texts if t}) >= need else f"{len({t for t in texts if t})} distinct, "
          f"need {need}", "catalysts")
    for i, text in enumerate(texts, start=1):
        check("" if DATED_RE.search(text) else "does not say when (a year, quarter or date)",
              f"catalyst {i}")

    directions = {d.lower() for d in DIRECTION_RE.findall(pitch)}
    check("" if directions == {direction} else
          "has no 'Direction: long|short|pass' line" if not directions else
          f"states direction {', '.join(sorted(directions))}, variant_view.json {direction or '(none)'}",
          "pitch.md")
    check("" if _states_percent(_section(pitch, "what the market is pricing in"), implied_cagr * 100)
          else f"its What the market is pricing in section does not state the implied revenue CAGR "
               f"({implied_cagr * 100:.1f}%)", "pitch.md")
    score = (checked - len(failures)) / checked if checked else 0.0
    details = f"{checked - len(failures)}/{checked} variant-view checks pass"
    if failures:
        details += f"; {len(failures)} failed: " + "; ".join(failures[:10])
    return _result(not failures, details, round(max(score, 0.0), 4))


CHECK_DEFS = {
    "xbrl_tieout": xbrl_tieout,
    "comps_tie_to_xbrl": comps_tie_to_xbrl,
    "comps_recompute": comps_recompute,
    "memo_figures_match": memo_figures_match,
    "no_recommendation_language": no_recommendation_language,
    "red_flag_checklist": red_flag_checklist,
    "source_inventory_resolves": source_inventory_resolves,
    "dataroom_index_complete": dataroom_index_complete,
    "pitch_valuation_recompute": pitch_valuation_recompute,
    "variant_view_grounded": variant_view_grounded,
}
