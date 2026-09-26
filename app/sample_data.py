"""
sample_data.py - example listings for local development.

Loaded only on demand with `flask --app app seed`. Nothing here is seeded
automatically, and none of it represents a real vendor or real activity.
Each sample carries an illustrative track record (rating, review and job
counts) so catalog cards show what a rated listing looks like; the
"Sample:" name prefix marks the whole listing as example data.

A re-run adds missing samples and re-applies the track record to sample
rows that already exist, so an older local database picks up new values.
A listing that shares a sample name but has another seller is left alone.
"""
from __future__ import annotations

SAMPLE_SELLER = "0x000000000000000000000000000000000000dEaD"

SAMPLE_AGENTS = [
    {
        "name": "Sample: Repo Refactor Specialist",
        "description": "Long-running refactors of Python and TypeScript codebases with a test-first workflow.",
        "category": "Development", "use_case": "Code Review", "billing": "per_minute",
        "min_price": 0.05, "max_price": 0.20, "current_price": 0.10,
        "model_provider": "Anthropic", "model_name": "claude-sonnet",
        "tags": ["refactoring", "python", "typescript", "tests"],
        "capabilities": ["Characterization tests before changes", "Incremental PRs", "Migration plans"],
        "rating": 4.8, "reviews": 214, "tasks_completed": 336,
        "featured": True,
    },
    {
        "name": "Sample: Security Review Agent",
        "description": "Threat-models a service and reviews auth, secrets handling and dependency risk.",
        "category": "Security", "use_case": "Code Review", "billing": "per_minute",
        "min_price": 0.08, "max_price": 0.30, "current_price": 0.15,
        "model_provider": "OpenAI", "model_name": "gpt-4.1",
        "tags": ["security", "threat-model", "audit"],
        "capabilities": ["Threat model", "Dependency audit", "Findings report with fixes"],
        "rating": 4.6, "reviews": 87, "tasks_completed": 129,
        "featured": True,
    },
    {
        "name": "Sample: Data Pipeline Builder",
        "description": "Builds and documents batch ETL pipelines from a source spec to a warehouse.",
        "category": "Data & Analytics", "use_case": "Summarization", "billing": "per_token",
        "min_price": 0.000002, "max_price": 0.00001, "current_price": 0.000004,
        "model_provider": "Google", "model_name": "gemini-2.5-pro",
        "input_price_per_1m": 1_250_000, "output_price_per_1m": 5_000_000,
        "tags": ["etl", "sql", "dbt"],
        "capabilities": ["Source profiling", "dbt models", "Data quality checks"],
        "rating": 4.2, "reviews": 58, "tasks_completed": 97,
        "featured": True,
    },
    {
        "name": "Sample: Research Brief Writer",
        "description": "Produces sourced research briefs with an explicit confidence rating per claim.",
        "category": "Research", "use_case": "Summarization", "billing": "per_token",
        "min_price": 0.000001, "max_price": 0.000008, "current_price": 0.000003,
        "model_provider": "Anthropic", "model_name": "claude-haiku",
        "input_price_per_1m": 800_000, "output_price_per_1m": 4_000_000,
        "tags": ["research", "citations"],
        "capabilities": ["Source gathering", "Claim-level citations", "Executive summary"],
        "rating": 4.9, "reviews": 142, "tasks_completed": 203,
    },
    {
        "name": "Sample: Docs Maintainer",
        "description": "Keeps READMEs, API references and runbooks in sync with the code.",
        "category": "Content", "use_case": "Summarization", "billing": "per_minute",
        "min_price": 0.03, "max_price": 0.10, "current_price": 0.05,
        "model_provider": "Mistral", "model_name": "mistral-large",
        "tags": ["docs", "api", "runbooks"],
        "capabilities": ["API reference generation", "Runbook updates", "Changelog drafting"],
        "rating": 4.4, "reviews": 31, "tasks_completed": 64,
    },
    {
        "name": "Sample: CI Automation Agent",
        "description": "Sets up and hardens CI pipelines: caching, test sharding, release automation.",
        "category": "Automation", "use_case": "Testing", "billing": "per_minute",
        "min_price": 0.04, "max_price": 0.12, "current_price": 0.06,
        "model_provider": "OpenAI", "model_name": "gpt-4.1-mini",
        "tags": ["ci", "github-actions", "release"],
        "capabilities": ["Pipeline setup", "Flaky test triage", "Release automation"],
        "rating": 3.9, "reviews": 46, "tasks_completed": 88,
    },
]


TRACK_RECORD_FIELDS = ("rating", "reviews", "tasks_completed")


def seed_sample_agents(db, Agent) -> dict:
    """Insert the sample agents that are not present yet and refresh the
    track record of the ones that are. Returns the counts added and updated."""
    rows = Agent.query.filter(Agent.name.in_([s["name"] for s in SAMPLE_AGENTS])).all()
    taken = {a.name for a in rows}
    samples = {a.name: a for a in rows if (a.seller or "").lower() == SAMPLE_SELLER.lower()}
    added = updated = 0
    for spec in SAMPLE_AGENTS:
        row = samples.get(spec["name"])
        if row is not None:
            for field in TRACK_RECORD_FIELDS:
                setattr(row, field, spec[field])
            updated += 1
            continue
        if spec["name"] in taken:
            continue  # same name, someone else's listing: leave it alone
        spec = dict(spec)
        tags = spec.pop("tags", [])
        caps = spec.pop("capabilities", [])
        row = Agent(
            long_description=spec["description"],
            seller=SAMPLE_SELLER, deployer_wallet=SAMPLE_SELLER.lower(),
            verified=False, verification_tier="none", seller_rating=0.0,
            **spec,
        )
        row.tags = tags
        row.capabilities = caps
        db.session.add(row)
        added += 1
    db.session.commit()
    return {"added": added, "updated": updated}
