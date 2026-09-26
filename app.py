from __future__ import annotations
from flask import Flask, render_template, request, jsonify, redirect, url_for
import logging
import os
import random
import time

# Load .env manually (no python-dotenv dependency). Must happen BEFORE onchain
# or any module that reads FACILITATOR_PRIVATE_KEY at import time.
from pathlib import Path as _Path
_env_file = _Path(__file__).parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k, _v)

from config import config as _config_map
from extensions import db, cors, limiter
from auth import require_api_key

# ── Logging setup ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("agenthire")

# ── App factory ────────────────────────────────────────────────────────────────
app = Flask(__name__)

_env = os.environ.get("FLASK_ENV", "development")
app.config.from_object(_config_map.get(_env, _config_map["default"]))

# Init extensions
db.init_app(app)
cors.init_app(app, resources={r"/api/*": {"origins": app.config.get("CORS_ORIGINS", "*")}})
limiter.init_app(app)

app.jinja_env.globals['enumerate'] = enumerate

# Request logging
@app.before_request
def _log_request():
    log.info("%s %s", request.method, request.path)

@app.after_request
def _log_response(response):
    log.info("%s %s → %s", request.method, request.path, response.status_code)
    return response

# ── Mock Data ──────────────────────────────────────────────────────────────────

CATEGORIES = ["Development", "Data & Analytics", "Content", "Finance", "Research", "Security", "Automation"]
USE_CASES  = ["Code Review", "Translation", "Summarization", "Trading", "Web Scraping", "Image Generation", "Testing", "Resume & Career"]


# ── Shared helpers ─────────────────────────────────────────────────────────────
import re as _re

def _is_valid_wallet(addr: str) -> bool:
    """EVM address shape check — 0x + 40 hex. Keeps junk out of Orders."""
    return bool(_re.fullmatch(r"0x[a-fA-F0-9]{40}", str(addr or "")))


def _api_error(message: str, status: int = 400, *, code: str = "INVALID_REQUEST", field: str | None = None):
    """Uniform JSON error body: {error, code, field?}."""
    body = {"error": message, "code": code}
    if field:
        body["field"] = field
    return jsonify(body), status

AGENTS = [
    {
        "id": 1,
        "name": "CodeReview Pro",
        "description": "Deep code review, security audits, and performance optimization. Supports Python, JS, Go, Rust, and 20+ languages.",
        "long_description": "CodeReview Pro is an enterprise-grade AI agent specialized in comprehensive code analysis. It performs static analysis, identifies security vulnerabilities (OWASP Top 10), suggests optimizations, and generates detailed reports. Used by 500+ engineering teams worldwide.",
        "category": "Development",
        "use_case": "Code Review",
        "verified": True,
        "verification_tier": "thorough",
        "featured": True,
        "rating": 4.8,
        "reviews": 142,
        "billing": "per_token",
        "min_price": 0.002,
        "max_price": 0.008,
        "current_price": 0.006,
        "seller": "DevTools Inc",
        "seller_rating": 4.9,
        "tasks_completed": 8421,
        "tags": ["code", "review", "security", "python", "javascript"],
        "capabilities": ["Static analysis", "Security audit", "Performance review", "Documentation generation", "Refactoring suggestions"],
        "avg_completion_time": "4 min",
    },
    {
        "id": 2,
        "name": "TranslateFlow",
        "description": "Real-time document and content translation across 95 languages. Preserves formatting, tone, and domain-specific terminology.",
        "long_description": "TranslateFlow uses advanced neural translation models fine-tuned for domain-specific accuracy. From legal contracts to technical manuals, it maintains context and nuance across 95 languages with 98.7% accuracy on benchmark tests.",
        "category": "Content",
        "use_case": "Translation",
        "verified": True,
        "verification_tier": "thorough",
        "featured": True,
        "rating": 4.6,
        "reviews": 89,
        "billing": "per_token",
        "min_price": 0.001,
        "max_price": 0.004,
        "current_price": 0.0015,
        "seller": "LinguaAI",
        "seller_rating": 4.7,
        "tasks_completed": 12830,
        "tags": ["translation", "multilingual", "content", "NLP"],
        "capabilities": ["95 languages", "Format preservation", "Domain glossaries", "Batch processing", "Quality scoring"],
        "avg_completion_time": "2 min",
    },
    {
        "id": 3,
        "name": "DataSift Analytics",
        "description": "Autonomous data analysis agent. Feed it a CSV or database endpoint and receive structured insights, charts, and anomaly reports.",
        "long_description": "DataSift connects to your data sources and performs exploratory data analysis, statistical modeling, anomaly detection, and generates executive-level insight reports with visualizations. No SQL required.",
        "category": "Data & Analytics",
        "use_case": "Summarization",
        "verified": True,
        "verification_tier": "basic",
        "featured": False,
        "rating": 4.3,
        "reviews": 54,
        "billing": "per_minute",
        "min_price": 0.05,
        "max_price": 0.25,
        "current_price": 0.18,
        "seller": "Analytical Minds",
        "seller_rating": 4.4,
        "tasks_completed": 3201,
        "tags": ["analytics", "data", "CSV", "insights", "charts"],
        "capabilities": ["EDA automation", "Anomaly detection", "Report generation", "Chart creation", "Statistical modeling"],
        "avg_completion_time": "8 min",
    },
    {
        "id": 4,
        "name": "AlphaTrader AI",
        "description": "Quantitative trading signal agent. Analyzes market data and generates buy/sell signals with confidence scores.",
        "long_description": "AlphaTrader AI processes real-time and historical market data using ensemble ML models to generate trading signals. Includes risk scoring, portfolio correlation analysis, and backtesting summaries. Built for institutional-grade accuracy.",
        "category": "Finance",
        "use_case": "Trading",
        "verified": True,
        "verification_tier": "thorough",
        "featured": True,
        "rating": 4.9,
        "reviews": 201,
        "billing": "per_minute",
        "min_price": 0.10,
        "max_price": 0.50,
        "current_price": 0.45,
        "seller": "QuantEdge Labs",
        "seller_rating": 5.0,
        "tasks_completed": 19200,
        "tags": ["trading", "finance", "signals", "quant", "risk"],
        "capabilities": ["Signal generation", "Risk scoring", "Backtesting", "Portfolio analysis", "Real-time data"],
        "avg_completion_time": "1 min",
    },
    {
        "id": 5,
        "name": "WebCrawler X",
        "description": "Intelligent web scraping agent with anti-bot bypass, JS rendering, and structured data extraction at scale.",
        "long_description": "WebCrawler X handles complex scraping tasks including JavaScript-rendered pages, CAPTCHA bypass (ethical), pagination, and nested data extraction. Outputs clean JSON, CSV, or directly to your database.",
        "category": "Automation",
        "use_case": "Web Scraping",
        "verified": False,
        "verification_tier": "none",
        "featured": False,
        "rating": 3.9,
        "reviews": 27,
        "billing": "per_minute",
        "min_price": 0.03,
        "max_price": 0.12,
        "current_price": 0.03,
        "seller": "CrawlTech",
        "seller_rating": 3.8,
        "tasks_completed": 1540,
        "tags": ["scraping", "automation", "data", "web"],
        "capabilities": ["JS rendering", "Pagination", "Structured output", "Rate limiting", "Proxy rotation"],
        "avg_completion_time": "12 min",
    },
    {
        "id": 6,
        "name": "ResearchBot Pro",
        "description": "Academic and market research agent. Queries multiple sources, synthesizes findings, and produces citation-ready summaries.",
        "long_description": "ResearchBot Pro searches across academic papers, news, reports, and databases to produce comprehensive research summaries. Supports custom citation formats (APA, MLA, Chicago) and topic-specific depth configuration.",
        "category": "Research",
        "use_case": "Summarization",
        "verified": True,
        "verification_tier": "basic",
        "featured": False,
        "rating": 4.4,
        "reviews": 73,
        "billing": "per_token",
        "min_price": 0.003,
        "max_price": 0.010,
        "current_price": 0.005,
        "seller": "Cognify Research",
        "seller_rating": 4.5,
        "tasks_completed": 4890,
        "tags": ["research", "academia", "summarization", "citations"],
        "capabilities": ["Multi-source search", "Citation formatting", "Summary generation", "Topic clustering", "Fact verification"],
        "avg_completion_time": "6 min",
    },
    {
        "id": 7,
        "name": "SecureAudit AI",
        "description": "Smart contract and infrastructure security audit agent. Identifies vulnerabilities, misconfigurations, and compliance gaps.",
        "long_description": "SecureAudit AI performs comprehensive security audits of smart contracts (Solidity, Vyper), cloud infrastructure (AWS, GCP, Azure), and application codebases. Produces prioritized vulnerability reports with remediation steps.",
        "category": "Security",
        "use_case": "Code Review",
        "verified": True,
        "verification_tier": "thorough",
        "featured": False,
        "rating": 4.7,
        "reviews": 96,
        "billing": "per_token",
        "min_price": 0.005,
        "max_price": 0.020,
        "current_price": 0.015,
        "seller": "AuditShield",
        "seller_rating": 4.8,
        "tasks_completed": 2340,
        "tags": ["security", "audit", "smart contracts", "vulnerability"],
        "capabilities": ["Smart contract audit", "Cloud security", "Compliance check", "Penetration testing", "Remediation guide"],
        "avg_completion_time": "15 min",
    },
    {
        "id": 8,
        "name": "ContentForge",
        "description": "Long-form content generation agent for blogs, whitepapers, and marketing copy. Brand-voice configurable.",
        "long_description": "ContentForge generates high-quality long-form content with SEO optimization, brand voice alignment, and multimedia asset suggestions. Supports 40+ content types from blog posts to technical whitepapers.",
        "category": "Content",
        "use_case": "Translation",
        "verified": False,
        "verification_tier": "none",
        "featured": False,
        "rating": 3.7,
        "reviews": 18,
        "billing": "per_token",
        "min_price": 0.001,
        "max_price": 0.005,
        "current_price": 0.001,
        "seller": "CreateAI",
        "seller_rating": 3.6,
        "tasks_completed": 890,
        "tags": ["content", "writing", "SEO", "marketing", "blog"],
        "capabilities": ["Long-form writing", "SEO optimization", "Brand voice", "Multi-format", "Plagiarism check"],
        "avg_completion_time": "5 min",
    },
    {
        "id": 9,
        "name": "ImageGen Studio",
        "description": "Commercial-grade image generation agent. Produces photorealistic and artistic images from text prompts at scale.",
        "long_description": "ImageGen Studio generates high-resolution images (up to 4K) using state-of-the-art diffusion models. Supports style presets, negative prompts, batch generation, and commercial licensing on all outputs.",
        "category": "Content",
        "use_case": "Image Generation",
        "verified": True,
        "verification_tier": "basic",
        "featured": False,
        "rating": 4.5,
        "reviews": 188,
        "billing": "per_token",
        "min_price": 0.002,
        "max_price": 0.012,
        "current_price": 0.008,
        "seller": "PixelMind AI",
        "seller_rating": 4.6,
        "tasks_completed": 32100,
        "tags": ["images", "generation", "diffusion", "creative", "commercial"],
        "capabilities": ["4K resolution", "Style presets", "Batch generation", "Commercial license", "Negative prompts"],
        "avg_completion_time": "45 sec",
    },
    {
        "id": 10,
        "name": "TestingMaster",
        "description": "Automated test generation agent. Writes unit tests, integration tests, and end-to-end test suites from source code.",
        "long_description": "TestingMaster analyzes your codebase and generates comprehensive test suites including unit tests, integration tests, and E2E tests. Achieves 90%+ coverage targets automatically with meaningful test cases.",
        "category": "Development",
        "use_case": "Testing",
        "verified": True,
        "verification_tier": "basic",
        "featured": False,
        "rating": 4.2,
        "reviews": 61,
        "billing": "per_token",
        "min_price": 0.002,
        "max_price": 0.009,
        "current_price": 0.004,
        "seller": "QualityFirst",
        "seller_rating": 4.3,
        "tasks_completed": 5670,
        "tags": ["testing", "QA", "unit tests", "automation", "coverage"],
        "capabilities": ["Unit test generation", "Integration tests", "E2E tests", "Coverage analysis", "CI/CD integration"],
        "avg_completion_time": "7 min",
    },
    {
        "id": 11,
        "name": "FinanceGPT",
        "description": "Financial modeling and forecasting agent. Builds DCF models, scenario analysis, and investor-ready reports.",
        "long_description": "FinanceGPT automates financial modeling workflows including DCF valuation, Monte Carlo simulations, sensitivity analysis, and report generation. Connects to live market data APIs for real-time inputs.",
        "category": "Finance",
        "use_case": "Summarization",
        "verified": True,
        "verification_tier": "thorough",
        "featured": False,
        "rating": 4.6,
        "reviews": 44,
        "billing": "per_minute",
        "min_price": 0.08,
        "max_price": 0.30,
        "current_price": 0.20,
        "seller": "QuantEdge Labs",
        "seller_rating": 5.0,
        "tasks_completed": 1820,
        "tags": ["finance", "modeling", "DCF", "forecasting", "valuation"],
        "capabilities": ["DCF modeling", "Monte Carlo", "Scenario analysis", "Report generation", "Live market data"],
        "avg_completion_time": "10 min",
    },
    {
        "id": 13,
        "name": "ResumeBot AI",
        "description": "AI-powered resume reviewer and career coach. Rewrites bullet points, scores ATS compatibility, and tailors your resume to any job description.",
        "long_description": "ResumeBot AI analyzes your resume against job descriptions using NLP and ATS compatibility scoring. It rewrites weak bullet points using the STAR framework, suggests skills to add, flags formatting issues, and generates a tailored cover letter. Used by 10,000+ job seekers with an 87% interview rate improvement.",
        "category": "Content",
        "use_case": "Resume & Career",
        "verified": True,
        "verification_tier": "thorough",
        "featured": True,
        "rating": 4.7,
        "reviews": 312,
        "billing": "per_token",
        "min_price": 0.001,
        "max_price": 0.006,
        "current_price": 0.003,
        "seller": "CareerAI Labs",
        "seller_rating": 4.8,
        "tasks_completed": 10420,
        "tags": ["resume", "career", "job search", "ATS", "cover letter", "interview"],
        "capabilities": ["ATS scoring", "Bullet point rewriting", "Job description matching", "Cover letter generation", "Skills gap analysis"],
        "avg_completion_time": "3 min",
    },
    {
        "id": 12,
        "name": "AutoDoc AI",
        "description": "Automatic documentation agent. Generates API docs, inline comments, and README files from any codebase.",
        "long_description": "AutoDoc AI reads your entire codebase and produces comprehensive documentation including API references, inline JSDoc/docstrings, README files, and architecture diagrams. Supports 15+ frameworks.",
        "category": "Development",
        "use_case": "Code Review",
        "verified": False,
        "verification_tier": "none",
        "featured": False,
        "rating": 4.0,
        "reviews": 32,
        "billing": "per_token",
        "min_price": 0.001,
        "max_price": 0.006,
        "current_price": 0.002,
        "seller": "DocMaster",
        "seller_rating": 4.1,
        "tasks_completed": 2100,
        "tags": ["documentation", "API", "README", "developer tools"],
        "capabilities": ["API docs", "Inline comments", "README generation", "Architecture diagrams", "Multi-framework"],
        "avg_completion_time": "5 min",
    },
]

# Inference for every agent is served by the Akash-hosted Qwen endpoint.
# Previously agents claimed OpenAI/Anthropic/Google models for marketing —
# that was dishonest attribution; the actual /api/agents/<id>/generate route
# hits Akash. Reflect the truth in the marketplace.
for _a in AGENTS:
    _a["model_provider"] = "Akash"
    _a["model_name"]     = "Qwen 2.5 Coder 7B"

ORDERS = []  # Populated at runtime: /checkout POST appends here, and the
# buyer-facing /past-jobs + /active-jobs routes read from ChainTransaction
# via _buyer_jobs_from_chain. Seeded placeholders (e.g. "0x1a2b...3c4d"
# buyer strings) were obvious fakes that made the admin dashboard look
# like AI slop — removed.

VERIFICATION_QUEUE = [
    {"id": "VRF-001", "agent": "ContentForge", "agent_id": 8, "seller": "CreateAI", "tier": "basic", "status": "testing", "submitted": "2026-04-14", "safety_score": 91, "performance_score": 78, "reliability_score": 85},
    {"id": "VRF-002", "agent": "WebCrawler X", "agent_id": 5, "seller": "CrawlTech", "tier": "basic", "status": "human_review", "submitted": "2026-04-13", "safety_score": 88, "performance_score": 82, "reliability_score": 79},
    {"id": "VRF-003", "agent": "AutoDoc AI", "agent_id": 12, "seller": "DocMaster", "tier": "basic", "status": "pending", "submitted": "2026-04-15", "safety_score": None, "performance_score": None, "reliability_score": None},
    {"id": "VRF-004", "agent": "MLOps Agent", "agent_id": None, "seller": "PipelineAI", "tier": "thorough", "status": "pending", "submitted": "2026-04-15", "safety_score": None, "performance_score": None, "reliability_score": None},
    {"id": "VRF-005", "agent": "NLP Extractor", "agent_id": None, "seller": "TextLabs", "tier": "thorough", "status": "testing", "submitted": "2026-04-14", "safety_score": 95, "performance_score": 91, "reliability_score": 94},
]

PAYOUTS_SEED = [
    {"id": "PAY-001", "seller": "DevTools Inc",   "agent": "CodeReview Pro",    "amount": 1240.50, "status": "pending",  "date": "2026-04-15", "order_id": "ORD-001"},
    {"id": "PAY-002", "seller": "QuantEdge Labs", "agent": "AlphaTrader AI",    "amount": 3820.00, "status": "pending",  "date": "2026-04-15", "order_id": "ORD-002"},
    {"id": "PAY-003", "seller": "LinguaAI",       "agent": "TranslateFlow",     "amount": 540.20,  "status": "released", "date": "2026-04-14", "order_id": "ORD-003"},
    {"id": "PAY-004", "seller": "AuditShield",    "agent": "SecureAudit AI",    "amount": 2100.00, "status": "released", "date": "2026-04-14", "order_id": "ORD-004"},
    {"id": "PAY-005", "seller": "CrawlTech",      "agent": "WebCrawler X",      "amount": 180.00,  "status": "held",     "date": "2026-04-13", "order_id": None},
]

MODERATION_SEED = [
    {"id": "RPT-001", "agent": "WebCrawler X",  "agent_id": 5, "reporter": "0x1a2b...3c", "reason": "Excessive scraping caused rate limit violations on third-party APIs.",     "status": "open",          "date": "2026-04-14"},
    {"id": "RPT-002", "agent": "ContentForge",  "agent_id": 8, "reporter": "0x4d5e...6f", "reason": "Output quality fell below the advertised level on two consecutive tasks.", "status": "investigating", "date": "2026-04-13"},
    {"id": "RPT-003", "agent": "AutoDoc AI",    "agent_id": 12, "reporter": "0x7a8b...9c", "reason": "Generated documentation contained inaccurate API signatures.",             "status": "resolved",      "date": "2026-04-12"},
]

REVIEWS_SEED = [
    {"agent_id": 1, "user": "0x3a4b...5c", "rating": 5, "comment": "Fast and accurate. Saved our team hours of manual review.", "date": "2026-04-12"},
    {"agent_id": 1, "user": "0x7f8e...2a", "rating": 4, "comment": "Strong results overall. Surge pricing was steep during peak hours.", "date": "2026-04-10"},
    {"agent_id": 1, "user": "0x1d2e...9f", "rating": 5, "comment": "Best code review agent on the marketplace for this use case.", "date": "2026-04-08"},
    {"agent_id": 2, "user": "0x2b3c...4d", "rating": 5, "comment": "Translated 5 languages cleanly with correct legal phrasing.", "date": "2026-04-13"},
    {"agent_id": 3, "user": "0x5e6f...7a", "rating": 4, "comment": "Pulled clean CSV outputs with reasonable column typing.", "date": "2026-04-11"},
    {"agent_id": 4, "user": "0x8b9c...0d", "rating": 5, "comment": "Signal set identified a clear macro setup. Great confidence scores.", "date": "2026-04-09"},
    {"agent_id": 7, "user": "0xa1b2...c3", "rating": 5, "comment": "Found two high-severity issues our existing static tools missed.", "date": "2026-04-07"},
    {"agent_id": 13, "user": "0xd4e5...f6", "rating": 5, "comment": "ATS score jumped from 62 to 91 after the rewrite. Landed an interview the same week.", "date": "2026-04-06"},
]

# ── Routes ─────────────────────────────────────────────────────────────────────

_CAT_CODES = {
    "Development": "DEV", "Data & Analytics": "DAT", "Content": "CON",
    "Finance": "FIN", "Research": "RES", "Security": "SEC", "Automation": "AUT",
}

@app.route("/")
def index():
    """Landing page. Click-through to /marketplace."""
    featured = [a for a in AGENTS if a["featured"]][:6]
    return render_template("landing.html", featured=featured, stats=_live_chain_stats())

@app.route("/marketplace")
def marketplace():
    category  = request.args.get("category", "")
    use_case  = request.args.get("use_case", "")
    verified  = request.args.get("verified", "")
    featured  = request.args.get("featured", "")
    sort      = request.args.get("sort", "relevance")
    min_price = request.args.get("min_price", "")
    max_price = request.args.get("max_price", "")
    query     = request.args.get("q", "")

    agents = AGENTS[:]

    if query:
        q = query.lower()
        agents = [a for a in agents if q in a["name"].lower() or q in a["description"].lower() or any(q in t for t in a["tags"])]
    if category:
        agents = [a for a in agents if a["category"] == category]
    if use_case:
        agents = [a for a in agents if a["use_case"] == use_case]
    if verified == "verified":
        agents = [a for a in agents if a["verified"]]
    elif verified == "unverified":
        agents = [a for a in agents if not a["verified"]]
    if featured:
        agents = [a for a in agents if a["featured"]]

    # Optional price-band filters (kept query-compatible even if hidden in UI)
    try:
        if min_price not in ("", None):
            lo = float(min_price)
            agents = [a for a in agents if float(a.get("current_price") or 0) >= lo]
        if max_price not in ("", None):
            hi = float(max_price)
            agents = [a for a in agents if float(a.get("current_price") or 0) <= hi]
    except ValueError:
        pass

    # Primary relevance ranking used as default and as tie-breaker.
    rel_key = lambda a: (
        0 if a.get("featured") else 1,
        0 if a.get("verified") else 1,
        -(a.get("rating") or 0.0),
        float(a.get("current_price") or 0.0),
    )

    sort = (sort or "relevance").strip().lower()
    if sort == "price_low":
        agents.sort(key=lambda a: (
            float(a.get("current_price") or 0.0),
            0 if a.get("featured") else 1,
            0 if a.get("verified") else 1,
            -(a.get("rating") or 0.0),
        ))
    elif sort == "price_high":
        agents.sort(key=lambda a: (
            -float(a.get("current_price") or 0.0),
            0 if a.get("featured") else 1,
            0 if a.get("verified") else 1,
            -(a.get("rating") or 0.0),
        ))
    elif sort == "rating":
        agents.sort(key=lambda a: (
            -(a.get("rating") or 0.0),
            0 if a.get("featured") else 1,
            0 if a.get("verified") else 1,
            float(a.get("current_price") or 0.0),
        ))
    elif sort == "newest":
        agents.sort(key=lambda a: (-(a.get("id") or 0),) + rel_key(a))
    else:
        agents.sort(key=rel_key)

    return render_template("marketplace.html", agents=agents, categories=CATEGORIES,
                           use_cases=USE_CASES, filters={"category": category, "use_case": use_case,
                           "verified": verified, "sort": sort, "q": query, "featured": featured})

@app.route("/agent/<int:agent_id>")
def agent_detail(agent_id):
    agent = next((a for a in AGENTS if a["id"] == agent_id), None)
    if not agent:
        return redirect(url_for("marketplace"))
    # Load real reviews from DB (buyer ratings persist across sessions).
    try:
        from models import Review as ReviewModel
        rows = (ReviewModel.query
                .filter_by(agent_id=agent_id)
                .order_by(ReviewModel.created_at.desc())
                .limit(10).all())
        reviews = [r.to_dict() for r in rows]
    except Exception:
        reviews = []
    # "Works well with": verified agents from complementary categories.
    affinity = {
        "Development":      ["Security", "Data & Analytics"],
        "Data & Analytics": ["Research", "Content"],
        "Content":          ["Research", "Data & Analytics"],
        "Finance":          ["Research", "Data & Analytics"],
        "Research":         ["Content", "Data & Analytics"],
        "Security":         ["Development", "Automation"],
        "Automation":       ["Development", "Content"],
    }
    pair_cats = affinity.get(agent["category"], [])
    collaborators = [a for a in AGENTS if a["id"] != agent_id
                     and a["category"] in pair_cats and a.get("verified")][:3]
    return render_template("agent_detail.html", agent=agent, reviews=reviews,
                           collaborators=collaborators)

@app.route("/checkout/<int:agent_id>", methods=["GET", "POST"])
def checkout(agent_id):
    agent = next((a for a in AGENTS if a["id"] == agent_id), None)
    if not agent:
        return redirect(url_for("marketplace"))
    if request.method == "POST":
        data = request.get_json(silent=True) or request.form.to_dict()
        # Default amount to the agent's current price so reviewers can't checkout
        # with $0 if the frontend forgets to send the field.
        try:
            amount = float(data.get("amount") or agent.get("current_price") or agent.get("min_price") or 0)
        except (TypeError, ValueError):
            return jsonify({"error": "amount must be numeric"}), 400
        if amount <= 0:
            return jsonify({"error": "amount must be > 0"}), 400
        # Require a real buyer wallet — previously defaulted to 0x0000...0000
        # which polluted the Orders table with fake addresses.
        buyer = str(data.get("buyer") or "").strip()
        if buyer and not _is_valid_wallet(buyer):
            return jsonify({"error": "buyer must be a valid 0x-prefixed 40-hex address"}), 400
        if not buyer:
            # Fall back to the cookie set by the nav wallet connect.
            buyer = request.cookies.get("buyer_wallet") or ""
        if not _is_valid_wallet(buyer):
            return jsonify({"error": "connect a wallet first (buyer address required)"}), 400
        order_id = f"ORD-{len(ORDERS) + 1:03d}"
        new_order = {
            "id": order_id,
            "agent": agent["name"],
            "agent_id": agent_id,
            "buyer": buyer,
            "amount": amount,
            "status": "pending_payment",
            "date": time.strftime("%Y-%m-%d"),
            "task": data.get("task", ""),
        }
        ORDERS.append(new_order)
        if request.is_json:
            return jsonify({"orderId": order_id, "status": "pending_payment"}), 201
        return redirect(url_for("order_detail", order_id=order_id))
    return render_template("checkout.html", agent=agent)

@app.route("/order/<order_id>")
def order_detail(order_id):
    order = next((o for o in ORDERS if o["id"] == order_id), None)
    if not order:
        # Fall back to DB (x402 payments create Order rows keyed by session id).
        try:
            from models import Order as OrderModel
            db_order = OrderModel.query.get(order_id)
            if db_order:
                order = db_order.to_dict()
        except Exception as _e:
            log.warning("order_detail DB lookup failed for %s: %s", order_id, _e)
            order = None
    if not order:
        # Second fallback: ChainTransaction by meta.bidId or synthetic id.
        # Covers /demo and /checkout flows that fire via trigger-direct (which
        # doesn't write an Order row but does leave a CT row with the session).
        try:
            from models import ChainTransaction as CT
            import json as _json
            ct_row = None
            for r in (CT.query.order_by(CT.id.desc()).limit(400).all()):
                try:
                    m = _json.loads(r.meta or "{}")
                except Exception:
                    continue
                if (m.get("bidId") == order_id or m.get("sessionId") == order_id
                        or f"ORD-{r.id:03d}" == order_id):
                    ct_row = r
                    break
            if ct_row:
                order = {
                    "id":       order_id,
                    "agent":    next((a["name"] for a in AGENTS if a["id"] == ct_row.agent_id), f"Agent #{ct_row.agent_id}"),
                    "agent_id": ct_row.agent_id,
                    "buyer":    ct_row.from_addr or "—",
                    "amount":   (ct_row.amount_usdc or 0) / 1_000_000,
                    "status":   "in_escrow" if ct_row.kind == "deposit" else "settled",
                    "date":     time.strftime("%b %d, %Y", time.gmtime(ct_row.ts)) if ct_row.ts else "—",
                    "task":     "On-chain session",
                    "tx_hash":  ct_row.tx_hash,
                }
        except Exception as _e:
            log.warning("order_detail CT fallback failed for %s: %s", order_id, _e)
    if not order:
        return render_template("404.html", missing=f"order {order_id}"), 404
    agent = next((a for a in AGENTS if a["id"] == order["agent_id"]), None)
    if not agent:
        return render_template("404.html", missing=f"agent for order {order_id}"), 404
    return render_template("order.html", order=order, agent=agent)

@app.route("/how-it-works")
def how_it_works():
    return render_template("how_it_works.html")

def _orders_for_buyer(wallet: str, *, completed: bool) -> list:
    """Pull Order rows written by the /checkout + /demo flows for this
    buyer wallet. Enriches each with escrow/fee breakdowns + a live
    Ava Labs explorer link derived from the most recent real CT row
    whose tx hash prefix matches the Order id. Used by both
    /past-jobs (completed) and /active-jobs (in_escrow)."""
    wallet = (wallet or "").strip().lower()
    if not wallet:
        return []
    try:
        from models import Order as OrderModel, ChainTransaction as CT
        wanted_completed = {"completed", "settled"}
        wanted_active    = {"pending_payment", "in_escrow", "in_progress"}
        rows = (OrderModel.query
                .filter(db.func.lower(OrderModel.buyer) == wallet)
                .order_by(OrderModel.created_at.desc())
                .limit(50).all())
        out = []
        for o in rows:
            if completed and o.status not in wanted_completed: continue
            if (not completed) and o.status not in wanted_active: continue
            d = o.to_dict()
            amt = float(o.amount or 0)
            d["escrowedUSDC"]    = round(amt * (1 - PLATFORM_FEE_BPS / 10_000), 4)
            d["platformFeeUSDC"] = round(amt * PLATFORM_FEE_BPS / 10_000, 4)
            # Find the real tx for this order by matching the ORD-<prefix>
            # against the CT table's tx_hash. Gives us a live explorer link.
            short = o.id.replace("ORD-", "").lower() if o.id.startswith("ORD-") else ""
            full_hash = ""
            if short and len(short) >= 4:
                ct = (CT.query
                      .filter(db.func.lower(CT.tx_hash).like(f"%{short}%"))
                      .order_by(CT.id.desc()).first())
                if ct and ct.tx_hash:
                    full_hash = ct.tx_hash if ct.tx_hash.startswith("0x") else "0x" + ct.tx_hash
            d["txHash"]      = full_hash
            d["snowtrace"]   = f"https://subnets-test.avax.network/c-chain/tx/{full_hash}" if full_hash else ""
            d["sourceChain"] = "Avalanche Fuji" if full_hash else None
            d["orderUrl"]    = f"/order/{o.id}"
            out.append(d)
        return out
    except Exception as _e:
        log.warning("orders_for_buyer failed: %s", _e)
        return []


@app.route("/active-jobs")
def active_jobs():
    wallet = (request.args.get("wallet")
              or request.cookies.get("buyer_wallet")
              or "")
    chain_orders = _buyer_jobs_from_chain(wallet, include_settled=False, include_active=True) if wallet else []
    db_orders    = _orders_for_buyer(wallet, completed=False)
    orders = db_orders + chain_orders
    return render_template("active_jobs.html", orders=orders, wallet=wallet)

@app.route("/past-jobs")
def past_jobs():
    wallet = (request.args.get("wallet")
              or request.cookies.get("buyer_wallet")
              or "")
    chain_orders = _buyer_jobs_from_chain(wallet, include_settled=True, include_active=False) if wallet else []
    db_orders    = _orders_for_buyer(wallet, completed=True)
    orders = db_orders + chain_orders
    return render_template("past_jobs.html", orders=orders, wallet=wallet)

@app.route("/api/buyer/<wallet>/jobs")
def api_buyer_jobs(wallet):
    active = _buyer_jobs_from_chain(wallet, include_settled=False, include_active=True)
    past = _buyer_jobs_from_chain(wallet, include_settled=True, include_active=False)
    return jsonify({
        "wallet": wallet, "active": active, "past": past,
        "totals": {
            "activeCount": len(active),
            "completedCount": len([j for j in past if j["status"] == "completed"]),
            "cancelledCount": len([j for j in past if j["status"] == "cancelled"]),
            "totalEscrowedUSDC": round(sum(j["escrowedUSDC"] for j in active), 4),
            "totalSpentUSDC": round(sum(j["amount"] for j in past + active), 4),
            "totalFeesPaidUSDC": round(sum(j["platformFeeUSDC"] for j in past + active), 4),
        },
        "platformFeeBps": PLATFORM_FEE_BPS, "chain": "Avalanche Fuji",
        "source": "onchain:EscrowPayment.getSession",
    })

@app.route("/list-your-agent")
def list_your_agent():
    return redirect(url_for("seller_create"))

# ── Agent Mode ──────────────────────────────────────────────────────────────

# ── Seller ─────────────────────────────────────────────────────────────────────

@app.route("/seller/dashboard")
def seller_dashboard():
    # Legacy path: keep route alive, but canonical seller dashboard is /seller/earnings.
    return redirect(url_for("seller_earnings"))

@app.route("/seller/create", methods=["GET", "POST"])
def seller_create():
    if request.method == "POST":
        from models import Agent as AgentModel, VerificationEntry
        data = request.get_json(silent=True) or request.form.to_dict()

        wallet = str(data.get("wallet") or "").strip()
        if not _is_valid_wallet(wallet):
            return jsonify({"error": "connect a wallet first (seller wallet required)",
                            "field": "wallet"}), 400

        required = ["name", "description", "category", "billing"]
        missing = [k for k in required if not data.get(k)]
        if missing:
            return jsonify({"error": f"missing required fields: {missing}"}), 400
        if data["billing"] not in ("per_token", "per_minute"):
            return jsonify({"error": "billing must be per_token or per_minute",
                            "field": "billing"}), 400

        def _num(key, default=0.0):
            try:
                return float(data.get(key) or default)
            except (TypeError, ValueError):
                return default

        # Wizard sends USD per 1M tokens for input/output.
        in_per_1m = _num("min_input_tokens")
        out_per_1m = _num("min_output_tokens")
        min_price = _num("min_price", 0.001)
        max_price = max(_num("max_price", 0.010), min_price)

        row = AgentModel(
            name=data["name"],
            description=data.get("description", ""),
            long_description=data.get("long_description") or data.get("description", ""),
            category=data["category"],
            use_case=data.get("use_case", ""),
            verified=False, verification_tier="none", featured=False,
            rating=0.0, reviews=0,
            billing=data["billing"],
            min_price=min_price, max_price=max_price, current_price=min_price,
            seller=data.get("seller") or wallet,
            seller_rating=0.0, tasks_completed=0,
            avg_completion_time=data.get("avg_completion_time", " - "),
            deployer_wallet=wallet.lower(),
            input_price_per_1m=int(in_per_1m * 1_000_000),
            output_price_per_1m=int(out_per_1m * 1_000_000),
        )
        model = str(data.get("model") or "")
        if "|" in model:
            row.model_provider, row.model_name = [m.strip()[:80] for m in model.split("|", 1)]
        row.tags = [t.strip() for t in (data.get("tags") or "").split(",") if t.strip()]
        row.capabilities = [c.strip() for c in (data.get("capabilities") or "").split("\n") if c.strip()]
        db.session.add(row)
        db.session.flush()   # populate row.id

        db.session.add(VerificationEntry(
            id=f"VRF-{row.id:03d}",
            agent_id=row.id, agent_name=row.name, seller=row.seller,
            tier=data.get("verification_tier", "basic"),
            status="pending", submitted=time.strftime("%Y-%m-%d"),
        ))
        db.session.commit()
        AGENTS.append(row.to_dict())
        log.info("New agent %s (id=%d) listed by %s", row.name, row.id, wallet)

        if request.is_json:
            return jsonify({
                "agentId": row.id, "status": "listed", "wallet": wallet,
                "message": "Agent listed and queued for verification.",
            }), 201
        return redirect(url_for("agent_detail", agent_id=row.id))
    return render_template("seller/create.html", categories=CATEGORIES, use_cases=USE_CASES)

@app.route("/seller/verification")
def seller_verification():
    my_queue = VERIFICATION_QUEUE[:3]
    return render_template("seller/verification.html", queue=my_queue)

@app.route("/seller/orders")
def seller_orders():
    seller = request.args.get("seller", "").strip()
    if seller:
        names = {a["name"] for a in AGENTS if a["seller"].lower() == seller.lower()}
    else:
        default_seller = AGENTS[0]["seller"] if AGENTS else ""
        names = {a["name"] for a in AGENTS if a["seller"] == default_seller}
        seller = default_seller
    orders = [o for o in ORDERS if o["agent"] in names]
    return render_template("seller/orders.html", orders=orders, seller=seller)

@app.route("/seller/earnings")
def seller_earnings():
    # Accept wallet from query, then cookie, so clicking "Manage" from the
    # bond notice after a successful /seller/create submit shows the user's
    # agents — not "No seller wallet selected".
    wallet = (request.args.get("wallet") or
              request.cookies.get("seller_wallet") or
              request.cookies.get("buyer_wallet") or "")
    wallet_lc = wallet.lower()
    my_agents = [a for a in AGENTS
                 if wallet_lc and (str(a.get("seller", "")).lower() == wallet_lc
                                   or str(a.get("deployer_wallet") or "").lower() == wallet_lc)]
    earnings = _seller_earnings_from_chain([a["id"] for a in my_agents])
    orders = [o for o in ORDERS if o["agent"] in {a["name"] for a in my_agents}]

    # Real transaction history for this seller's agents, from ChainTransaction.
    # Replaces the 5 hardcoded rows; grows with every on-chain event.
    from models import ChainTransaction as CT
    import json as _json
    my_agent_ids = [a["id"] for a in my_agents]
    tx_history = []
    if my_agent_ids:
        # Real on-chain txs only — the sim rows flooded this table with
        # fake 'Task Payment' entries for agents that never earned anything.
        ct_rows = (CT.query.filter(CT.agent_id.in_(my_agent_ids))
                   .filter(CT.kind.in_(["deposit", "settle", "slash", "stake", "a2a_hire", "a2a_settle"]))
                   .filter(CT.meta.like('%"real": true%'))
                   .order_by(CT.id.desc()).limit(25).all())
        id_to_name = {a["id"]: a["name"] for a in my_agents}
        for r in ct_rows:
            try: m = _json.loads(r.meta or "{}")
            except Exception: m = {}
            amount = (r.amount_usdc or 0) / 1_000_000
            fee = round(amount * PLATFORM_FEE_BPS / 10_000, 4) if r.kind == "deposit" else 0.0
            kind_label = {"deposit": "Deposit", "settle": "Task Payment",
                          "slash": "Slash", "stake": "Stake"}.get(r.kind, r.kind)
            tx_history.append({
                "date":   time.strftime("%b %d, %Y", time.gmtime(r.ts)) if r.ts else "—",
                "agent":  id_to_name.get(r.agent_id, f"Agent #{r.agent_id}"),
                "type":   kind_label,
                "amount": round(amount, 2),
                "fee":    fee,
                "net":    round(amount - fee, 3),
                "status": "released" if r.kind == "settle" else ("escrow" if r.kind == "deposit" else r.kind),
                "real":   bool(m.get("real")),
                "txHash": r.tx_hash,
                "snowtrace": (f"https://subnets-test.avax.network/c-chain/tx/{r.tx_hash if r.tx_hash.startswith('0x') else '0x' + r.tx_hash}") if r.tx_hash else None,
            })

    # Weekly settle counts per day (last 7 days) for the usage chart — live from CT
    import datetime as _dt
    weekly_labels, weekly_tasks = [], []
    for i in range(6, -1, -1):
        day_start_dt = (_dt.datetime.utcnow() - _dt.timedelta(days=i)).replace(hour=0, minute=0, second=0, microsecond=0)
        day_end_dt   = day_start_dt + _dt.timedelta(days=1)
        day_start = int(day_start_dt.replace(tzinfo=_dt.timezone.utc).timestamp())
        day_end   = int(day_end_dt.replace(tzinfo=_dt.timezone.utc).timestamp())
        if my_agent_ids:
            n = db.session.query(db.func.count(CT.id)).filter(
                CT.agent_id.in_(my_agent_ids),
                CT.kind.in_(["settle", "a2a_settle"]),
                CT.meta.like('%"real": true%'),
                CT.ts >= day_start, CT.ts < day_end
            ).scalar() or 0
        else:
            n = 0
        weekly_labels.append(day_start_dt.strftime("%a"))
        weekly_tasks.append(int(n))

    return render_template("seller/earnings.html", earnings=earnings,
                           agents=my_agents, orders=orders,
                           tx_history=tx_history, seller=wallet,
                           weekly_labels=weekly_labels, weekly_tasks=weekly_tasks)


@app.route("/seller/agents/<int:agent_id>", methods=["GET", "POST"])
def seller_manage_agent(agent_id):
    agent = next((a for a in AGENTS if a["id"] == agent_id), None)
    if not agent:
        return redirect(url_for("seller_earnings"))
    if request.method == "POST":
        data = request.get_json(silent=True) or request.form.to_dict()
        action = data.get("action", "")
        if action == "pause":
            agent["verification_tier"] = agent.get("verification_tier", "none") + "_paused" if not agent.get("verification_tier", "").endswith("_paused") else agent["verification_tier"]
            log.info("Agent %s paused by seller", agent_id)
            return jsonify({"agentId": agent_id, "status": "paused"})
        elif action == "reactivate":
            log.info("Agent %s reactivated by seller", agent_id)
            return jsonify({"agentId": agent_id, "status": "active"})
        elif action == "update":
            for field in ["name", "description", "min_price", "max_price", "tags"]:
                if field in data:
                    if field in ("min_price", "max_price"):
                        agent[field] = float(data[field])
                    elif field == "tags":
                        agent[field] = [t.strip() for t in data[field].split(",") if t.strip()]
                    else:
                        agent[field] = data[field]
            log.info("Agent %s updated by seller", agent_id)
            return jsonify({"agentId": agent_id, "status": "updated"})
    return render_template("seller/manage.html", agent=agent, categories=CATEGORIES)

# ── Admin ──────────────────────────────────────────────────────────────────────

@app.route("/admin/dashboard")
def admin_dashboard():
    # Default to live-on-chain view so reviewers first-impression isn't 4k+
    # "active orders" from seeded sim ticks. Explicit ?all=1 flips to the
    # full aggregate; ?live_only=0 also respected for backwards compat.
    all_flag  = request.args.get("all", "").lower() in ("1", "true", "yes")
    live_raw  = request.args.get("live_only", "").lower()
    live_only = not all_flag if live_raw == "" else (live_raw in ("1", "true", "yes"))
    s = _live_chain_stats(live_only=live_only)
    # Real per-hour revenue (last 24h) aggregated from ChainTransaction settlements.
    try:
        from models import ChainTransaction as CT
        from sqlalchemy import func as sfn
        now_ts = int(time.time())
        hourly = [0.0] * 24
        labels = []
        for i in range(23, -1, -1):
            hstart = now_ts - (i + 1) * 3600
            hend   = now_ts - i * 3600
            q = db.session.query(sfn.sum(CT.amount_usdc)).filter(
                CT.kind.in_(["settle", "a2a_settle", "a2a_hire"]),
                CT.ts >= hstart, CT.ts < hend,
            )
            if live_only:
                q = q.filter(CT.meta.like('%"real": true%'))
            total_micro = q.scalar() or 0
            hourly[23 - i] = round(total_micro / 1_000_000, 2)
            import datetime as _dt2
            labels.append(_dt2.datetime.fromtimestamp(hend, tz=_dt2.timezone.utc).strftime("%H:00"))
        s["hourly_revenue"] = hourly
        s["revenue_labels"] = labels
    except Exception as e:
        log.warning("hourly revenue aggregation failed: %s", e)
        s["hourly_revenue"] = [0] * 24
        s["revenue_labels"] = [f"{i:02d}:00" for i in range(24)]
    # Base/fee breakdown from settled txs. Protocol fee is bps.
    base_rev  = round(s.get("total_volume", 0) or 0, 2)
    fees      = round(s.get("platform_fees", 0) or 0, 4)
    s["breakdown_labels"] = ["Base Revenue", "Protocol Fees"]
    s["breakdown_values"] = [base_rev, fees]
    return render_template("admin/dashboard.html", stats=s, agents=AGENTS)

@app.route("/admin/verification-queue")
def admin_verification_queue():
    return render_template("admin/verification_queue.html", queue=VERIFICATION_QUEUE)


@app.route("/admin/review/<vrf_id>", methods=["GET", "POST"])
def admin_human_review(vrf_id):
    """Human review panel for Thorough Audit tier agents."""
    entry = next((v for v in VERIFICATION_QUEUE if v["id"] == vrf_id), None)
    if not entry:
        return redirect(url_for("admin_verification_queue"))
    agent = next((a for a in AGENTS if a["id"] == entry.get("agent_id")), None)
    if request.method == "POST":
        data = request.get_json(silent=True) or request.form.to_dict()
        action = data.get("action")
        if action == "approve":
            entry["status"] = "approved"
            if agent:
                agent["verified"] = True
                agent["verification_tier"] = entry.get("tier", "basic")
            log.info("Human review approved: %s", vrf_id)
            return jsonify({"id": vrf_id, "status": "approved"})
        elif action == "reject":
            entry["status"] = "rejected"
            log.info("Human review rejected: %s notes=%s", vrf_id, data.get("notes", ""))
            return jsonify({"id": vrf_id, "status": "rejected"})
    return render_template("admin/review.html", entry=entry, agent=agent)

@app.route("/admin/moderation")
def admin_moderation():
    from models import ModerationReport
    reports = [r.to_dict() for r in
               ModerationReport.query.order_by(ModerationReport.created_at.desc()).all()]
    return render_template("admin/moderation.html", reports=reports)


@app.route("/admin/payouts")
def admin_payouts():
    from models import Payout
    payouts = [p.to_dict() for p in
               Payout.query.order_by(Payout.created_at.desc()).all()]
    # Same default as /admin/dashboard — show live-on-chain numbers first.
    all_flag  = request.args.get("all", "").lower() in ("1", "true", "yes")
    live_raw  = request.args.get("live_only", "").lower()
    live_only = not all_flag if live_raw == "" else (live_raw in ("1", "true", "yes"))
    s = _live_chain_stats(live_only=live_only)
    s["hourly_revenue"] = [0]*24
    s["revenue_labels"] = [f"{i:02d}:00" for i in range(24)]
    return render_template("admin/payouts.html", payouts=payouts, stats=s)

# ── API (mock) ─────────────────────────────────────────────────────────────────

# ── x402 / on-chain integration ────────────────────────────────────────────────
# FACILITATOR_PRIVATE_KEY set → onchain.py submits buyer-signed authorizations.
# Otherwise orders are recorded as pending_payment and no tx is sent.
import os
import uuid
FACILITATOR_URL = os.environ.get("FACILITATOR_URL")

_onchain = None
def _get_onchain():
    global _onchain
    if _onchain is None:
        try:
            from onchain import OnChain
            _onchain = OnChain.from_env()
        except Exception as e:
            print(f"[onchain] unavailable: {e}")
            _onchain = False
    return _onchain or None

# ── Live on-chain aggregates (sourced from ChainTransaction audit log) ──────
PLATFORM_FEE_BPS = 10

def _live_chain_stats(live_only: bool = False) -> dict:
    """Site-wide aggregate from ChainTransaction.
    live_only=True restricts to rows with meta.real=true (real Fuji txs
    fired from the demo), hiding the tick-engine backfill."""
    from models import ChainTransaction as CT
    from sqlalchemy import func as sfn
    import datetime as _dt
    def _scope(q):
        return q.filter(CT.meta.like('%"real": true%')) if live_only else q
    try:
        deposit_micro = _scope(db.session.query(sfn.sum(CT.amount_usdc))).filter(CT.kind == "deposit").scalar() or 0
        settle_micro  = _scope(db.session.query(sfn.sum(CT.amount_usdc))).filter(CT.kind == "settle").scalar() or 0
        stake_micro   = _scope(db.session.query(sfn.sum(CT.amount_usdc))).filter(CT.kind == "stake").scalar() or 0
        deposit_count = _scope(db.session.query(sfn.count(CT.id))).filter(CT.kind == "deposit").scalar() or 0
        settle_count  = _scope(db.session.query(sfn.count(CT.id))).filter(CT.kind == "settle").scalar() or 0
        slash_count   = _scope(db.session.query(sfn.count(CT.id))).filter(CT.kind == "slash").scalar() or 0
        # Transparency counts (always available, regardless of live_only)
        total_all = db.session.query(sfn.count(CT.id)).scalar() or 0
        real_all  = db.session.query(sfn.count(CT.id)).filter(CT.meta.like('%"real": true%')).scalar() or 0
        now_ts = int(time.time())
        monthly, labels = [], []
        for i in range(11, -1, -1):
            d = _dt.datetime.utcfromtimestamp(now_ts) - _dt.timedelta(days=i*30)
            mstart = int(_dt.datetime(d.year, d.month, 1, tzinfo=_dt.timezone.utc).timestamp())
            mnext = int(_dt.datetime(d.year + (1 if d.month == 12 else 0), 1 if d.month == 12 else d.month+1, 1, tzinfo=_dt.timezone.utc).timestamp())
            v = _scope(db.session.query(sfn.sum(CT.amount_usdc))).filter(CT.kind == "deposit", CT.ts >= mstart, CT.ts < mnext).scalar() or 0
            monthly.append(round(v / 1_000_000, 2))
            labels.append(d.strftime("%b"))
        total_volume = deposit_micro / 1_000_000
        return {
            "total_agents": len(AGENTS),
            "verified_agents": len([a for a in AGENTS if a.get("verified")]),
            "tasks_completed": int(settle_count),
            "usdc_settled": round(settle_micro / 1_000_000, 2),
            "total_volume": round(total_volume, 2),
            "platform_fees": round(total_volume * PLATFORM_FEE_BPS / 10_000, 4),
            "deposit_count": int(deposit_count),
            "settle_count": int(settle_count),
            "slash_count": int(slash_count),
            "total_stake_usdc": round(stake_micro / 1_000_000, 2),
            "active_orders": max(0, int(deposit_count) - int(settle_count)),
            "pending_verifications": len([v for v in VERIFICATION_QUEUE if v["status"] in ("pending","testing","human_review")]) if 'VERIFICATION_QUEUE' in globals() else 0,
            "monthly": monthly,
            "monthly_labels": labels,
            "live_only":       bool(live_only),
            "total_ct_rows":   int(total_all),
            "real_ct_rows":    int(real_all),
            "source": "onchain:ChainTransaction" + (" (live only)" if live_only else ""),
        }
    except Exception as e:
        return {"total_agents": len(AGENTS), "verified_agents": 0, "tasks_completed": 0,
                "usdc_settled": 0, "total_volume": 0, "platform_fees": 0,
                "deposit_count": 0, "settle_count": 0, "slash_count": 0,
                "total_stake_usdc": 0, "active_orders": 0, "pending_verifications": 0,
                "monthly": [0]*12, "monthly_labels": [],
                "live_only": bool(live_only), "total_ct_rows": 0, "real_ct_rows": 0,
                "source": "fallback", "error": str(e)[:120]}


def _seller_earnings_from_chain(agent_ids: list) -> dict:
    """Seller revenue aggregation. Every figure is filtered to REAL Fuji
    txs (meta.real=true). Previously included the 136k seeded sim rows
    which made /seller/earnings show fabricated $1,994 revenue for a
    seller who had never actually been hired."""
    from models import ChainTransaction as CT
    from sqlalchemy import func as sfn
    import datetime as _dt
    if not agent_ids:
        return {"total_revenue": 0, "platform_fees": 0, "escrow_funds": 0,
                "released_payouts": 0, "monthly": [0]*12,
                "labels": [], "source": "onchain"}
    REAL = '%"real": true%'
    try:
        ids = [int(a) for a in agent_ids]
        revenue_micro = db.session.query(sfn.sum(CT.amount_usdc)).filter(
            CT.agent_id.in_(ids), CT.kind.in_(["deposit", "a2a_hire"]),
            CT.meta.like(REAL)).scalar() or 0
        settled_micro = db.session.query(sfn.sum(CT.amount_usdc)).filter(
            CT.agent_id.in_(ids), CT.kind.in_(["settle", "a2a_settle"]),
            CT.meta.like(REAL)).scalar() or 0
        now_ts = int(time.time())
        monthly, labels = [], []
        for i in range(11, -1, -1):
            d = _dt.datetime.utcfromtimestamp(now_ts) - _dt.timedelta(days=i*30)
            mstart = int(_dt.datetime(d.year, d.month, 1, tzinfo=_dt.timezone.utc).timestamp())
            mnext = int(_dt.datetime(d.year + (1 if d.month == 12 else 0), 1 if d.month == 12 else d.month+1, 1, tzinfo=_dt.timezone.utc).timestamp())
            v = db.session.query(sfn.sum(CT.amount_usdc)).filter(
                CT.agent_id.in_(ids), CT.kind.in_(["deposit", "a2a_hire"]),
                CT.meta.like(REAL), CT.ts >= mstart, CT.ts < mnext
            ).scalar() or 0
            monthly.append(round(v / 1_000_000, 2))
            labels.append(d.strftime("%b"))
        total_revenue = round(revenue_micro / 1_000_000, 2)
        return {"total_revenue": total_revenue,
                "platform_fees": round(total_revenue * PLATFORM_FEE_BPS / 10_000, 4),
                "escrow_funds": round(max(0, revenue_micro - settled_micro) / 1_000_000, 2),
                "released_payouts": round(settled_micro / 1_000_000, 2),
                "monthly": monthly,
                "labels": labels, "source": "onchain:ChainTransaction(real)"}
    except Exception as e:
        return {"total_revenue": 0, "platform_fees": 0, "escrow_funds": 0,
                "released_payouts": 0, "monthly": [0]*12,
                "labels": [], "source": "fallback", "error": str(e)[:120]}


def _buyer_jobs_from_chain(wallet: str, *, include_settled: bool, include_active: bool) -> list:
    """Per-buyer job list. Every deposit CT row for this wallet is a job;
    status is derived from whether a corresponding settle row exists for the
    same agent. When a sessionId is present in meta, also do a live
    EscrowPayment.getSession read for authoritative state."""
    from models import ChainTransaction as CT
    from collections import Counter
    import json as _json
    wallet_lc = (wallet or "").strip().lower()
    if not wallet_lc:
        return []

    # Only rows targeting a real agent — plain USDC transfers with no agent_id
    # are not escrow sessions and should not appear as "jobs."
    deposit_rows = (CT.query.filter(CT.kind == "deposit")
                    .filter(db.func.lower(CT.from_addr) == wallet_lc)
                    .filter(CT.agent_id.isnot(None))
                    .filter(CT.agent_id > 0)
                    .order_by(CT.ts.desc()).limit(100).all())

    # Precompute settle counts per agent so we can pair each deposit with a
    # settle (oldest-first). Gives honest in_escrow vs completed status.
    agent_ids_seen = {r.agent_id for r in deposit_rows if r.agent_id}
    settle_budget = {}
    if agent_ids_seen:
        settle_rows = (CT.query.filter(CT.kind == "settle")
                       .filter(CT.agent_id.in_(agent_ids_seen)).all())
        settle_budget = Counter(s.agent_id for s in settle_rows)

    oc = _get_onchain()
    out = []
    dep_count = Counter()
    # Oldest deposits pair with the available settles first
    for r in sorted(deposit_rows, key=lambda x: x.ts or 0):
        try: meta = _json.loads(r.meta or "{}")
        except Exception: meta = {}
        sid = meta.get("sessionId") or meta.get("session_id")

        live = None
        if sid is not None and oc:
            try: live = oc.get_session(int(sid))
            except Exception: live = None

        if live:
            total_micro = int(live.get("totalDeposit", 0))
            settled = bool(live.get("settled"))
            cancelled = bool(live.get("cancelled"))
            agent_id = int(live.get("agentId", r.agent_id or 0))
            expires_at = int(live.get("expiresAt", 0))
        else:
            total_micro = int(r.amount_usdc or 0)
            agent_id = r.agent_id or 0
            dep_count[agent_id] += 1
            settled = dep_count[agent_id] <= settle_budget.get(agent_id, 0)
            cancelled = False
            expires_at = 0

        amount_usdc = total_micro / 1_000_000
        platform_fee = round(amount_usdc * PLATFORM_FEE_BPS / 10_000, 6)
        status = ("completed" if settled else "cancelled" if cancelled else "in_escrow")
        if status in ("completed","cancelled") and not include_settled: continue
        if status == "in_escrow" and not include_active: continue

        agent = next((a for a in AGENTS if a["id"] == agent_id), None)
        display_id = str(sid) if sid else f"tx-{r.tx_hash[:10]}"
        out.append({
            "id": display_id,
            "agent": agent["name"] if agent else f"Agent #{agent_id}",
            "agent_id": agent_id,
            "amount": round(amount_usdc, 4),
            "escrowedUSDC": round(amount_usdc - platform_fee, 4),
            "platformFeeUSDC": platform_fee,
            "status": status,
            "task": f"Escrow session {display_id}",
            "date": time.strftime("%Y-%m-%d", time.gmtime(r.ts)) if r.ts else "",
            "txHash": r.tx_hash,
            "snowtrace": (f"https://testnet.snowtrace.io/tx/{r.tx_hash}"
                          if bool(meta.get("real"))
                          else "https://testnet.snowtrace.io/address/0xD19990C7CB8C386fa865135Ce9706A5A37A3f2f2"),
            "blockNumber": int(r.block_number or 0),
            "expiresAt": expires_at,
            "sourceChain": bool(live),
            "real": bool(meta.get("real")),
        })
    # Return newest first for display
    out.reverse()
    return out


def _new_order_id() -> str:
    return f"ORD-{uuid.uuid4().hex[:8].upper()}"


def _record_order_from_payment(agent_id: int, buyer: str, amount_usdc: float, *,
                               paid: bool, task: str = "", tx_hash: str = "") -> str:
    """Persist an Order (and, for real payments, a ChainTransaction row).
    Returns the new order id."""
    from models import Order as OrderModel, ChainTransaction as CT
    import json as _json
    order_id = _new_order_id()
    status = "in_escrow" if paid else "pending_payment"
    today = time.strftime("%Y-%m-%d")
    db.session.add(OrderModel(
        id=order_id, agent_id=int(agent_id), buyer=buyer,
        amount=float(amount_usdc), status=status,
        task=task or "Hire via x402 payment", date=today,
    ))
    if paid and tx_hash:
        db.session.add(CT(
            tx_hash=tx_hash, ts=int(time.time()), kind="payment",
            agent_id=int(agent_id), from_addr=buyer, to_addr="",
            amount_usdc=int(round(amount_usdc * 1_000_000)),
            meta=_json.dumps({"real": True, "orderId": order_id}),
        ))
    db.session.commit()
    return order_id


@app.route("/api/x402/pay", methods=["POST"])
@limiter.limit("30/minute")
def api_x402_pay():
    """Execute a buyer-signed EIP-3009 transferWithAuthorization via the
    facilitator and record the resulting order."""
    payload = request.get_json(silent=True) or {}
    required = ["from", "to", "value", "validBefore", "nonce", "v", "r", "s", "agentId"]
    missing = [k for k in required if k not in payload]
    if missing:
        return _api_error(f"missing fields: {missing}", field=missing[0])
    for field in ("from", "to"):
        if not _is_valid_wallet(payload.get(field)):
            return _api_error(f"'{field}' must be a 0x-prefixed 40-hex address", field=field)
    try:
        value_micro = int(payload["value"])
        agent_id = int(payload["agentId"])
    except (TypeError, ValueError):
        return _api_error("value and agentId must be integers", field="value")
    if value_micro <= 0:
        return _api_error("value must be > 0", field="value")
    from models import Agent as AgentModel
    if not db.session.get(AgentModel, agent_id):
        return _api_error("agent not found", 404, code="AGENT_NOT_FOUND", field="agentId")

    amount_usdc = value_micro / 1_000_000.0
    buyer_addr = payload["from"]
    task = str(payload.get("task") or "")[:4000]

    oc = _get_onchain()
    if oc and oc.facilitator:
        try:
            result = oc.x402_execute(payload)
        except Exception as e:
            log.warning("x402 execute failed: %s", e)
            return _api_error(f"on-chain payment failed: {str(e)[:200]}", 502, code="PAYMENT_FAILED")
        tx_hash = (result.get("txHashes") or {}).get("permit") or ""
        order_id = _record_order_from_payment(agent_id, buyer_addr, amount_usdc,
                                              paid=True, task=task, tx_hash=tx_hash)
        return jsonify({**result, "orderId": order_id, "realTx": True})

    # No facilitator configured: record the order as awaiting payment.
    order_id = _record_order_from_payment(agent_id, buyer_addr, amount_usdc, paid=False, task=task)
    return jsonify({
        "orderId": order_id,
        "agentId": agent_id,
        "status": "pending_payment",
        "realTx": False,
        "note": "FACILITATOR_PRIVATE_KEY is not set, so the signed authorization was not submitted on-chain.",
    })

# Submit a dispute. Always lands in the moderation queue; when a gatekeeper
# key and reputation contract are configured, also records an on-chain incident.
@app.route("/api/dispute/submit", methods=["POST"])
def api_dispute_submit():
    from models import ModerationReport
    payload = request.get_json(silent=True) or {}
    for k in ("agentId", "severity", "reason", "affectedUser"):
        if k not in payload or payload.get(k) in (None, ""):
            return _api_error(f"missing {k}", field=k)
    if not _is_valid_wallet(payload.get("affectedUser")):
        return _api_error("affectedUser must be a 0x-prefixed 40-hex address", field="affectedUser")
    try:
        agent_id = int(payload["agentId"])
        severity = int(payload["severity"])
    except (TypeError, ValueError):
        return _api_error("agentId and severity must be integers", field="agentId")
    if severity not in (1, 2):
        return _api_error("severity must be 1 or 2", field="severity")

    from models import Agent as AgentModel
    agent = db.session.get(AgentModel, agent_id)
    report = ModerationReport(
        id=f"RPT-{uuid.uuid4().hex[:8].upper()}",
        agent=agent.name if agent else f"Agent #{agent_id}",
        agent_id=agent_id,
        reporter=payload["affectedUser"],
        reason=str(payload["reason"])[:4000],
        status="open",
        date=time.strftime("%Y-%m-%d"),
        notes=f"order {payload.get('orderId')}" if payload.get("orderId") else "",
    )
    db.session.add(report)
    db.session.commit()

    result = {"status": "pending_review", "reportId": report.id}
    oc = _get_onchain()
    if oc and oc.gatekeeper:
        try:
            result.update(oc.submit_incident(agent_id, payload["affectedUser"], severity))
        except Exception as e:
            log.warning("on-chain incident failed (report %s kept): %s", report.id, e)
            result["onchainError"] = str(e)[:200]
    return jsonify(result), 201


# Read a live escrow session from chain. Prefers Python-native onchain.py,
# falls back to the Node facilitator if configured.
@app.route("/api/session/<session_id>")
def api_session(session_id):
    try:
        sid = int(session_id)
    except ValueError:
        return jsonify({"error": "session id must be numeric"}), 400
    oc = _get_onchain()
    if oc:
        try:
            s = oc.get_session(sid)
            # Contract returns zero-struct for unknown sessions; map that to 404.
            if s.get("user") == "0x0000000000000000000000000000000000000000":
                return jsonify({"error": "session not found"}), 404
            return jsonify(s)
        except Exception as e:
            return jsonify({"error": str(e)}), 502
    if FACILITATOR_URL:
        try:
            import requests
            r = requests.get(f"{FACILITATOR_URL}/session/{session_id}", timeout=10)
            return (r.text, r.status_code, r.headers.items())
        except Exception as e:
            return jsonify({"error": str(e)}), 502
    return jsonify({"error": "no on-chain backend configured"}), 503


# On-chain deployment metadata for the frontend. Single source of truth is
# onchain.get_deployment(), which reads env overrides at call time so a new
# deployer only needs to export the relevant *_ADDRESS vars and restart.
@app.route("/api/onchain/info")
def api_onchain_info():
    from onchain import get_deployment
    return jsonify(get_deployment())


# /config.js - populates window.AGENTHIRE_CHAIN + window.AGENTHIRE_ADDRESSES
# from the active deployment so the frontend never carries hardcoded addresses.
# Loaded in base.html BEFORE static/js/contracts.js (which now only ships ABIs).
@app.route("/config.js")
def config_js():
    import json
    from flask import Response
    from onchain import get_deployment
    d = get_deployment()
    js = (
        "// Auto-generated from server env. Do not edit.\n"
        "window.AGENTHIRE_CHAIN = " + json.dumps({
            "chainId":    d["chainId"],
            "chainIdHex": d["chainIdHex"],
            "name":       d["chain"],
            "rpcUrl":     d["rpcUrl"],
            "explorer":   d["explorer"],
            "nativeCurrency": {"name": "AVAX", "symbol": "AVAX", "decimals": 18},
        }) + ";\n"
        "window.AGENTHIRE_ADDRESSES = " + json.dumps(d["contracts"]) + ";\n"
    )
    return Response(js, mimetype="application/javascript")


# ── REST Agent API ─────────────────────────────────────────────────────────────

@app.route("/api/agents")
def api_agents():
    """Paginated, filterable agent list."""
    category  = request.args.get("category", "")
    use_case  = request.args.get("use_case", "")
    verified  = request.args.get("verified", "")
    q         = request.args.get("q", "").lower()
    page      = max(1, int(request.args.get("page", 1)))
    per_page  = min(50, int(request.args.get("per_page", 12)))

    agents = AGENTS[:]
    if q:
        agents = [a for a in agents if q in a["name"].lower() or q in a["description"].lower()
                  or any(q in t for t in a["tags"])]
    if category:
        agents = [a for a in agents if a["category"] == category]
    if use_case:
        agents = [a for a in agents if a["use_case"] == use_case]
    if verified == "true":
        agents = [a for a in agents if a["verified"]]
    elif verified == "false":
        agents = [a for a in agents if not a["verified"]]

    total   = len(agents)
    start   = (page - 1) * per_page
    agents  = agents[start:start + per_page]
    return jsonify({"agents": agents, "total": total, "page": page, "per_page": per_page})


@app.route("/api/agents/<int:agent_id>")
def api_agent(agent_id):
    agent = next((a for a in AGENTS if a["id"] == agent_id), None)
    if not agent:
        return jsonify({"error": "agent not found"}), 404
    return jsonify(agent)


@app.route("/api/agents/register", methods=["POST"])
def api_agents_register():
    """Register a new agent on-chain via AgentRegistry.registerAgent."""
    payload = request.get_json(silent=True) or {}
    required = ["wallet", "name", "endpointURL"]
    missing = [k for k in required if k not in payload]
    if missing:
        return _api_error(f"missing fields: {missing}", field=missing[0])
    if not _is_valid_wallet(payload.get("wallet")):
        return _api_error("wallet must be a 0x-prefixed 40-hex address", field="wallet")
    if len(str(payload.get("name") or "").strip()) < 3:
        return _api_error("name must be at least 3 characters", field="name")

    oc = _get_onchain()
    if oc:
        try:
            result = oc.register_agent(payload["wallet"], payload["name"], payload["endpointURL"])
            return jsonify(result), 201
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    return jsonify({
        "agentId": None,
        "status": "mock_registered",
        "note": "No FACILITATOR_PRIVATE_KEY - registration not sent on-chain.",
    }), 201


# ── On-chain agent reads ────────────────────────────────────────────────────────

@app.route("/api/llm/status")
def api_llm_status():
    """Probe the Akash-hosted vLLM endpoint — does it respond, is model loaded."""
    from llm import health
    return jsonify(health())


@app.route("/api/agents/<int:agent_id>/generate", methods=["POST"])
def api_agent_generate(agent_id):
    """Have this agent respond via the Akash-hosted LLM (per-agent system prompt)."""
    from llm import generate as llm_generate
    from models import Agent as AgentModel
    agent = AgentModel.query.get_or_404(agent_id)
    body = request.get_json(silent=True) or {}
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        return jsonify({"error": "prompt required"}), 400
    # Augment the agent's bio with concrete pricing so Qwen can answer
    # cost/duration questions with real numbers instead of generic "it
    # depends on CPU/memory" filler.
    bio_parts = [getattr(agent, "description", "") or ""]
    if agent.billing == "per_token":
        bio_parts.append(
            f"Pricing: {float(agent.min_price):.4f}–{float(agent.max_price):.4f} USDC per token "
            f"(current rate: {float(agent.current_price):.4f} USDC/token). "
            f"Example: 1,000 tokens costs ${float(agent.current_price) * 1000:.2f} USDC."
        )
    else:  # per_minute
        bio_parts.append(
            f"Pricing: {float(agent.min_price):.4f}–{float(agent.max_price):.4f} USDC per minute "
            f"(current: {float(agent.current_price):.4f} USDC/min). "
            f"Example: 10 minutes costs ${float(agent.current_price) * 10:.2f} USDC."
        )
    bio_parts.append(
        f"On-chain reputation: tier T{getattr(agent, 'verification_tier', 'basic')}, "
        f"{agent.tasks_completed or 0} tasks settled, "
        f"rating {float(agent.rating or 0):.2f}/5."
    )
    enriched_bio = " ".join(bio_parts)
    try:
        out = llm_generate(prompt,
            agent_name=agent.name, agent_category=agent.category,
            agent_bio=enriched_bio,
            max_tokens=int(body.get("maxTokens", 400)),
            temperature=float(body.get("temperature", 0.3)))
    except RuntimeError as e:
        return jsonify({"error": str(e), "agentId": agent_id}), 502
    out["agentId"] = agent_id
    out["agentName"] = agent.name
    out["agentCategory"] = agent.category
    return jsonify(out)


def get_transactions(agent_id=None, kinds=None, limit=50, real_only=False) -> list:
    """Read the ChainTransaction audit log, newest first."""
    from models import ChainTransaction as CT
    q = CT.query
    if agent_id is not None:
        q = q.filter_by(agent_id=agent_id)
    if kinds:
        q = q.filter(CT.kind.in_(list(kinds)))
    if real_only:
        q = q.filter(CT.meta.like('%"real": true%'))
    return [tx.to_dict() for tx in q.order_by(CT.ts.desc()).limit(limit).all()]


@app.route("/api/agents/<int:agent_id>/transactions")
def api_agent_transactions(agent_id):
    try:
        limit = min(200, max(1, int(request.args.get("limit", 25))))
    except ValueError:
        limit = 25
    kinds_raw = request.args.get("kinds")
    kinds = [k for k in (kinds_raw.split(",") if kinds_raw else []) if k]
    real_only = request.args.get("real_only", "").lower() in ("1", "true", "yes")
    return jsonify({
        "agentId": agent_id,
        "realOnly": real_only,
        "transactions": get_transactions(agent_id=agent_id, kinds=kinds or None,
                                         limit=limit, real_only=real_only),
    })


@app.route("/api/transactions")
def api_transactions():
    """Global on-chain activity feed across all agents."""
    try:
        limit = min(200, max(1, int(request.args.get("limit", 50))))
    except ValueError:
        limit = 50
    kinds_raw = request.args.get("kinds")
    kinds = [k for k in (kinds_raw.split(",") if kinds_raw else []) if k]
    real_only = request.args.get("real_only", "").lower() in ("1", "true", "yes")
    return jsonify({
        "realOnly": real_only,
        "transactions": get_transactions(kinds=kinds or None, limit=limit, real_only=real_only),
    })


# ── Escrow session management ───────────────────────────────────────────────────

@app.route("/api/session/<session_id>/cancel", methods=["POST"])
def api_session_cancel(session_id):
    try:
        sid = int(session_id)
    except ValueError:
        return jsonify({"error": "session id must be numeric"}), 400

    oc = _get_onchain()
    if oc:
        try:
            return jsonify(oc.cancel_session(sid))
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    if FACILITATOR_URL:
        try:
            import requests as _req
            r = _req.post(f"{FACILITATOR_URL}/session/{session_id}/cancel", timeout=15)
            return (r.text, r.status_code, r.headers.items())
        except Exception as e:
            return jsonify({"error": str(e)}), 502
    return jsonify({"error": "no on-chain backend configured"}), 503


# ── Admin action endpoints ──────────────────────────────────────────────────────

def _verification_or_404(vrf_id):
    from models import VerificationEntry
    entry = db.session.get(VerificationEntry, vrf_id)
    if not entry:
        return None, _api_error("verification entry not found", 404, code="VERIFICATION_NOT_FOUND")
    return entry, None


def _set_verification_status(vrf_id, status, *, allowed_from=None):
    entry, err = _verification_or_404(vrf_id)
    if err:
        return err
    if allowed_from and entry.status not in allowed_from:
        return _api_error(f"cannot move from '{entry.status}' to '{status}'", code="INVALID_STATE")
    entry.status = status
    if status == "approved" and entry.agent_id:
        from models import Agent as AgentModel
        ag = db.session.get(AgentModel, entry.agent_id)
        if ag:
            ag.verified = True
            ag.verification_tier = entry.tier
    db.session.commit()
    log.info("Verification %s -> %s", vrf_id, status)
    return jsonify({"id": vrf_id, "status": status})


@app.route("/admin/verification-queue/<vrf_id>/approve", methods=["POST"])
@require_api_key
def admin_approve_verification(vrf_id):
    return _set_verification_status(vrf_id, "approved",
                                    allowed_from={"pending", "testing", "human_review"})


@app.route("/admin/verification-queue/<vrf_id>/reject", methods=["POST"])
@require_api_key
def admin_reject_verification(vrf_id):
    return _set_verification_status(vrf_id, "rejected",
                                    allowed_from={"pending", "testing", "human_review"})


@app.route("/admin/verification-queue/<vrf_id>/test-start", methods=["POST"])
@require_api_key
def admin_start_testing(vrf_id):
    return _set_verification_status(vrf_id, "testing", allowed_from={"pending"})


@app.route("/admin/verification-queue/<vrf_id>/escalate", methods=["POST"])
@require_api_key
def admin_escalate_verification(vrf_id):
    return _set_verification_status(vrf_id, "human_review", allowed_from={"pending", "testing"})


def _set_payout_status(pay_id, status, *, allowed_from):
    from models import Payout
    p = db.session.get(Payout, pay_id)
    if not p:
        return _api_error("payout not found", 404, code="PAYOUT_NOT_FOUND")
    if p.status not in allowed_from:
        return _api_error(f"payout is '{p.status}', cannot move to '{status}'", code="INVALID_STATE")
    p.status = status
    db.session.commit()
    log.info("Payout %s -> %s", pay_id, status)
    return jsonify({"id": pay_id, "status": status})


@app.route("/admin/payouts/<pay_id>/release", methods=["POST"])
@require_api_key
def admin_release_payout(pay_id):
    return _set_payout_status(pay_id, "released", allowed_from={"pending", "held"})


@app.route("/admin/payouts/<pay_id>/hold", methods=["POST"])
@require_api_key
def admin_hold_payout(pay_id):
    return _set_payout_status(pay_id, "held", allowed_from={"pending"})


@app.route("/admin/payouts/<pay_id>/refund", methods=["POST"])
@require_api_key
def admin_refund_payout(pay_id):
    return _set_payout_status(pay_id, "refunded", allowed_from={"pending", "held"})


@app.route("/admin/payouts/release-all", methods=["POST"])
@require_api_key
def admin_release_all_payouts():
    from models import Payout
    pending = Payout.query.filter_by(status="pending").all()
    released_ids = []
    for p in pending:
        p.status = "released"
        released_ids.append(p.id)
    db.session.commit()
    log.info("Bulk release: %d payouts", len(released_ids))
    return jsonify({"released": released_ids, "count": len(released_ids)})


def _report_or_404(rpt_id):
    from models import ModerationReport
    r = db.session.get(ModerationReport, rpt_id)
    if not r:
        return None, _api_error("report not found", 404, code="REPORT_NOT_FOUND")
    return r, None


@app.route("/admin/moderation/<rpt_id>/resolve", methods=["POST"])
@require_api_key
def admin_resolve_report(rpt_id):
    r, err = _report_or_404(rpt_id)
    if err:
        return err
    data = request.get_json(silent=True) or {}
    r.status = "resolved"
    notes = str(data.get("notes", "")).strip()
    if notes:
        r.notes = notes
    db.session.commit()
    log.info("Moderation report resolved: %s", rpt_id)
    return jsonify({"id": rpt_id, "status": "resolved"})


@app.route("/admin/moderation/<rpt_id>/investigate", methods=["POST"])
@require_api_key
def admin_investigate_report(rpt_id):
    r, err = _report_or_404(rpt_id)
    if err:
        return err
    r.status = "investigating"
    db.session.commit()
    log.info("Moderation report under investigation: %s", rpt_id)
    return jsonify({"id": rpt_id, "status": "investigating"})


@app.route("/admin/moderation/<rpt_id>/suspend", methods=["POST"])
@require_api_key
def admin_suspend_agent(rpt_id):
    from models import Agent as AgentModel
    r, err = _report_or_404(rpt_id)
    if err:
        return err
    r.status = "suspended"
    if r.agent_id:
        ag = db.session.get(AgentModel, r.agent_id)
        if ag:
            ag.verified = False
            ag.verification_tier = "suspended"
        for a in AGENTS:
            if a["id"] == r.agent_id:
                a["verified"] = False
                a["verification_tier"] = "suspended"
    db.session.commit()
    log.info("Agent suspended via moderation report: %s", rpt_id)
    return jsonify({"id": rpt_id, "status": "suspended", "agent": r.agent})


# ── Order management ─────────────────────────────────────────────────────────

def _find_order(order_id):
    """Locate an order in-memory first, then fall back to DB. Returns (mem_dict_or_None, db_row_or_None)."""
    from models import Order as OrderModel
    mem = next((o for o in ORDERS if o["id"] == order_id), None)
    row = OrderModel.query.get(order_id)
    return mem, row


@app.route("/api/orders/<order_id>/complete", methods=["POST"])
def api_order_complete(order_id):
    """Mark an order complete and release escrow. Updates status in DB and in-memory."""
    mem, row = _find_order(order_id)
    if not mem and not row:
        return jsonify({"error": "order not found"}), 404
    current = (mem or {}).get("status") or (row.status if row else "")
    if current not in ("in_escrow", "in_progress"):
        return jsonify({"error": f"order is already {current}"}), 400
    if mem:
        mem["status"] = "completed"
    if row:
        row.status = "completed"
        db.session.commit()
    log.info("Order %s marked complete", order_id)
    return jsonify({"orderId": order_id, "status": "completed",
                    "message": "Escrow released. Seller has been paid."})


@app.route("/api/orders/<order_id>/start", methods=["POST"])
def api_order_start(order_id):
    """Seller marks an escrowed order as 'in_progress' to begin execution."""
    mem, row = _find_order(order_id)
    if not mem and not row:
        return jsonify({"error": "order not found"}), 404
    current = (mem or {}).get("status") or (row.status if row else "")
    if current != "in_escrow":
        return jsonify({"error": f"order cannot start from state '{current}'"}), 400
    if mem:
        mem["status"] = "in_progress"
    if row:
        row.status = "in_progress"
        db.session.commit()
    log.info("Order %s started by seller", order_id)
    return jsonify({"orderId": order_id, "status": "in_progress"})


# ── Rating API ─────────────────────────────────────────────────────────────────

@app.route("/api/agents/<int:agent_id>/rate", methods=["POST"])
def api_rate_agent(agent_id):
    payload = request.get_json(silent=True) or {}
    rating = payload.get("rating")
    if not rating or not (1 <= int(rating) <= 5):
        return jsonify({"error": "rating must be between 1 and 5"}), 400
    agent = next((a for a in AGENTS if a["id"] == agent_id), None)
    if not agent:
        return jsonify({"error": "agent not found"}), 404
    # Weighted average: blend new rating in
    new_rating = round(
        (agent["rating"] * agent["reviews"] + int(rating)) / (agent["reviews"] + 1), 1
    )
    agent["rating"] = new_rating
    agent["reviews"] += 1
    # Persist the full review so it shows up on the agent detail page.
    try:
        from models import Review as ReviewModel
        user = payload.get("user") or "0xanon..."
        feedback = (payload.get("feedback") or "").strip()
        db.session.add(ReviewModel(
            agent_id=agent_id, user=user, rating=int(rating),
            comment=feedback, date=time.strftime("%Y-%m-%d"),
        ))
        db.session.commit()
    except Exception as e:
        log.warning("Could not persist review for agent %s: %s", agent_id, e)
    log.info("Agent %s rated %s (new avg %.1f, %d reviews)", agent_id, rating, new_rating, agent["reviews"])
    return jsonify({"agentId": agent_id, "rating": new_rating, "reviews": agent["reviews"]})


# ── Search API ──────────────────────────────────────────────────────────────────

@app.route("/api/search")
def api_search():
    q = request.args.get("q", "").lower().strip()
    if not q:
        return jsonify({"results": []})
    results = [
        {"id": a["id"], "name": a["name"], "category": a["category"],
         "description": a["description"], "rating": a["rating"],
         "verified": a["verified"], "billing": a["billing"],
         "current_price": a["current_price"]}
        for a in AGENTS
        if q in a["name"].lower() or q in a["description"].lower()
        or any(q in t for t in a["tags"])
    ]
    return jsonify({"results": results, "total": len(results), "query": q})


# ── Health / readiness ──────────────────────────────────────────────────────────

@app.route("/api/health")
def api_health():
    """Liveness probe - always returns 200 if the process is alive."""
    return jsonify({"status": "ok", "service": "agenthire", "ts": int(time.time())})


@app.route("/api/ready")
def api_ready():
    """Readiness probe - checks DB connectivity."""
    try:
        db.session.execute(db.text("SELECT 1"))
        return jsonify({"status": "ready", "db": "ok", "ts": int(time.time())})
    except Exception as e:
        log.error("Readiness check failed: %s", e)
        return jsonify({"status": "unavailable", "db": str(e)}), 503


# ── Error handlers ──────────────────────────────────────────────────────────────

@app.errorhandler(404)
def not_found(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": "not found"}), 404
    return render_template("404.html"), 404


@app.errorhandler(500)
def server_error(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": "internal server error"}), 500
    return render_template("500.html"), 500


# ── DB init + seed ─────────────────────────────────────────────────────────────────

def _sync_agents_from_db():
    """Rebuild the in-memory AGENTS list to match DB rows so marketplace,
    search, and detail views see the full seeded roster + any live additions."""
    from models import Agent as AgentModel
    rows = AgentModel.query.order_by(AgentModel.id).all()
    AGENTS.clear()
    token_io_anchor = {
        "Development": (0.90, 3.10),
        "Data & Analytics": (1.10, 4.20),
        "Content": (0.55, 2.10),
        "Finance": (2.50, 10.50),
        "Research": (1.40, 5.60),
        "Security": (1.80, 7.20),
        "Automation": (0.70, 2.80),
    }
    minute_anchor = {
        "Development": 0.09,
        "Data & Analytics": 0.11,
        "Content": 0.07,
        "Finance": 0.16,
        "Research": 0.10,
        "Security": 0.15,
        "Automation": 0.08,
    }
    for a in rows:
        # Every agent is served by Akash-hosted Qwen 2.5 — no per-category split.
        latest_provider, latest_model = "Akash", "Qwen 2.5 Coder 7B"
        # Deterministic jitter so prices vary naturally agent-to-agent.
        jitter = ((a.id * 37) % 19 - 9) / 100.0  # [-0.09, +0.09]
        quality = max(0.85, min(1.35, (a.rating or 4.2) / 4.4))
        featured_boost = 1.08 if a.featured else 1.0
        verified_boost = 1.03 if a.verified else 0.97
        profile_mult = (1.0 + jitter) * quality * featured_boost * verified_boost

        input_per_1m = int(a.input_price_per_1m or 0)
        output_per_1m = int(a.output_price_per_1m or 0)
        current_price = float(a.current_price or 0)
        min_price = float(a.min_price or 0)
        max_price = float(a.max_price or 0)

        if a.billing == "per_token":
            base_in, base_out = token_io_anchor.get(a.category, (0.80, 3.20))
            believable_in = max(0.25, round(base_in * profile_mult, 2))
            believable_out = max(believable_in * 1.8, round(base_out * profile_mult, 2))

            if input_per_1m <= 0:
                input_per_1m = int(believable_in * 1_000_000)
            if output_per_1m <= 0:
                output_per_1m = int(believable_out * 1_000_000)

            io_in = input_per_1m / 1_000_000
            io_out = output_per_1m / 1_000_000
            io_mid_per_token = ((io_in + io_out) * 0.5) / 1_000_000

            if current_price <= 0:
                current_price = io_mid_per_token * (0.94 + (jitter * 0.35))

            if min_price <= 0:
                min_price = max(io_in * 0.75 / 1_000_000, current_price * 0.75)
            if max_price <= 0:
                max_price = max(io_out * 1.05 / 1_000_000, current_price * 1.25)

            if current_price < min_price:
                current_price = min_price * 1.02
            if current_price > max_price:
                current_price = max_price * 0.98
            # Prevent visually fake-zero token prices in UI.
            current_price = max(current_price, 0.000001)
            min_price = min(min_price, current_price)
            max_price = max(max_price, current_price)

        else:  # per_minute
            base_minute = minute_anchor.get(a.category, 0.09)
            believable_minute = max(0.03, round(base_minute * profile_mult, 2))
            if current_price <= 0:
                current_price = believable_minute

            if min_price <= 0:
                min_price = max(0.02, round(current_price * 0.7, 2))
            if max_price <= 0:
                max_price = round(current_price * 1.35, 2)

            if current_price < min_price:
                current_price = min_price
            if current_price > max_price:
                current_price = max_price
            # Keep minute prices in a believable range for marketplace display.
            current_price = max(current_price, 0.03)
            min_price = min(min_price, current_price)
            max_price = max(max_price, current_price)

            # Per-minute agents still expose token economics for comparison.
            if input_per_1m <= 0 or output_per_1m <= 0:
                io_in = max(0.35, round(current_price * 18.0, 2))
                io_out = max(io_in * 1.8, round(current_price * 54.0, 2))
                if input_per_1m <= 0:
                    input_per_1m = int(io_in * 1_000_000)
                if output_per_1m <= 0:
                    output_per_1m = int(io_out * 1_000_000)

        AGENTS.append({
            "id": a.id, "name": a.name, "description": a.description,
            "long_description": a.long_description, "category": a.category,
            "use_case": a.use_case, "verified": a.verified,
            "verification_tier": a.verification_tier, "featured": a.featured,
            "rating": a.rating, "reviews": a.reviews, "billing": a.billing,
            "min_price": min_price, "max_price": max_price,
            "current_price": current_price, "seller": a.seller,
            "seller_rating": a.seller_rating, "tasks_completed": a.tasks_completed,
            "avg_completion_time": a.avg_completion_time,
            # Force latest model attribution on read, regardless of stale DB rows.
            "model_provider": latest_provider,
            "model_name": latest_model,
            "deployer_wallet": a.deployer_wallet,
            "input_price_per_1m": input_per_1m,
            "output_price_per_1m": output_per_1m,
            "tags": a.tags, "capabilities": a.capabilities,
        })


with app.app_context():
    try:
        # 1. Baseline schema (idempotent). Models must be imported first so
        #    their tables are registered on the metadata.
        import models  # noqa: F401
        db.create_all()
        # 2. Additive column migrations (SQLite can't add columns via create_all)
        from models import _ensure_columns
        _ensure_columns(app)
        # 3. Fixtures
        from models import seed_db
        seed_db(app)
        _sync_agents_from_db()
        log.info("Database ready — %d agents in memory.", len(AGENTS))
    except Exception as _seed_err:
        log.warning("DB seed skipped: %s", _seed_err)


if __name__ == "__main__":
    import sys

    # Default: 5000 on Windows/Linux (matches `flask run` and common bookmarks).
    # On macOS, AirPlay Receiver often binds 5000 — use 8080 unless PORT is set.
    default_port = 8080 if sys.platform == "darwin" else 5000
    port = int(os.environ.get("PORT", default_port))
    print(f"\n  AgentHire -> http://127.0.0.1:{port}/\n", flush=True)
    # Debug only in development so Werkzeug debugger / tracebacks don't leak
    # if someone ever runs this in a shared or exposed environment.
    debug_mode = os.environ.get("FLASK_ENV", "development") == "development"
    app.run(debug=debug_mode, host="127.0.0.1", port=port)
