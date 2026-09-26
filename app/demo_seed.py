"""
demo_seed.py - `flask --app wsgi seed-demo`: specialist agents for a demo.

Loads ten realistic listings for a business and engineering audience. Each
has a category, a one-line specialty, token prices, what it does and does
not do, the tools and models it uses, short example deliverables and an FAQ.

Token prices: each listing charges its model's public list price per 1M
input and output tokens (``MODEL_LIST_PRICES``) times a fixed operator margin
(``margin`` in the spec), rounded to the cent. Stored as USDC micro-units per
1M tokens in ``input_price_per_1m`` / ``output_price_per_1m``. The older
per-minute ``price`` range is kept for the stamped manifest and the paid-task
API, which still quote it; the UI shows token prices only.

Where each field goes (no schema change):
    listing columns    name, specialty (description), about, category, price
                       range, token prices, model, icon, tags, capabilities
                       (= does)
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
    * Demo listings carry an illustrative track record (jobs, rating, review
      count and three written example reviews) so cards and profiles show
      what an established listing looks like. The rows are flagged
      ``demo_listing`` and no listing is marked verified.
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

Idempotent: agents are matched by name, reviews by agent and reviewer. A
re-run refreshes the listing text, addresses, track record and reviews of
demo agents and never duplicates rows or touches others.
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
        "margin": 1.4,
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
        "margin": 1.5,
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
        "margin": 1.6,
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
        "margin": 1.5,
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
        "margin": 1.25,
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
        "margin": 1.75, "icon": "clipboard-check",
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
        "margin": 1.5,
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
        "margin": 1.75, "icon": "plug",
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
        "margin": 1.5,
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
        "margin": 1.5, "icon": "flask-conical",
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


# Three short example reviews per demo listing: (reviewer, stars, date, text).
DEMO_REVIEWS: dict[str, list[tuple[str, int, str, str]]] = {
    "solidity-auditor": [
        ("Marta V., protocol lead", 5, "2026-08-14",
         "Found a rounding issue in our share math that two earlier reviews missed. Every "
         "finding came with a Foundry test we could run ourselves."),
        ("Owen T.", 5, "2026-07-02",
         "Clear severity ranking and it re-checked our fix commits the same day. We shipped "
         "the vault a week earlier than planned."),
        ("Ines K., CTO", 4, "2026-05-21",
         "Thorough on access control. A couple of the low findings were style notes, but it "
         "said so up front."),
    ],
    "growth-analyst": [
        ("Priya S., head of growth", 5, "2026-08-30",
         "Rebuilt our activation funnel from a messy Amplitude export and found that mobile "
         "signups never saw the invite step. That one fix paid for the job."),
        ("Daniel R.", 4, "2026-07-11",
         "Good experiment backlog with ICE scores we actually agreed with. I had to explain "
         "our plan tiers twice before the segments made sense."),
        ("Hannah L., founder", 4, "2026-06-03",
         "Useful cohort breakdown. Would have liked the readout a day sooner, but the "
         "numbers matched what we saw in the warehouse."),
    ],
    "data-pipeline-engineer": [
        ("Tomasz W., data lead", 4, "2026-09-05",
         "Solid dbt models and the freshness checks caught a broken Stripe sync in the first "
         "week. Naming followed our conventions without being asked."),
        ("Grace O.", 5, "2026-07-19",
         "The data contract doc alone was worth it. Our analysts finally know which columns "
         "they can trust."),
        ("Miguel A., analytics engineer", 3, "2026-06-12",
         "Pipelines work, but the first pass over-partitioned a small table and I had to ask "
         "for a rework. It fixed it quickly once flagged."),
    ],
    "market-researcher": [
        ("Chloe D., product manager", 5, "2026-08-22",
         "Every number in the market map linked to a source, and the low-confidence claims "
         "were marked as such. Made our board deck easy to defend."),
        ("Ravi P.", 4, "2026-07-08",
         "Competitor matrix was accurate on pricing. The European section was thinner, which "
         "it warned us about before starting."),
        ("Sofia M., founder", 5, "2026-05-30",
         "Bottom-up sizing with the assumptions written out, so we could swap in our own "
         "conversion rates. Exactly what I asked for."),
    ],
    "landing-page-copywriter": [
        ("Jonas B., marketing lead", 4, "2026-09-10",
         "Headline B beat our old page by a clear margin in the first two weeks of testing. "
         "Benefit blocks needed a light edit for tone."),
        ("Amara N.", 3, "2026-07-27",
         "Decent structure, but the first draft leaned on claims we could not back up. The "
         "revision round fixed it."),
        ("Lucas F., founder", 4, "2026-06-18",
         "Turned twenty pages of customer interview notes into a page my cofounder approved "
         "on the first read. Fast turnaround."),
    ],
    "soc2-readiness": [
        ("Nadia H., head of ops", 5, "2026-08-27",
         "The gap list was sorted by effort, so we closed the easy controls in a week. Our "
         "auditor said the evidence checklist saved them time too."),
        ("Ben C., CTO", 5, "2026-07-15",
         "Policy drafts were short and readable, not boilerplate. We adopted the access "
         "review and incident response policies almost as written."),
        ("Leah G.", 5, "2026-05-26",
         "Plain-language summary for our leadership team was spot on. Clear about what it "
         "does not do, which I appreciated."),
    ],
    "investor-update-writer": [
        ("Sam E., CEO", 5, "2026-09-02",
         "Took my bullet points and our KPI sheet and produced an update that read like me "
         "on a good day. Runway math matched our own."),
        ("Julia R., COO", 4, "2026-07-31",
         "Kept the format of our previous updates. The asks section was sharper than what "
         "we usually write."),
        ("Mark D.", 4, "2026-06-29",
         "Good draft in under an hour. I rewrote the lowlights paragraph because it was a "
         "bit too cheerful about churn."),
    ],
    "api-integration-engineer": [
        ("Elena Z., backend lead", 4, "2026-08-19",
         "The typed client and webhook verification were solid, and CI never touches the "
         "live API. Rate-limit handling needed one more pass."),
        ("Chris M.", 4, "2026-07-06",
         "Contract tests against recorded fixtures are a pattern we are now copying for "
         "other integrations. The runbook was brief but correct."),
        ("Aisha K., founder", 3, "2026-06-09",
         "Worked in the end, but it misread the pagination rules in the provider docs and "
         "the first milestone slipped by two days."),
    ],
    "ops-runbook-automation": [
        ("Pete L., SRE", 5, "2026-09-08",
         "Our certificate rotation used to eat a morning every month. Now it is one command "
         "with a dry run, and the Slack summary is genuinely useful."),
        ("Rosa J., ops manager", 5, "2026-07-23",
         "Left the approval gates exactly where we wanted them. Every script is idempotent "
         "and the logs read well."),
        ("Victor N.", 4, "2026-06-15",
         "Good prioritisation by time saved. Two steps stayed manual and the runbook says "
         "why, which is fair."),
    ],
    "qa-test-planner": [
        ("Kate S., QA lead", 4, "2026-08-25",
         "Turned our OpenAPI summary into a test plan with auth cases we had never written "
         "down. Easy to review line by line."),
        ("Arjun R.", 5, "2026-07-14",
         "The plan listed every endpoint and flagged three with missing examples. Our team "
         "ran it in a sprint."),
        ("Ola B., engineering manager", 3, "2026-05-28",
         "Useful, but it only covers what is in the spec, so our undocumented endpoints "
         "were left out. It does say that on the listing."),
    ],
}


def demo_rating(slug: str) -> float:
    """Illustrative rating for a demo listing, 3.7 to 4.9 in steps of 0.1.
    The listings are ordered by a hash of their slug and spread evenly across
    the range, so every reseed gives the same values and no two listings
    share a rating."""
    slugs = sorted((s["slug"] for s in DEMO_AGENTS),
                   key=lambda x: hashlib.sha256(f"agents-list demo rating:{x}".encode()).hexdigest())
    rank = slugs.index(slug)
    return round(3.7 + 1.2 * rank / max(1, len(slugs) - 1), 1)


def demo_review_count(slug: str, jobs: int) -> int:
    """Illustrative review count: 40-75% of jobs delivered, 12 to 140."""
    import random
    rng = random.Random(f"agents-list demo reviews:{slug}")
    return min(140, max(12, min(jobs, round(jobs * rng.uniform(0.40, 0.75)))))


def _seed_reviews(db, row, slug: str) -> None:
    """Insert or refresh the example reviews of one demo listing."""
    from datetime import datetime, timezone

    from app.models import Review
    existing = {r.user: r for r in Review.query.filter_by(agent_id=row.id).all()}
    for user, stars, date, text in DEMO_REVIEWS.get(slug, []):
        review = existing.get(user)
        if review is None:
            review = Review(agent_id=row.id, user=user)
            db.session.add(review)
        review.rating, review.comment, review.date = stars, text, date
        review.created_at = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc)


# ── token prices ─────────────────────────────────────────────────────────────

# Public list prices in USD per 1M tokens (input, output), by the model family
# named in each spec. Anthropic: Opus 5, Sonnet 5 and Haiku 4.5 rates; the
# other providers' published standard rates. Review these when providers
# change their price lists.
MODEL_LIST_PRICES: dict[str, tuple[float, float]] = {
    "claude-opus": (5.00, 25.00),
    "claude-sonnet": (2.00, 10.00),
    "claude-haiku": (1.00, 5.00),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gemini-2.5-pro": (1.25, 10.00),
    "mistral-large": (0.50, 1.50),
}


def token_prices(spec: dict) -> tuple[int, int]:
    """(input, output) price in USDC micro-units per 1M tokens: the model's
    list price times the listing's operator margin, rounded to the cent."""
    list_in, list_out = MODEL_LIST_PRICES[spec["model"][1]]

    def micro(usd: float) -> int:
        return round(usd * spec["margin"] * 100) * 10_000

    return micro(list_in), micro(list_out)


# ── track record ─────────────────────────────────────────────────────────────

def demo_track_record(slug: str, category: str) -> dict:
    """Deterministic sample track record for a demo listing (same slug, same
    numbers on every run)."""
    import random
    rng = random.Random(f"agents-list demo track:{slug}")
    jobs = rng.randint(26, 164)
    rng.random()  # was the turnaround draw; kept so the other numbers stay put
    return {
        "jobs": jobs,
        "on_time": round(rng.uniform(0.88, 0.99), 2),
        "repeat": round(rng.uniform(0.22, 0.61), 2),
    }


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
            row = Agent(name=spec["name"], category=spec["category"], billing="per_token")
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
        row.billing = "per_token"
        row.min_price, row.max_price, row.current_price = low, high, current
        row.input_price_per_1m, row.output_price_per_1m = token_prices(spec)
        row.icon = spec.get("icon")
        row.model_provider, row.model_name = spec["model"]
        row.seller = DEMO_OPERATOR
        row.deployer_wallet = payout
        row.payout_address = payout
        row.screening_address = screening
        row.verified, row.verification_tier, row.featured = False, "none", False
        track = demo_track_record(spec["slug"], spec["category"])
        row.tasks_completed = track["jobs"]
        row.rating = demo_rating(spec["slug"])
        row.reviews = demo_review_count(spec["slug"], track["jobs"])
        row.avg_completion_time = " - "   # turnaround depends on the job; not shown
        row.on_time_rate = track["on_time"]
        row.repeat_hire_rate = track["repeat"]
        row.demo_listing = True
        row.tags = spec["tags"]
        row.capabilities = spec["does"]
        db.session.flush()  # assigns public_id, which the manifest carries
        _seed_reviews(db, row, spec["slug"])
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
