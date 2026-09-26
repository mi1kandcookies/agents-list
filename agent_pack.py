"""
agent_pack.py - realistic, category-specific roster generator.

Produces a deterministic set of 10 to 20 agents per category. Each agent
carries:
  - a unique wallet (via OnchainProfile downstream)
  - a distinct description (4 to 6 templates per category, no repeats within
    category, no em-dashes anywhere)
  - a model assignment weighted across frontier, open-source, and
    "Undisclosed" / proprietary buckets so the roster looks like a real
    marketplace, not every-agent-is-Claude
  - input + output token pricing in USDC micro-units per 1M tokens,
    grounded in real published model prices
  - a deployer wallet scoped to the seller so multiple agents from the same
    seller share an identity, but different sellers don't collide

Safe to re-run: only inserts new names, idempotent on model/deployer fields.
"""
from __future__ import annotations
import hashlib
import random

from extensions import db
from models import Agent


# ── Model bank ───────────────────────────────────────────────────────────────
# (provider, name, input_per_1m_usdc, output_per_1m_usdc)
# Prices in USDC. Real-world public pricing as of early 2026 where available;
# self-hosted / open-source agents use operator-cost estimates.

MODELS_POOL = [
    # Frontier closed-source
    ("OpenAI",      "gpt-4-turbo",           10.00,  30.00),
    ("OpenAI",      "gpt-4o",                 5.00,  15.00),
    ("OpenAI",      "gpt-4o-mini",            0.15,   0.60),
    ("Anthropic",   "claude-opus-4-7",       15.00,  75.00),
    ("Anthropic",   "claude-sonnet-4-6",      3.00,  15.00),
    ("Anthropic",   "claude-haiku-4-5",       0.80,   4.00),
    ("Google",      "gemini-1.5-pro",         3.50,  10.50),
    ("Google",      "gemini-1.5-flash",       0.07,   0.30),
    ("Cohere",      "command-r-plus",         2.50,  10.00),
    # Open-source (self-hosted prices)
    ("Meta",        "llama-3.3-70b",          0.60,   0.80),
    ("Meta",        "codellama-70b",          0.50,   1.50),
    ("Mistral",     "mistral-large-2",        3.00,   9.00),
    ("Mistral",     "codestral-22b",          1.00,   3.00),
    ("Mistral",     "mixtral-8x22b",          2.00,   6.00),
    ("DeepSeek",    "deepseek-coder-v2",      0.14,   0.28),
    ("DeepSeek",    "deepseek-r1",            0.55,   2.19),
    ("Qwen",        "qwen2.5-72b",            0.40,   1.20),
    ("01.AI",       "yi-large",               3.00,   6.00),
    ("xAI",         "grok-2",                 5.00,  15.00),
    # Marketplace "mystery" entries — deliberately undisclosed
    ("Undisclosed", "proprietary-v3",         2.00,   8.00),
    ("Undisclosed", "fine-tuned-ensemble",    4.00,  12.00),
    ("Undisclosed", "domain-specific-llm",    1.50,   6.00),
    ("Internal",    "custom-distillation",    0.50,   2.00),
    ("Internal",    "rule-based-hybrid",      0.20,   0.40),
]


# Per-category model weighting (which buckets dominate for this work type).
# Keys are (provider, name) tuples; values are multiplicative weights.
# Any model not listed gets weight 1. Weight 0 means never use here.
def _model_weights_for(category: str) -> dict:
    """Return sampling weights so each category has a plausible model mix."""
    base = {mk[:2]: 1 for mk in [(m[0], m[1]) for m in MODELS_POOL]}
    # Kill obviously-wrong fits per category
    if category == "Development":
        base[("OpenAI", "gpt-4o-mini")] = 2
        base[("DeepSeek", "deepseek-coder-v2")] = 4
        base[("Meta", "codellama-70b")] = 4
        base[("Mistral", "codestral-22b")] = 3
        base[("Google", "gemini-1.5-flash")] = 0
        base[("xAI", "grok-2")] = 0
    elif category == "Data & Analytics":
        base[("OpenAI", "gpt-4o")] = 3
        base[("Anthropic", "claude-opus-4-7")] = 2
        base[("Google", "gemini-1.5-pro")] = 3
        base[("Meta", "codellama-70b")] = 0
    elif category == "Content":
        base[("Anthropic", "claude-sonnet-4-6")] = 3
        base[("OpenAI", "gpt-4o")] = 3
        base[("Mistral", "mistral-large-2")] = 2
        base[("Cohere", "command-r-plus")] = 2
        base[("Meta", "codellama-70b")] = 0
        base[("DeepSeek", "deepseek-coder-v2")] = 0
    elif category == "Finance":
        base[("OpenAI", "gpt-4-turbo")] = 2
        base[("Anthropic", "claude-opus-4-7")] = 2
        base[("Undisclosed", "proprietary-v3")] = 4
        base[("Undisclosed", "domain-specific-llm")] = 3
        base[("Internal", "rule-based-hybrid")] = 2
        base[("Meta", "codellama-70b")] = 0
    elif category == "Research":
        base[("Anthropic", "claude-opus-4-7")] = 3
        base[("OpenAI", "gpt-4-turbo")] = 2
        base[("Google", "gemini-1.5-pro")] = 3
        base[("DeepSeek", "deepseek-r1")] = 3
        base[("Meta", "codellama-70b")] = 0
    elif category == "Security":
        base[("Anthropic", "claude-sonnet-4-6")] = 2
        base[("OpenAI", "gpt-4-turbo")] = 2
        base[("Undisclosed", "domain-specific-llm")] = 4
        base[("Internal", "custom-distillation")] = 3
    elif category == "Automation":
        base[("OpenAI", "gpt-4o-mini")] = 3
        base[("Anthropic", "claude-haiku-4-5")] = 3
        base[("Google", "gemini-1.5-flash")] = 3
        base[("Mistral", "mistral-large-2")] = 1
    # Every category gets some undisclosed variety so no provider dominates
    base[("Undisclosed", "fine-tuned-ensemble")] = max(base.get(("Undisclosed", "fine-tuned-ensemble"), 0), 1)
    return base


def _pick_model(rng: random.Random, category: str):
    weights = _model_weights_for(category)
    pool, pws = [], []
    for m in MODELS_POOL:
        w = weights.get((m[0], m[1]), 1)
        if w <= 0:
            continue
        pool.append(m)
        pws.append(w)
    return rng.choices(pool, weights=pws, k=1)[0]


# ── Description templates (no em-dashes anywhere) ────────────────────────────

DESCRIPTION_TEMPLATES = {
    "Development": [
        "Automates {cap1} and {cap2} across polyglot codebases.",
        "Ships safer code by combining {cap1} with targeted {cap2}.",
        "Built for teams that care about {cap1} without slowing down shipping.",
        "Integrates with your CI to run {cap1} and surface {cap2} inline.",
        "A senior-engineer copilot focused on {cap1} and {cap2}.",
        "Ranks dead code, tech debt, and {cap2} so you can tackle the real issues first.",
        "Focuses on {cap1} with {cap2} as a secondary pass.",
        "Trained on FOSS diffs to catch {cap1} before merge.",
    ],
    "Data & Analytics": [
        "Writes production SQL for {cap1} and explains the plan in plain English.",
        "Your on-call analyst covering {cap1}, cohort breakdowns, and dashboards on demand.",
        "Turns raw warehouse tables into {cap1} with a clear audit trail.",
        "Cuts the time from ad-hoc question to {cap2} from hours to minutes.",
        "Built on the SQL dialect you actually use; handles {cap1} end to end.",
        "Flags schema drift, slow queries, and {cap2} before they break dashboards.",
        "Handles {cap1} on big-lake tables without blowing up the cluster.",
        "Pairs {cap1} with {cap2} for data teams that live in notebooks.",
    ],
    "Content": [
        "Drafts long-form copy that sounds like your best writer did it, guided by {cap1}.",
        "A production writer for {cap1}, backed by strict {cap2} checks.",
        "Ships headlines, hooks, and outlines calibrated to your top-performing posts.",
        "Swap out the generic LLM feel with {cap1} tuned to your audience.",
        "Writes the first draft, then iterates based on {cap2} from your last 50 pieces.",
        "Edits with a light touch using {cap2}: keeps your voice, fixes flow, tightens the ending.",
        "Built around {cap1} so content teams stop burning time on rewrites.",
        "Generates {cap1} and refines it with {cap2} before you ever hit publish.",
    ],
    "Finance": [
        "Scans on-chain flows and flags {cap1} before your treasury committee meets.",
        "Portfolio analysis powered by {cap1}, with a research memo attached, not just a score.",
        "Tuned on a decade of trade data; handles {cap1} and {cap2} in one run.",
        "Reconciles wallets, exchanges, and custodian statements using {cap1}.",
        "Back-tests the idea, stress-tests {cap2}, and writes the writeup.",
        "Spots correlation breaks via {cap1} faster than your dashboard can refresh.",
        "Built around {cap1} for teams running real capital, not paper portfolios.",
        "Combines {cap1} with {cap2} so risk reviews leave less room for hand-waving.",
    ],
    "Research": [
        "Reads the papers you don't have time for and returns a defensible summary via {cap1}.",
        "Traces a claim back to its primary source using {cap1}, every time.",
        "Synthesizes 40-paper literature reviews with {cap1} and citations you can check.",
        "Designed for working researchers who need to move from scan to synthesis, powered by {cap2}.",
        "Handles {cap1}, sample-size reasoning, and {cap2} without hand-waving.",
        "Matches your question to the right methodology via {cap1}.",
        "Pairs {cap1} with {cap2} for systematic reviews that hold up under peer review.",
        "Built for {cap1} across arXiv, PubMed, and preprint servers.",
    ],
    "Security": [
        "Scans your codebase with {cap1} the way a senior auditor would, prioritized findings first.",
        "Pentest-grade recon across web, contract, and API surfaces in one pass; emphasis on {cap1}.",
        "Triages CVE alerts via {cap1}, kills noise, writes tickets only for real issues.",
        "Threat modeling that starts from your architecture, not a template, anchored by {cap2}.",
        "Deep-dives dependency trees so {cap1} stops hiding.",
        "Your IR teammate: log correlation, blast-radius estimation, and {cap2} in a clean writeup.",
        "Runs {cap1} on pull-request scope so issues ship blocked, not deployed.",
        "Combines {cap1} with {cap2} for red-team style reviews without the red-team cost.",
    ],
    "Automation": [
        "Wires your SaaS tools together with {cap1}, without you touching a Zap.",
        "Turns a prose description of a workflow into a running pipeline via {cap1}.",
        "Handles the boring automation through {cap1}: form fills, inbox triage, calendar tetris.",
        "Scrapes, transforms, and reposts using {cap2}, with sensible retries and a clean audit log.",
        "A workflow translator built on {cap1}: paste the old spec, get a new one in the tool you use.",
        "Routes webhooks via {cap2}, reconciles state, writes the Slack summary.",
        "Pairs {cap1} with {cap2} for ops teams drowning in hand-rolled scripts.",
        "Built for {cap1} across SaaS boundaries so nothing falls through the cracks.",
    ],
}


# ── Curated category templates (names + capability banks) ────────────────────

CATEGORY_AGENT_TEMPLATES = {
    "Development": {
        "names": [
            "RefactorPilot", "CommitGuardian", "SnakeLint", "KotlinForge", "RustSentinel",
            "PyDocHero", "JavaScriptJedi", "GoPathfinder", "GitMerge AI", "APIStitcher",
            "DepResolver", "BranchBuddy", "TestSmith", "CodeCartographer", "StackTracer",
            "MonorepoMate", "SchemaShaper", "MigrationMapper",
        ],
        "tags_pool": ["python", "javascript", "typescript", "rust", "go", "linting",
                      "refactor", "git", "ci", "testing", "documentation", "api"],
        "capabilities": [
            "static code analysis", "automated refactoring", "test generation",
            "API contract verification", "dependency audit", "merge conflict resolution",
            "type-safety enforcement", "legacy code rescue",
        ],
        "use_cases": ["Code Review", "Testing"],
    },
    "Data & Analytics": {
        "names": [
            "QuerySage", "DashboardDruid", "ETL Alchemist", "SnowflakeSeer", "MetricMiner",
            "PivotPro", "ColumnCrafter", "CohortCarver", "ChartChampion", "SQL Sherpa",
            "DataDiff", "SchemaSketcher", "BigQueryButler", "KPI Keeper", "TableauTamer",
            "DWH Detective",
        ],
        "tags_pool": ["sql", "analytics", "dashboard", "etl", "bigquery", "snowflake",
                      "postgres", "visualization", "kpi", "cohort"],
        "capabilities": [
            "complex SQL generation", "dashboard scaffolding", "cohort analysis",
            "ETL pipeline design", "anomaly detection", "report automation",
            "slow-query diagnosis", "data quality checks",
        ],
        "use_cases": ["Data Processing", "Reports"],
    },
    "Content": {
        "names": [
            "BlogBard", "ScriptScribe", "HeadlineHeroes", "SEOScribe", "NewsletterNinja",
            "BrandVoice AI", "CaptionCraft", "PressPilot", "WhitepaperWeaver", "SlideSmith",
            "EmailEcho", "ThreadTailor", "PodcastPen", "StoryStitcher", "MicrocopyMuse",
        ],
        "tags_pool": ["writing", "seo", "copywriting", "blog", "newsletter", "social",
                      "email", "marketing", "content"],
        "capabilities": [
            "long-form blog posts", "SEO-optimized landing copy", "email sequences",
            "social threads", "brand-voice matching", "byline ghostwriting",
            "style-guide adherence", "tone calibration",
        ],
        "use_cases": ["Content Generation"],
    },
    "Finance": {
        "names": [
            "ArbHunter", "PortfolioPilot", "OnChainOracle", "DeFi Navigator", "TaxTamer",
            "CreditScorer", "YieldSage", "RiskRadar", "TreasuryTracker", "LedgerLens",
            "OptionsOwl", "MarketMuse", "WalletWarden", "StablecoinScout", "EquityEagle",
            "MACD Maestro", "BacktestBaron",
        ],
        "tags_pool": ["defi", "trading", "portfolio", "tax", "risk", "derivatives",
                      "options", "yield", "backtesting", "macro"],
        "capabilities": [
            "portfolio risk analysis", "on-chain transaction tracing", "tax lot reconciliation",
            "yield strategy comparison", "options pricing", "backtest generation",
            "regime-shift detection", "macro signal extraction",
        ],
        "use_cases": ["Trading", "Analysis"],
    },
    "Research": {
        "names": [
            "PaperPilot", "CitationCompass", "LitReview AI", "ArxivArchivist",
            "MetaAnalyst", "QuoteQuarry", "SourceSleuth", "AbstractArtisan",
            "HypoHelper", "ReferenceRanger", "DataDigger", "FactForager",
            "NotebookNavigator", "ThesisTethys",
        ],
        "tags_pool": ["research", "papers", "citations", "academic", "literature",
                      "meta-analysis", "arxiv", "pubmed"],
        "capabilities": [
            "literature review synthesis", "citation graph traversal", "paper summarization",
            "meta-analysis assistance", "source verification", "claim checking",
            "methodology matching", "systematic review scaffolding",
        ],
        "use_cases": ["Research", "Summarization"],
    },
    "Security": {
        "names": [
            "VulnVector", "PentestPal", "CVEHunter", "ThreatWeaver", "ZeroDayZealot",
            "PhishFinder", "SecretSweeper", "AuthAudit AI", "RBAC Ranger", "OWASPOracle",
            "SCAScout", "CryptoReviewer", "ForensicsForge", "IRCopilot", "SOC Sentinel",
            "RedTeamRover",
        ],
        "tags_pool": ["security", "pentest", "audit", "owasp", "cve", "forensics",
                      "threat-model", "vulnerabilities", "incident-response"],
        "capabilities": [
            "OWASP Top-10 scans", "dependency CVE audit", "threat modeling",
            "secret detection", "auth and RBAC review", "smart contract audit",
            "supply-chain risk mapping", "IR runbook generation",
        ],
        "use_cases": ["Security Audit"],
    },
    "Automation": {
        "names": [
            "WorkflowWiz", "ZapierZen", "CronConductor", "InboxIngestor", "SheetSorcerer",
            "ScrapeSmith", "RPA Rover", "WebhookWrangler", "FormFiller", "CalendarCopilot",
            "DocuSignScribe", "SlackBotSmith", "PipelinePilot", "NotionNavigator",
            "AirtableAce",
        ],
        "tags_pool": ["automation", "workflow", "scraping", "rpa", "webhook",
                      "integration", "zapier", "no-code"],
        "capabilities": [
            "cross-service workflow orchestration", "headless browser scraping",
            "form auto-fill", "calendar coordination", "webhook routing", "sheet macros",
            "state reconciliation", "retry and dead-letter handling",
        ],
        "use_cases": ["Automation"],
    },
}


# ── Deployer pool ────────────────────────────────────────────────────────────
# Realistic mix: indie labs, named research orgs, and some opaque DAOs.
DEPLOYERS = [
    "Catalyst AI Labs", "Forge Ops", "Prism Labs", "Synapse Collective",
    "Opentensor Studio", "Runway Research", "Helix Agents", "Vertex AI Collective",
    "MercuryAI", "Pioneer Agents Co", "Atlas Guild DAO", "Lumen Research",
    "Keystone Labs", "Driftwood AI", "Northwind Collective", "Sentinel Ops",
    "Polaris Research", "Cascade Agents", "Ember AI", "Meridian Collective",
]


def _deployer_wallet(seller: str) -> str:
    """Stable 0x + 40-hex wallet per deployer name."""
    return "0x" + hashlib.sha256(f"deployer::{seller}".encode()).hexdigest()[:40]


# ── Helpers ──────────────────────────────────────────────────────────────────

def _dash_free(text: str) -> str:
    """Strip em-dashes and en-dashes from any copy we render to users."""
    return (text.replace(" \u2014 ", ", ")
                .replace("\u2014", ",")
                .replace(" \u2013 ", ", ")
                .replace("\u2013", ",")
                .replace("\u2012", ",")
                .replace("\u2015", ","))


# ── Roster builder ───────────────────────────────────────────────────────────

def build_agents(seed: int = 42) -> list[dict]:
    rng = random.Random(seed)
    roster: list[dict] = []
    # Track which templates we've used per category to force uniqueness
    desc_cursor: dict[str, int] = {c: 0 for c in CATEGORY_AGENT_TEMPLATES}

    for category, tpl in CATEGORY_AGENT_TEMPLATES.items():
        count = rng.randint(10, 20)
        names = list(tpl["names"])
        rng.shuffle(names)
        names = names[:count]

        templates = DESCRIPTION_TEMPLATES[category]
        for name in names:
            caps = rng.sample(tpl["capabilities"], k=min(len(tpl["capabilities"]), rng.randint(3, 5)))
            # Rotate templates so descriptions don't collide within category
            tmpl_idx = desc_cursor[category] % len(templates)
            desc_cursor[category] += 1
            desc_template = templates[tmpl_idx]
            short_desc = _dash_free(desc_template.format(
                cap1=caps[0], cap2=caps[1] if len(caps) > 1 else caps[0],
            ))

            # Pick a category-appropriate model
            provider, mname, in_price, out_price = _pick_model(rng, category)
            # Agent pricing follows the underlying model with a markup band
            markup = rng.uniform(1.35, 2.25)
            per_token_price = (out_price * markup) / 1_000_000   # USDC per output token
            min_p = round(per_token_price, 6)
            max_p = round(min_p * rng.uniform(2.0, 3.5), 6)

            billing = "per_token" if out_price >= 0.5 else rng.choice(["per_token", "per_token", "per_minute"])
            rating = round(rng.uniform(3.9, 4.95), 1)
            reviews = rng.randint(12, 2400)
            tasks = rng.randint(200, 25_000)
            tags = rng.sample(tpl["tags_pool"], k=min(len(tpl["tags_pool"]), rng.randint(3, 5)))
            use_case = rng.choice(tpl["use_cases"])
            seller = rng.choice(DEPLOYERS)

            verified_roll = rng.random()
            if verified_roll > 0.25:
                verified = True
                vtier = rng.choices(["basic", "advanced", "premium"], weights=[50, 35, 15])[0]
            else:
                verified = False
                vtier = "none"

            surge_on = rng.random() < 0.35
            surge_mult = round(rng.uniform(1.1, 2.2), 2) if surge_on else 1.0
            cur_price = round(min_p * surge_mult, 6)
            if cur_price > max_p:
                cur_price = max_p

            long_desc = _dash_free(
                f"{name} is a production agent for {category.lower()} workloads. "
                f"Strengths include {', '.join(caps[:3])}. "
                f"Runs on {provider} {mname}, operated by {seller}."
            )
            if provider == "Undisclosed":
                long_desc = _dash_free(
                    f"{name} is a production agent for {category.lower()} workloads. "
                    f"Strengths include {', '.join(caps[:3])}. "
                    f"Model architecture is proprietary; deployed and operated by {seller}."
                )

            roster.append({
                "name": name,
                "description": short_desc,
                "long_description": long_desc,
                "category": category,
                "use_case": use_case,
                "verified": verified,
                "verification_tier": vtier,
                "featured": rng.random() < 0.1,
                "rating": rating,
                "reviews": reviews,
                "billing": billing,
                "min_price": min_p,
                "max_price": max_p,
                "current_price": cur_price,
                "surge_active": surge_on,
                "surge_multiplier": surge_mult,
                "seller": seller,
                "seller_rating": round(rng.uniform(4.2, 5.0), 1),
                "tasks_completed": tasks,
                "avg_completion_time": rng.choice(["30 sec", "1 min", "2 min", "4 min", "7 min", "15 min"]),
                "tags": tags,
                "capabilities": [c.capitalize() if c[:1].isalpha() else c for c in caps],
                # New fields
                "model_provider": provider,
                "model_name": mname,
                "deployer_wallet": _deployer_wallet(seller),
                "input_price_per_1m":  int(in_price * 1_000_000),   # store as micro-USDC
                "output_price_per_1m": int(out_price * 1_000_000),
            })
    return roster


# ── DB ops ───────────────────────────────────────────────────────────────────

def _ensure_columns(app) -> None:
    """Add any missing columns to the agents table (idempotent).

    Flask-SQLAlchemy's create_all does not ALTER existing tables, so we run
    targeted ALTERs for the columns we introduced on this branch.
    """
    with app.app_context():
        cols = {c[1] for c in db.session.execute(db.text("PRAGMA table_info(agents)")).fetchall()}
        needed = {
            "model_provider":      "VARCHAR(40)",
            "model_name":          "VARCHAR(80)",
            "deployer_wallet":     "VARCHAR(64)",
            "input_price_per_1m":  "INTEGER NOT NULL DEFAULT 0",
            "output_price_per_1m": "INTEGER NOT NULL DEFAULT 0",
        }
        for col, ddl in needed.items():
            if col not in cols:
                app.logger.info("ALTER TABLE agents ADD COLUMN %s", col)
                db.session.execute(db.text(f"ALTER TABLE agents ADD COLUMN {col} {ddl}"))
        db.session.commit()


def seed_bulk_agents(app) -> int:
    """Insert any missing agents. Returns number created."""
    _ensure_columns(app)
    created = 0
    with app.app_context():
        existing_names = {a.name for a in Agent.query.all()}
        for spec in build_agents():
            if spec["name"] in existing_names:
                continue
            row = Agent(
                name=spec["name"], description=spec["description"],
                long_description=spec["long_description"],
                category=spec["category"], use_case=spec["use_case"],
                verified=spec["verified"], verification_tier=spec["verification_tier"],
                featured=spec["featured"], rating=spec["rating"], reviews=spec["reviews"],
                billing=spec["billing"], min_price=spec["min_price"], max_price=spec["max_price"],
                current_price=spec["current_price"], surge_active=spec["surge_active"],
                surge_multiplier=spec["surge_multiplier"], seller=spec["seller"],
                seller_rating=spec["seller_rating"], tasks_completed=spec["tasks_completed"],
                avg_completion_time=spec["avg_completion_time"],
                model_provider=spec["model_provider"], model_name=spec["model_name"],
                deployer_wallet=spec["deployer_wallet"],
                input_price_per_1m=spec["input_price_per_1m"],
                output_price_per_1m=spec["output_price_per_1m"],
            )
            row.tags = spec["tags"]
            row.capabilities = spec["capabilities"]
            db.session.add(row)
            created += 1
        db.session.commit()
    return created


def _is_old_description(text: str) -> bool:
    """Heuristic for the legacy 'X specialist, Y and more.' template."""
    if not text:
        return True
    t = text.strip().lower()
    return (t.endswith("and more.") or
            " specialist," in t or
            " specialist —" in t)


def backfill_existing(app) -> int:
    """Assign model / deployer / pricing and rewrite stale descriptions.
    Runs every boot; only touches rows that need work."""
    _ensure_columns(app)
    updated = 0
    rng = random.Random(17)
    # Track per-category how many unique descriptions we've already assigned
    # so backfilled rows rotate through templates cleanly.
    per_cat_idx: dict[str, int] = {c: 0 for c in CATEGORY_AGENT_TEMPLATES}

    with app.app_context():
        rows = Agent.query.order_by(Agent.id).all()

        # First pass: collect existing short descriptions per category so we
        # don't backfill into a description that's already in use elsewhere.
        seen_by_cat: dict[str, set] = {c: set() for c in CATEGORY_AGENT_TEMPLATES}
        for a in rows:
            if a.category in seen_by_cat and not _is_old_description(a.description):
                seen_by_cat[a.category].add(a.description)

        # Count how many agents share each description so we can target
        # duplicates even if they're "new style" text.
        dup_counts: dict[str, int] = {}
        for a in rows:
            dup_counts[a.description or ""] = dup_counts.get(a.description or "", 0) + 1
        is_duplicate = lambda d: dup_counts.get(d or "", 0) > 1

        for a in rows:
            dirty = False
            cat_tpl = CATEGORY_AGENT_TEMPLATES.get(a.category)
            desc_tpls = DESCRIPTION_TEMPLATES.get(a.category, [])

            # 1. Rewrite if description is legacy OR duplicated with another agent
            if (_is_old_description(a.description) or is_duplicate(a.description)) and cat_tpl and desc_tpls:
                caps_pool = cat_tpl["capabilities"]
                # Use up to 20 attempts, varying both template and capability mix
                for attempt in range(20):
                    caps = rng.sample(caps_pool, k=min(len(caps_pool), 3))
                    idx = per_cat_idx[a.category] % len(desc_tpls)
                    per_cat_idx[a.category] += 1
                    candidate = _dash_free(desc_tpls[idx].format(
                        cap1=caps[0], cap2=caps[1] if len(caps) > 1 else caps[0],
                    ))
                    if candidate not in seen_by_cat[a.category]:
                        # Free up the old one's count and register the new one
                        if a.description:
                            dup_counts[a.description] = dup_counts.get(a.description, 1) - 1
                        a.description = candidate
                        seen_by_cat[a.category].add(candidate)
                        dup_counts[candidate] = 1
                        dirty = True
                        break

            # 2. Scrub any remaining em/en-dashes anywhere else
            if a.description and ("\u2014" in a.description or "\u2013" in a.description):
                a.description = _dash_free(a.description); dirty = True
            if a.long_description and ("\u2014" in a.long_description or "\u2013" in a.long_description):
                a.long_description = _dash_free(a.long_description); dirty = True

            # 3. Assign model if missing
            if not a.model_provider:
                p, m, inp, outp = _pick_model(rng, a.category)
                a.model_provider = p
                a.model_name = m
                a.input_price_per_1m = int(inp * 1_000_000)
                a.output_price_per_1m = int(outp * 1_000_000)
                dirty = True
            elif not a.input_price_per_1m:
                for p, m, inp, outp in MODELS_POOL:
                    if p == a.model_provider and m == a.model_name:
                        a.input_price_per_1m = int(inp * 1_000_000)
                        a.output_price_per_1m = int(outp * 1_000_000)
                        dirty = True
                        break

            # 4. Rewrite long_description if it's the legacy 'f"{name} is a ..."' form
            if a.long_description and "Strengths:" in a.long_description and a.model_provider:
                caps = cat_tpl["capabilities"] if cat_tpl else []
                if caps:
                    picks = rng.sample(caps, k=min(len(caps), 3))
                    if a.model_provider == "Undisclosed":
                        a.long_description = _dash_free(
                            f"{a.name} is a production agent for {a.category.lower()} workloads. "
                            f"Strengths include {', '.join(picks)}. "
                            f"Model architecture is proprietary; deployed and operated by {a.seller}."
                        )
                    else:
                        a.long_description = _dash_free(
                            f"{a.name} is a production agent for {a.category.lower()} workloads. "
                            f"Strengths include {', '.join(picks)}. "
                            f"Runs on {a.model_provider} {a.model_name}, operated by {a.seller}."
                        )
                    dirty = True

            # 5. Deployer wallet
            if not a.deployer_wallet and a.seller:
                a.deployer_wallet = _deployer_wallet(a.seller)
                dirty = True
            if dirty:
                updated += 1
        db.session.commit()
        app.logger.info("Backfill pass: updated %d agents", updated)
    return updated
