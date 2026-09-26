"""Pinned scope and match for the demo statement of work.

The demo buyer uploads one known SOW (the Luckin Coffee variant-view pitch).
Recognising it here keeps the recorded flow deterministic: the upload always
fills the same milestones, budget and deadline, and the matchmaker always
recommends the finance research agent. Everything else goes through the
normal parser and matchmaker.
"""
from datetime import date, timedelta

DEMO_MARKER = "luckin coffee variant view"
DEMO_AGENT_NAME = "Ledgerline Financial Analyst"
DEMO_BUDGET_USDC = 75
DEMO_BUSINESS_DAYS = 20

_MILESTONES = [
    ("Research baseline and data plan", 15, [
        "Latest company release and annual filing identified with dates; research cutoff recorded",
        "Dated, attributable consensus snapshot with gaps listed",
        "Two alternative-data series documented with access rights and limitations",
    ]),
    ("Industry and alternative-data evidence", 25, [
        "Store and pricing series with dated observations, coverage and dedup rules",
        "Store-count estimate compared with an official count, with absolute and % error",
        "One-page evidence note on which signals drive the model",
    ]),
    ("Operating model and valuation", 30, [
        "Historicals reconcile to company disclosures; 2026E-2028E forecasts",
        "Per-cup bridge from ASP to operating profit per cup",
        "Bear and base cases tie through the valuation bridge to a dated price",
    ]),
    ("One-page draft and charts", 20, [
        "One US Letter page with variant view versus dated consensus",
        "Four labeled charts: stores vs consensus, per-cup bridge, consumption, footprint",
        "Every material number sourced or labeled as an assumption",
    ]),
    ("Final validation and handoff", 10, [
        "Review defects corrected or resolved in writing",
        "Page, workbook and evidence pack agree on dates, price, target and store numbers",
        "Buyer can reproduce the target price from the workbook",
    ]),
]


def is_demo(text: str | None) -> bool:
    return DEMO_MARKER in (text or "").lower()


def _add_business_days(start: date, days: int) -> date:
    d = start
    while days > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            days -= 1
    return d


def scope(today: date | None = None) -> dict:
    today = today or date.today()
    due = _add_business_days(today, DEMO_BUSINESS_DAYS)
    return {
        "method": "llm",
        "title": "Luckin Coffee Variant View Investment Pitch",
        "outcome": ("A decision-ready, one-page investment pitch on Luckin Coffee (LKNCY) showing whether "
                    "and why its operating and valuation outcomes differ from a dated market-consensus "
                    "baseline, backed by a reproducible model and an evidence pack."),
        "brief": "Luckin Coffee (LKNCY) variant-view equity research pitch with model and evidence pack.",
        "category_key": "research", "category_label": "Research", "agent_category": "Research",
        "milestones": [{"title": t, "acceptance": crit, "amount_usdc": round(DEMO_BUDGET_USDC * pct / 100, 2)}
                       for t, pct, crit in _MILESTONES],
        "acceptance": [],
        "deadline": {"date": due.isoformat(), "mode": "date",
                     "text": f"{DEMO_BUSINESS_DAYS} business days (target Day {DEMO_BUSINESS_DAYS})"},
        "budget_usdc": DEMO_BUDGET_USDC,
        "warnings": [],
        "notes": [],
    }


def job_is_demo(job: dict) -> bool:
    parts = [job.get("outcome") or "", job.get("brief") or ""]
    parts += [m.get("title", "") for m in job.get("milestones") or []]
    text = " ".join(parts).lower()
    return "luckin" in text or is_demo(text)
