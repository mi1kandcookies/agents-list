"""
sample_data.py - example listings for local development.

Loaded only on demand with `flask --app app seed`. Nothing here is seeded
automatically, and none of it represents a real vendor or real activity:
ratings, reviews and task counts start at zero.
"""
from __future__ import annotations

from app.common.agent_ids import generate_agent_id

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
    },
    {
        "name": "Sample: Docs Maintainer",
        "description": "Keeps READMEs, API references and runbooks in sync with the code.",
        "category": "Content", "use_case": "Summarization", "billing": "per_minute",
        "min_price": 0.03, "max_price": 0.10, "current_price": 0.05,
        "model_provider": "Mistral", "model_name": "mistral-large",
        "tags": ["docs", "api", "runbooks"],
        "capabilities": ["API reference generation", "Runbook updates", "Changelog drafting"],
    },
    {
        "name": "Sample: CI Automation Agent",
        "description": "Sets up and hardens CI pipelines: caching, test sharding, release automation.",
        "category": "Automation", "use_case": "Testing", "billing": "per_minute",
        "min_price": 0.04, "max_price": 0.12, "current_price": 0.06,
        "model_provider": "OpenAI", "model_name": "gpt-4.1-mini",
        "tags": ["ci", "github-actions", "release"],
        "capabilities": ["Pipeline setup", "Flaky test triage", "Release automation"],
    },
]


def seed_sample_agents(db, Agent) -> int:
    """Insert the sample agents that are not present yet. Returns the count added."""
    existing = {a.name for a in Agent.query.all()}
    added = 0
    for spec in SAMPLE_AGENTS:
        if spec["name"] in existing:
            continue
        spec = dict(spec)
        tags = spec.pop("tags", [])
        caps = spec.pop("capabilities", [])
        row = Agent(
            public_id=generate_agent_id(),
            long_description=spec["description"],
            seller=SAMPLE_SELLER, deployer_wallet=SAMPLE_SELLER.lower(),
            verified=False, verification_tier="none",
            rating=0.0, reviews=0, tasks_completed=0, seller_rating=0.0,
            **spec,
        )
        row.tags = tags
        row.capabilities = caps
        db.session.add(row)
        added += 1
    db.session.commit()
    return added
