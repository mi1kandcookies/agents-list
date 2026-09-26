"""
demo_seed.py - `flask --app wsgi seed-demo`: specialist agents for a demo.

Loads ten realistic listings for a business and engineering audience. Each
has a category, a one-line specialty, a price range, what it does and does
not do, the tools and models it uses, short example deliverables and an FAQ.

Where each field goes (no schema change):
    listing columns    name, specialty (description), about, category, price
                       range, model, tags, capabilities (= does)
    manifest_json      the canonical operator manifest (app/seller/stamp.py):
                       model, tools, skills (= does), price range, payout
    this module        doesn't-list, example outputs and FAQ, via
                       ``demo_profile(name)``; the listing has no column for
                       them yet

Stamping: hiring requires an operator stamp of the manifest. The seed does
not stamp by itself; an operator stamps each agent with World ID from its
manifest page. For local development ``--dev-stamp`` writes simulated stamps
under the same rules as ``flask seed-stamps`` (never in production, never
over a real operator stamp).

Honesty rules:
    * Nothing here is a real vendor. The operator is "Demo operator".
    * Ratings, review counts and completed-job counts stay at zero, and no
      listing is marked verified. Track record has to be earned.
    * Sample outputs are labelled as example work.

Addresses (docs/decisions/0001-custody-chain.md §4):
    Payouts settle on Sepolia, but screening data is mainnet-only. If
    SCREENING_ADDRESS_MAP is set (inline JSON or a path, same format the
    screener reads), its entries are assigned to the demo agents in order,
    wrapping around when there are fewer entries than agents: the Sepolia key
    becomes the payout address and the mainnet value the screening address.
    Without the map every agent gets a deterministic placeholder payout
    address that nobody holds a key for, and no screening address, so every
    payment to it is REFUSED (UNMAPPED_ADDRESS) - screening fails closed.

Idempotent: agents are matched by name. A re-run refreshes the listing text
and addresses of demo agents and never duplicates rows or touches others.
"""
from __future__ import annotations

import hashlib

import click
from flask.cli import with_appcontext

DEMO_OPERATOR = "Demo operator"
PROFILE_VERSION = 1

DEMO_AGENTS: list[dict] = [
    {
        "slug": "solidity-auditor",
        "name": "Keelhaul Audit",
        "category": "Security", "use_case": "Code Review",
        "specialty": "Solidity security reviews for ERC-20, vault and escrow contracts.",
        "about": ("Reviews Solidity codebases for access-control gaps, reentrancy, unsafe "
                  "external calls, rounding and upgradeability issues. Reproduces every "
                  "finding with a Foundry test and ranks it by impact and likelihood."),
        "price": (0.18, 0.40, 0.25),
        "model": ("Anthropic", "claude-opus"),
        "tags": ["solidity", "audit", "foundry", "defi", "smart-contracts"],
        "does": ["Line-by-line review of contracts in scope",
                 "Foundry proof-of-concept test for each finding",
                 "Severity-ranked report with suggested fixes",
                 "Re-review of the fix commits"],
        "doesnt": ["Formal verification",
                   "Economic or token-design review",
                   "Deploying or upgrading contracts",
                   "Guarantee that code is free of bugs"],
        "tools": ["Foundry (forge, cast)", "Slither", "Echidna", "Git"],
        "samples": [
            {"title": "Example finding",
             "body": ("H-01 withdraw() sends USDC before zeroing the balance. A receiver "
                      "hook can re-enter and drain the vault. Fix: update state first "
                      "(checks-effects-interactions) and add nonReentrant. PoC: "
                      "test/Reenter.t.sol::test_drain.")},
        ],
        "faq": [
            {"q": "What do you need from us?",
             "a": "A commit hash, the list of contracts in scope and any design notes."},
            {"q": "Do you publish the report?",
             "a": "No. The report belongs to you; publishing is your decision."},
        ],
    },
    {
        "slug": "growth-analyst",
        "name": "Northstar Growth Analyst",
        "category": "Marketing", "use_case": "Summarization",
        "specialty": "Funnel and cohort analysis that ends in a ranked list of growth experiments.",
        "about": ("Connects to your product analytics export, rebuilds the acquisition and "
                  "activation funnel, finds the biggest drop-offs by segment and proposes "
                  "experiments with an expected impact and a success metric."),
        "price": (0.06, 0.15, 0.09),
        "model": ("OpenAI", "gpt-4.1"),
        "tags": ["growth", "funnel", "cohorts", "experiments", "analytics"],
        "does": ["Funnel and cohort breakdowns from CSV or warehouse exports",
                 "Drop-off analysis by channel, plan and device",
                 "Experiment backlog with ICE scores",
                 "Readout of finished experiments"],
        "doesnt": ["Running paid ad campaigns",
                   "Changing your product or site",
                   "Buying data or contact lists"],
        "tools": ["DuckDB", "pandas", "Metabase-compatible SQL", "Google Sheets export"],
        "samples": [
            {"title": "Example insight",
             "body": ("Trial users who invite a teammate in week 1 convert at 2.4x the "
                      "rate of those who don't. Proposed test: invite prompt after the "
                      "first project is created. Metric: week-1 invite rate.")},
        ],
        "faq": [
            {"q": "Which analytics tools do you support?",
             "a": "Any tool that exports events or tables as CSV, or a read-only SQL connection."},
            {"q": "Do you need production access?",
             "a": "No. Read-only exports are enough and are preferred."},
        ],
    },
    {
        "slug": "data-pipeline-engineer",
        "name": "Conduit Data Pipelines",
        "category": "Data & Analytics", "use_case": "Summarization",
        "specialty": "Builds tested batch pipelines from SaaS APIs and databases into your warehouse.",
        "about": ("Profiles the sources, writes the ingestion jobs and dbt models, adds "
                  "freshness and quality checks, and documents lineage so your team can "
                  "run and change the pipeline without the agent."),
        "price": (0.08, 0.20, 0.12),
        "model": ("Google", "gemini-2.5-pro"),
        "tags": ["etl", "dbt", "sql", "warehouse", "data-quality"],
        "does": ["Source profiling and a written data contract",
                 "Incremental ingestion jobs",
                 "dbt models with tests and docs",
                 "Freshness and row-count alerts"],
        "doesnt": ["Streaming or sub-minute pipelines",
                   "Managing your cloud account or billing",
                   "Moving personal data without a signed data agreement"],
        "tools": ["dbt", "Airflow or Dagster", "BigQuery / Snowflake / Postgres", "Great Expectations"],
        "samples": [
            {"title": "Example model test",
             "body": ("models/orders.yml: unique + not_null on order_id; "
                      "accepted_values on status; relationship orders.customer_id -> "
                      "customers.id. Freshness: warn after 6h, error after 24h.")},
        ],
        "faq": [
            {"q": "Who owns the code?",
             "a": "You do. Everything is delivered as pull requests to your repository."},
            {"q": "Can it work with our existing dbt project?",
             "a": "Yes. It follows your naming and folder conventions."},
        ],
    },
    {
        "slug": "market-researcher",
        "name": "Fieldnote Market Research",
        "category": "Research", "use_case": "Summarization",
        "specialty": "Sourced market maps and competitor teardowns with a confidence rating per claim.",
        "about": ("Sizes a market bottom-up, maps competitors by segment and pricing, and "
                  "summarizes public customer feedback. Every claim links to its source "
                  "and carries a high, medium or low confidence rating."),
        "price": (0.04, 0.12, 0.07),
        "model": ("Anthropic", "claude-sonnet"),
        "tags": ["research", "market-sizing", "competitors", "citations"],
        "does": ["Bottom-up market sizing with stated assumptions",
                 "Competitor matrix: segment, pricing, positioning",
                 "Summary of public reviews and forum threads",
                 "One-page executive summary"],
        "doesnt": ["Primary interviews or surveys",
                   "Paywalled or non-public sources",
                   "Investment advice"],
        "tools": ["Web search", "Public filings", "Review sites", "Spreadsheet model"],
        "samples": [
            {"title": "Example claim",
             "body": ("Mid-market teams (50-500 staff) make up an estimated 38% of seats "
                      "in the category [medium confidence; derived from 3 vendors' public "
                      "case-study counts, see sources 4-6].")},
        ],
        "faq": [
            {"q": "How current is the data?",
             "a": "Sources are dated in the report; nothing older than 24 months without a flag."},
            {"q": "Can you cover a specific region?",
             "a": "Yes, as long as enough public sources exist in a language the agent reads."},
        ],
    },
    {
        "slug": "landing-page-copywriter",
        "name": "Headline Landing Copy",
        "category": "Content", "use_case": "Summarization",
        "specialty": "Conversion-focused landing page copy with variants ready for A/B testing.",
        "about": ("Turns your positioning notes and customer quotes into a full landing "
                  "page: headline, subhead, benefit blocks, objections, FAQ and calls to "
                  "action, plus two alternative headlines for testing."),
        "price": (0.03, 0.08, 0.05),
        "model": ("Anthropic", "claude-sonnet"),
        "tags": ["copywriting", "landing-page", "conversion", "growth"],
        "does": ["Page structure and full copy",
                 "Two headline variants for testing",
                 "Tone matched to your existing brand",
                 "One revision round per milestone"],
        "doesnt": ["Visual design or building the page",
                   "Claims that your team cannot back up",
                   "Keyword stuffing"],
        "tools": ["Style guide intake", "Readability checks", "Markdown and HTML output"],
        "samples": [
            {"title": "Example headline set",
             "body": ("A: Close the books in two days, not ten. "
                      "B: Month-end without the spreadsheet chase. "
                      "Subhead: Reconciliations, approvals and audit trail in one place.")},
        ],
        "faq": [
            {"q": "What do you need to start?",
             "a": "Who the page is for, what they get, proof points and your current page if any."},
            {"q": "Do you write in other languages?",
             "a": "English by default; ask for an estimate for other languages."},
        ],
    },
    {
        "slug": "soc2-readiness",
        "name": "Attest SOC 2 Readiness",
        "category": "Security", "use_case": "Summarization",
        "specialty": "Gap assessment and policy drafts to get a startup ready for a SOC 2 audit.",
        "about": ("Maps your current practices to the SOC 2 trust services criteria, lists "
                  "the gaps by effort, drafts the policies you are missing and prepares an "
                  "evidence checklist for your auditor."),
        "price": (0.07, 0.18, 0.10),
        "model": ("OpenAI", "gpt-4.1"),
        "tags": ["soc2", "compliance", "policies", "security"],
        "does": ["Gap assessment against the trust services criteria",
                 "Policy drafts (access, change management, incident response)",
                 "Evidence checklist per control",
                 "Plain-language summary for leadership"],
        "doesnt": ["Performing the audit or issuing a report",
                   "Legal advice",
                   "Configuring your infrastructure"],
        "tools": ["Control matrix template", "Policy library", "Cloud config exports (read-only)"],
        "samples": [
            {"title": "Example gap entry",
             "body": ("CC6.2 access reviews: no quarterly review of production access. "
                      "Effort: low. Action: calendar a quarterly export from the identity "
                      "provider and record sign-off in the ticket tracker.")},
        ],
        "faq": [
            {"q": "Is this a SOC 2 report?",
             "a": "No. It prepares you for the audit; only a licensed CPA firm issues the report."},
            {"q": "Type I or Type II?",
             "a": "Both start the same way; the plan notes what Type II adds."},
        ],
    },
    {
        "slug": "investor-update-writer",
        "name": "Quarterly Investor Updates",
        "category": "Finance", "use_case": "Summarization",
        "specialty": "Clear monthly or quarterly investor updates drafted from your metrics and notes.",
        "about": ("Takes your KPI export, bank balance and a few bullet points from the "
                  "founders, and drafts an investor update with highlights, lowlights, "
                  "metrics, runway and specific asks."),
        "price": (0.03, 0.09, 0.05),
        "model": ("Anthropic", "claude-haiku"),
        "tags": ["investor-update", "finance", "startups", "reporting"],
        "does": ["Draft update in your usual format",
                 "Metric table with period-over-period change",
                 "Runway calculation from the numbers you provide",
                 "A short list of concrete asks"],
        "doesnt": ["Financial audits or tax filings",
                   "Fundraising advice or introductions",
                   "Sending the email for you"],
        "tools": ["Spreadsheet import", "Markdown and email-ready HTML output"],
        "samples": [
            {"title": "Example opening",
             "body": ("Highlights: MRR up 11% to $84k; two enterprise pilots signed. "
                      "Lowlight: onboarding time slipped to 9 days. Runway: 19 months at "
                      "current burn. Ask: intros to heads of finance at 200+ person companies.")},
        ],
        "faq": [
            {"q": "Are our numbers kept private?",
             "a": "They are used only for this job and are not shared with other clients."},
            {"q": "Can it match our previous updates?",
             "a": "Yes. Share two past updates and it follows their structure and tone."},
        ],
    },
    {
        "slug": "api-integration-engineer",
        "name": "Bridgework API Integrations",
        "category": "Development", "use_case": "Code Review",
        "specialty": "Builds and tests integrations between your app and third-party REST APIs.",
        "about": ("Reads the provider's API reference, writes a typed client with retries, "
                  "pagination and webhook verification, and ships it with contract tests "
                  "against recorded fixtures so CI never calls the live API."),
        "price": (0.10, 0.24, 0.14),
        "model": ("Anthropic", "claude-sonnet"),
        "tags": ["api", "integration", "webhooks", "python", "typescript"],
        "does": ["Typed API client with retries and rate-limit handling",
                 "Webhook endpoint with signature verification",
                 "Contract tests using recorded fixtures",
                 "Runbook for keys, limits and failure modes"],
        "doesnt": ["Scraping sites without an API",
                   "Handling production secrets directly (you add them)",
                   "Ongoing on-call support"],
        "tools": ["Python or TypeScript", "OpenAPI tooling", "pytest / vitest", "GitHub Actions"],
        "samples": [
            {"title": "Example test",
             "body": ("test_invoice_webhook_rejects_bad_signature: replays a recorded "
                      "invoice.paid event with one byte changed and asserts HTTP 400 and no "
                      "database write.")},
        ],
        "faq": [
            {"q": "Which languages?",
             "a": "Python and TypeScript; others on request after an estimate."},
            {"q": "Will tests hit the real API?",
             "a": "No. CI uses recorded fixtures; a separate manual smoke script uses your sandbox key."},
        ],
    },
    {
        "slug": "ops-runbook-automation",
        "name": "Runbook Ops Automation",
        "category": "Automation", "use_case": "Testing",
        "specialty": "Turns repetitive ops checklists into scripted, reviewable automations.",
        "about": ("Interviews your runbooks, finds the steps that are safe to automate, and "
                  "writes idempotent scripts and scheduled jobs with dry-run modes and "
                  "clear logs. Manual approval gates stay where your team wants them."),
        "price": (0.05, 0.14, 0.08),
        "model": ("Mistral", "mistral-large"),
        "tags": ["automation", "runbooks", "ops", "scripts", "ci"],
        "does": ["Automation plan ranked by time saved",
                 "Idempotent scripts with dry-run flags",
                 "Scheduled jobs in your CI or scheduler",
                 "Updated runbook with the new steps"],
        "doesnt": ["Changes to production without your review",
                   "Replacing your paging or incident tools",
                   "24/7 operations"],
        "tools": ["Bash / Python", "GitHub Actions", "Terraform (plan only)", "Slack webhooks"],
        "samples": [
            {"title": "Example automation",
             "body": ("rotate-staging-certs --dry-run lists 4 certificates expiring within "
                      "21 days and the commands it would run; without --dry-run it renews "
                      "them and posts a summary to #ops.")},
        ],
        "faq": [
            {"q": "Will it run anything in production?",
             "a": "Only after your team reviews and merges the change."},
            {"q": "What if a step can't be automated safely?",
             "a": "It stays manual and the runbook says why."},
        ],
    },
    {
        "slug": "qa-test-planner",
        "name": "Quartz QA Test Planner",
        "category": "Development", "use_case": "Testing",
        "specialty": "Turns an API specification into a structured, reviewable test plan.",
        "about": ("Reads the supplied API contract, enumerates its operations and produces "
                  "happy-path, validation and authorization cases with a hash of the exact "
                  "source text. It plans tests only and never executes arbitrary code."),
        "price": (0.05, 0.05, 0.05),
        "model": ("OpenAI", "gpt-4.1-mini"),
        "tags": ["qa", "api", "testing", "test-plan", "contracts"],
        "does": ["Endpoint inventory from an API specification",
                 "Success, validation and authorization test cases",
                 "Traceable source hash for the delivered plan",
                 "Assumptions and missing examples called out"],
        "doesnt": ["Executing requests against your systems",
                   "Running arbitrary code or browser automation",
                   "Guaranteeing coverage beyond the supplied specification"],
        "tools": ["OpenAPI and Markdown parsing", "Structured JSON output", "pytest plan conventions"],
        "samples": [
            {"title": "Example test plan",
             "body": ("GET /v1/orders receives success, invalid-input and authorization cases; "
                      "POST /v1/orders adds a no-side-effect denial assertion. The plan records "
                      "the SHA-256 of the supplied specification.")},
        ],
        "faq": [
            {"q": "What should I provide?",
             "a": "An API specification in Markdown, plain text or an OpenAPI-derived summary."},
            {"q": "Does the specialist call my API?",
             "a": "No. It returns a reviewable plan; your team decides what to run."},
        ],
    },
]


# ── addresses ────────────────────────────────────────────────────────────────

def placeholder_payout_address(slug: str) -> str:
    """Deterministic Sepolia payout placeholder. Derived from a hash, so no
    one holds its key; it is never in SCREENING_ADDRESS_MAP by accident."""
    digest = hashlib.sha256(f"agents-list demo payout:{slug}".encode()).hexdigest()
    return "0x" + digest[:40]


def address_pairs(raw: str | None = None) -> list[tuple[str, str]]:
    """(sepolia, mainnet) pairs from SCREENING_ADDRESS_MAP, in file order.
    Uses the screener's own parser so the formats can't drift."""
    from app.screening.service import load_address_map
    return list(load_address_map(raw).items())


def assign_addresses(pairs: list[tuple[str, str]]) -> dict[str, tuple[str, str | None]]:
    """slug -> (payout_address, screening_address or None)."""
    out = {}
    for i, spec in enumerate(DEMO_AGENTS):
        if pairs:
            out[spec["slug"]] = pairs[i % len(pairs)]
        else:
            out[spec["slug"]] = (placeholder_payout_address(spec["slug"]), None)
    return out


# ── seeding ──────────────────────────────────────────────────────────────────

def demo_profile(name: str) -> dict | None:
    """The listing profile of a demo agent (specialty, does / doesn't, tools
    and models, example outputs, FAQ), or None for other names."""
    spec = next((s for s in DEMO_AGENTS if s["name"] == name), None)
    if spec is None:
        return None
    return {
        "v": PROFILE_VERSION,
        "specialty": spec["specialty"],
        "does": list(spec["does"]),
        "doesnt": list(spec["doesnt"]),
        "tools": list(spec["tools"]),
        "models": [f"{spec['model'][0]} {spec['model'][1]}"],
        "sample_outputs": [{**s, "label": "Example work"} for s in spec["samples"]],
        "faq": [dict(item) for item in spec["faq"]],
    }


def _manifest(row, spec: dict, payout: str) -> dict:
    """The canonical operator manifest (app/seller/stamp.py) for this listing."""
    from app.seller.stamp import build_manifest
    low, high, _ = spec["price"]
    return build_manifest(row, model=" ".join(spec["model"]), tools=spec["tools"],
                          skills=spec["does"], price_min_usdc=str(low),
                          price_max_usdc=str(high), payout_address=payout)


def seed_demo_agents(db, Agent, *, address_map: str | None = None,
                     dev_stamp: bool = False) -> dict:
    """Insert or refresh the demo agents. With ``dev_stamp`` also write
    simulated operator stamps (the caller must refuse outside development).
    Returns counts and the mapping mode."""
    from app.seller import stamp
    pairs = address_pairs(address_map)
    addresses = assign_addresses(pairs)
    existing = {a.name: a for a in Agent.query.filter(
        Agent.name.in_([s["name"] for s in DEMO_AGENTS])).all()}
    added = updated = skipped = stamped = 0
    for spec in DEMO_AGENTS:
        row = existing.get(spec["name"])
        if row is not None and row.seller != DEMO_OPERATOR:
            skipped += 1  # same name, someone else's listing: leave it alone
            continue
        if row is None:
            row = Agent(name=spec["name"], category=spec["category"], billing="per_minute")
            db.session.add(row)
            added += 1
        else:
            updated += 1
        low, high, current = spec["price"]
        payout, screening = addresses[spec["slug"]]
        row.description = spec["specialty"]
        row.long_description = spec["about"]
        row.category = spec["category"]
        row.use_case = spec["use_case"]
        row.billing = "per_minute"
        row.min_price, row.max_price, row.current_price = low, high, current
        row.model_provider, row.model_name = spec["model"]
        row.seller = DEMO_OPERATOR
        row.deployer_wallet = payout
        row.payout_address = payout
        row.screening_address = screening
        row.verified, row.verification_tier, row.featured = False, "none", False
        row.rating, row.reviews, row.tasks_completed, row.seller_rating = 0.0, 0, 0, 0.0
        row.avg_completion_time = " - "
        row.tags = spec["tags"]
        row.capabilities = spec["does"]
        db.session.flush()  # assigns public_id, which the manifest carries
        # Same content -> same hash, so a valid stamp survives a re-run; a
        # changed payout or price makes the listing need a re-stamp.
        stamp.save_manifest(row, _manifest(row, spec, payout))
        real_stamp = row.manifest_stamp_sub and row.manifest_stamp_sub != stamp.DEV_STAMP_SUB
        if dev_stamp and not real_stamp:
            stamp.dev_stamp(row)
            stamped += 1
    db.session.commit()
    return {"added": added, "updated": updated, "skipped": skipped, "dev_stamped": stamped,
            "mapped": bool(pairs), "map_entries": len(pairs)}


@click.command("seed-demo")
@click.option("--dev-stamp", is_flag=True,
              help="DEVELOPMENT ONLY: also write simulated operator stamps so the demo "
                   "agents can be hired (same rules as `flask seed-stamps`).")
@with_appcontext
def seed_demo(dev_stamp: bool):
    """Load or refresh the demo specialist agents (idempotent)."""
    from flask import current_app

    from app.extensions import db
    from app.models import Agent
    from app.seller.stamp import seed_stamps_refusal
    if dev_stamp:
        refusal = seed_stamps_refusal(current_app)
        if refusal:
            raise click.ClickException(f"Refusing --dev-stamp: {refusal}")
    try:
        result = seed_demo_agents(db, Agent, dev_stamp=dev_stamp)
    except (ValueError, OSError) as exc:
        raise click.ClickException(f"SCREENING_ADDRESS_MAP is unreadable: {exc}") from None
    published = 0
    if current_app.config.get("ENS_SIDECAR_URL") and current_app.config.get("ENS_SIDECAR_TOKEN"):
        from app.names.service import on_agent_published
        for spec in DEMO_AGENTS:
            row = Agent.query.filter_by(name=spec["name"], seller=DEMO_OPERATOR).first()
            if row is not None and on_agent_published(row) is not None and row.ens_name:
                published += 1
    click.echo(f"Demo agents: {result['added']} added, {result['updated']} refreshed"
               + (f", {result['skipped']} skipped (name taken)" if result["skipped"] else "") + ".")
    if dev_stamp:
        click.echo(f"SIMULATED dev stamps written for {result['dev_stamped']} demo agents.")
    else:
        click.echo("Not stamped: an operator must stamp each manifest with World ID before "
                   "the agent can be hired (or use --dev-stamp in development).")
    if result["mapped"]:
        click.echo(f"Payout/screening addresses from SCREENING_ADDRESS_MAP "
                   f"({result['map_entries']} entr{'y' if result['map_entries'] == 1 else 'ies'}).")
    else:
        click.echo("SCREENING_ADDRESS_MAP not set: payout addresses are placeholders and "
                   "unmapped, so screening refuses every payment (fail closed).")
    if published:
        click.echo(f"ENS agent names published: {published}.")
    for spec in DEMO_AGENTS:
        row = Agent.query.filter_by(name=spec["name"], seller=DEMO_OPERATOR).first()
        if row is not None:
            click.echo(f"  {row.public_id}  {row.name}")
