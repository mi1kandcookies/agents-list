"""Guided-flow defaults and the rule-based estimate.

CATEGORIES is the single source for the category chips, keyword preselect,
default milestones and estimate constants. It is sent to the browser as JSON
(see routes.flow_config) so app/static/js/flow.js uses the same numbers;
``estimate()`` below and ``estimate()`` in flow.js implement the same rules
and must be kept in step.

The estimate is deliberately simple and deterministic: the escrowed total is
exactly the milestone sum, and the upper bound adds a revision buffer that
shrinks as the job is specified more precisely. A scoping agent will replace
this later; nothing here claims to be market data.
"""
from __future__ import annotations

import math
from datetime import date

# Days a delivered milestone stays open for review before funds release
# automatically. Placeholder until the escrow release policy is settled.
AUTO_RELEASE_DAYS = 7

# Revision buffer applied to the upper cost bound, by confidence level.
BUFFER = {"low": 0.30, "medium": 0.20, "good": 0.10}

CATEGORIES: list[dict] = [
    {
        "key": "research", "label": "Research", "agent_category": "Research",
        "hint": "Market scans, competitor teardowns, literature reviews",
        "keywords": ["research", "competitor", "market", "survey", "study", "analyse",
                     "analyze", "landscape", "review", "interview", "benchmark"],
        "suggested_budget": 600, "days_per_milestone": 3,
        "milestones": [
            {"title": "Scope and sources", "share": 0.25,
             "criteria": ["Agreed list of questions and sources"]},
            {"title": "Findings draft", "share": 0.40,
             "criteria": ["Every claim links to a source"]},
            {"title": "Final report", "share": 0.35,
             "criteria": ["Executive summary fits on one page"]},
        ],
    },
    {
        "key": "growth", "label": "Growth", "agent_category": "Marketing",
        "hint": "Acquisition experiments, SEO, outbound, lifecycle email",
        "keywords": ["growth", "marketing", "seo", "leads", "outbound", "campaign", "ads",
                     "funnel", "signup", "conversion", "newsletter", "acquisition"],
        "suggested_budget": 900, "days_per_milestone": 5,
        "milestones": [
            {"title": "Audit and plan", "share": 0.30,
             "criteria": ["Baseline metrics recorded"]},
            {"title": "First experiments live", "share": 0.40,
             "criteria": ["Each experiment has a success metric"]},
            {"title": "Results and next steps", "share": 0.30,
             "criteria": ["Results compared against the baseline"]},
        ],
    },
    {
        "key": "engineering", "label": "Engineering", "agent_category": "Development",
        "hint": "Features, integrations, bug fixes, internal tools",
        "keywords": ["build", "app", "api", "code", "bug", "feature", "integration", "website",
                     "backend", "frontend", "deploy", "script", "engineering", "migrate"],
        "suggested_budget": 1500, "days_per_milestone": 5,
        "milestones": [
            {"title": "Technical plan", "share": 0.20,
             "criteria": ["Plan approved before any code is written"]},
            {"title": "Working build", "share": 0.55,
             "criteria": ["Runs in a staging environment", "Automated tests pass"]},
            {"title": "Hand-off and docs", "share": 0.25,
             "criteria": ["Setup steps documented in the repository"]},
        ],
    },
    {
        "key": "data", "label": "Data", "agent_category": "Data & Analytics",
        "hint": "Dashboards, cleaning, forecasting, reporting",
        "keywords": ["data", "dashboard", "sql", "spreadsheet", "report", "metrics", "kpi",
                     "forecast", "analytics", "clean", "etl", "csv"],
        "suggested_budget": 900, "days_per_milestone": 4,
        "milestones": [
            {"title": "Data access and cleaning", "share": 0.30,
             "criteria": ["Row counts reconciled with the source"]},
            {"title": "Analysis", "share": 0.40,
             "criteria": ["Method written up in plain English"]},
            {"title": "Dashboard and summary", "share": 0.30,
             "criteria": ["Dashboard refreshes without manual steps"]},
        ],
    },
    {
        "key": "ops", "label": "Ops", "agent_category": "Automation",
        "hint": "Workflow automation, back-office, support triage",
        "keywords": ["automate", "automation", "workflow", "process", "ops", "operations",
                     "invoice", "support", "ticket", "crm", "zapier", "onboarding"],
        "suggested_budget": 700, "days_per_milestone": 3,
        "milestones": [
            {"title": "Process map", "share": 0.25,
             "criteria": ["Current steps and owners written down"]},
            {"title": "Automation live", "share": 0.50,
             "criteria": ["Runs end to end on real inputs"]},
            {"title": "Runbook", "share": 0.25,
             "criteria": ["Someone new can fix a failure from the runbook"]},
        ],
    },
    {
        "key": "content", "label": "Content", "agent_category": "Content",
        "hint": "Articles, docs, landing copy, social posts",
        "keywords": ["write", "writing", "blog", "article", "copy", "content", "post",
                     "social", "docs", "documentation", "script", "newsletter"],
        "suggested_budget": 450, "days_per_milestone": 3,
        "milestones": [
            {"title": "Outline and angle", "share": 0.20,
             "criteria": ["Outline approved"]},
            {"title": "First drafts", "share": 0.45,
             "criteria": ["Matches the agreed tone and length"]},
            {"title": "Final edits", "share": 0.35,
             "criteria": ["Two rounds of revisions included"]},
        ],
    },
]

DEFAULT_DAYS_PER_MILESTONE = 4


def category_for(value: str | None) -> dict | None:
    """Find a category by key, label or listing category (case-insensitive)."""
    if not value:
        return None
    v = value.strip().lower()
    for cat in CATEGORIES:
        if v in (cat["key"], cat["label"].lower(), cat["agent_category"].lower()):
            return cat
    return None


def confidence(outcome: str, criteria_counts: list[int]) -> tuple[str, list[str]]:
    """Confidence level plus what would raise it."""
    tips = []
    score = 0
    if len((outcome or "").strip()) >= 80:
        score += 1
    else:
        tips.append("Describe the outcome in a bit more detail.")
    if criteria_counts and all(c >= 1 for c in criteria_counts):
        score += 1
    else:
        tips.append("Give every milestone at least one success criterion.")
    if criteria_counts and all(c >= 2 for c in criteria_counts):
        score += 1
    else:
        tips.append("Add a second success criterion to each milestone.")
    level = "good" if score == 3 else "medium" if score == 2 else "low"
    return level, tips


def estimate(*, outcome: str, milestones: list[dict], category: str | None,
             deadline: date | None = None, today: date | None = None) -> dict:
    """Cost and duration ranges.

    ``milestones``: ``[{"amount_cents": int, "criteria": int}]``.
    """
    cat = category_for(category)
    per = cat["days_per_milestone"] if cat else DEFAULT_DAYS_PER_MILESTONE
    total = sum(int(m.get("amount_cents") or 0) for m in milestones)
    level, tips = confidence(outcome, [int(m.get("criteria") or 0) for m in milestones])
    buffer = BUFFER[level]
    days_low = max(1, per * len(milestones))
    days_high = math.ceil(round(days_low * (1 + 2 * buffer), 6))
    result = {
        "total_cents": total,
        "cost_low_cents": total,
        "cost_high_cents": math.ceil(math.floor(total * (1 + buffer) + 0.5) / 500) * 500,
        "days_low": days_low,
        "days_high": days_high,
        "confidence": level,
        "tips": tips,
        "deadline_fit": None,
    }
    if deadline:
        left = (deadline - (today or date.today())).days
        result["deadline_fit"] = ("ok" if left >= days_high else
                                  "tight" if left >= days_low else "short")
    return result
