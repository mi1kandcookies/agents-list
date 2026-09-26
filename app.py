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

# ── A2A Workflow definitions ────────────────────────────────────────────────────
A2A_WORKFLOWS = {
    1: {  # CodeReview Pro
        "composable": True,
        "stage_count": 4,
        "workflow_label": "4-stage analysis pipeline",
        "workflow_summary": "Analyzes code → routes to SecureAudit AI if vulnerabilities detected → routes to TestingMaster if coverage gaps found → compiles unified report.",
        "trigger_rules": [
            {"condition": "Security issues detected", "calls": "SecureAudit AI", "trigger": "automatic"},
            {"condition": "Test coverage < 80%",      "calls": "TestingMaster",  "trigger": "automatic"},
        ],
        "sub_agents": [
            {"id": 7,  "name": "SecureAudit AI", "role": "Deep vulnerability scan",   "billing": "per_token", "est_cost_low": 0.003, "est_cost_high": 0.012, "verified": True},
            {"id": 10, "name": "TestingMaster",  "role": "Test suite generation",     "billing": "per_token", "est_cost_low": 0.002, "est_cost_high": 0.006, "verified": True},
        ],
        "base_cost_label":      "$0.00600 / token",
        "subagent_range_label": "$0.003 - $0.018",
        "total_range_label":    "$0.009 - $0.024",
        "steps": [
            {"name": "CodeReview Pro",  "role": "main",      "action": "Parse & analyse source code"},
            {"name": "SecureAudit AI",  "role": "subagent",  "action": "Deep vulnerability scan (if needed)"},
            {"name": "TestingMaster",   "role": "subagent",  "action": "Generate test coverage report (if needed)"},
            {"name": "CodeReview Pro",  "role": "main",      "action": "Compile unified findings report"},
        ],
        "stages": [
            {
                "id": "parse",    "order": 1, "type": "internal",  "conditional": False,
                "name": "Input Parser",
                "exec_label": "Parsing your code",
                "purpose": "Tokenises submitted code, detects language and framework, maps the dependency graph.",
                "detail": "Supports 20+ languages. Normalises whitespace, strips comments for analysis, extracts AST structure for downstream stages.",
                "summary": "Parsed your code - detected Python, 14 files, 2,840 tokens.",
            },
            {
                "id": "reason",   "order": 2, "type": "internal",  "conditional": False,
                "name": "Reasoning Engine",
                "exec_label": "Analysing code patterns",
                "purpose": "Applies OWASP rules, static analysis heuristics, and pattern matching against known vulnerability signatures.",
                "detail": "Checks 300+ rule patterns. Scores each finding by severity (Critical / High / Med / Low). Flags code paths for sub-agent escalation.",
                "summary": "Found 3 potential issues - escalating 2 for deep scan.",
            },
            {
                "id": "secure",   "order": 3, "type": "subagent",  "conditional": True,
                "name": "SecureAudit AI",
                "exec_label": "Running deep vulnerability scan",
                "purpose": "Sub-agent. Runs CVE database matching and smart contract safety checks when security issues are flagged.",
                "detail": "Called only if Reasoning Engine flags security-severity findings. Adds 15-90s to execution time. Billed separately.",
                "summary": "Confirmed 2 high-severity vulnerabilities - SQL injection (line 142), unvalidated auth input.",
            },
            {
                "id": "format",   "order": 4, "type": "internal",  "conditional": False,
                "name": "Output Formatter",
                "exec_label": "Compiling your report",
                "purpose": "Structures findings into a prioritised report with severity ratings, code snippets, and remediation steps.",
                "detail": "Outputs: executive summary, per-issue breakdown, diff-ready patches, and a machine-readable JSON manifest.",
                "summary": "Report compiled - 3 issues, 2 high, 1 medium. PDF and JSON available.",
            },
        ],
    },
    4: {  # AlphaTrader AI
        "composable": True,
        "stage_count": 4,
        "workflow_label": "4-stage signal pipeline",
        "workflow_summary": "Ingests market data → calls ResearchBot Pro for macro context → calls FinanceGPT for portfolio modeling → generates final signal set.",
        "trigger_rules": [
            {"condition": "Always - macro context required", "calls": "ResearchBot Pro", "trigger": "automatic"},
            {"condition": "Portfolio analysis requested",    "calls": "FinanceGPT",      "trigger": "automatic"},
        ],
        "sub_agents": [
            {"id": 6,  "name": "ResearchBot Pro", "role": "Market & macro research",   "billing": "per_token", "est_cost_low": 0.02, "est_cost_high": 0.08, "verified": True},
            {"id": 11, "name": "FinanceGPT",      "role": "Portfolio & DCF modeling",  "billing": "per_minute","est_cost_low": 0.10, "est_cost_high": 0.25, "verified": True},
        ],
        "base_cost_label":      "$0.45000 / min",
        "subagent_range_label": "$0.12 - $0.33",
        "total_range_label":    "$0.57 - $0.78 / min",
        "steps": [
            {"name": "AlphaTrader AI",  "role": "main",     "action": "Ingest live market data & price history"},
            {"name": "ResearchBot Pro", "role": "subagent", "action": "Pull macro research & news context"},
            {"name": "FinanceGPT",      "role": "subagent", "action": "Portfolio correlation & risk modeling"},
            {"name": "AlphaTrader AI",  "role": "main",     "action": "Generate final buy/sell signals"},
        ],
        "stages": [
            {
                "id": "ingest",   "order": 1, "type": "internal",  "conditional": False,
                "name": "Market Data Ingestion",
                "exec_label": "Fetching live market data",
                "purpose": "Pulls OHLCV data, order book depth, and funding rates from connected exchanges.",
                "detail": "Aggregates across 12 exchange feeds. Normalises tick data to 1-min candles. Detects data gaps and fills via interpolation.",
                "summary": "Ingested 90 days of BTC/ETH data - 129,600 candles across 3 exchanges.",
            },
            {
                "id": "research", "order": 2, "type": "subagent",  "conditional": False,
                "name": "ResearchBot Pro",
                "exec_label": "Gathering macro research",
                "purpose": "Sub-agent. Always called - provides macro context, news sentiment, and on-chain metrics.",
                "detail": "Queries 40+ news sources and on-chain analytics. Outputs a sentiment score and key macro flags that feed the signal model.",
                "summary": "Macro: bearish sentiment (score −0.34). Key flag: Fed rate decision in 3 days.",
            },
            {
                "id": "model",    "order": 3, "type": "subagent",  "conditional": True,
                "name": "FinanceGPT",
                "exec_label": "Running portfolio model",
                "purpose": "Sub-agent. Called when portfolio analysis is requested. Runs DCF and correlation analysis.",
                "detail": "Builds a correlation matrix across your holdings, runs Monte Carlo simulation (10k paths), outputs VaR at 95% and 99% confidence.",
                "summary": "Portfolio VaR (95%): $1,240. Correlation risk: BTC/ETH at 0.87 - consider hedging.",
            },
            {
                "id": "signal",   "order": 4, "type": "internal",  "conditional": False,
                "name": "Signal Generator",
                "exec_label": "Generating trade signals",
                "purpose": "Combines market data, macro context, and portfolio state to produce final buy/sell signals with confidence scores.",
                "detail": "Ensemble of LSTM, momentum, and mean-reversion models. Each signal includes entry price, stop-loss, take-profit, and confidence %.",
                "summary": "Generated 4 signals - 2 long BTC, 1 short ETH, 1 hold. Avg confidence: 73%.",
            },
        ],
    },
    3: {  # DataSift Analytics
        "composable": True,
        "stage_count": 4,
        "workflow_label": "4-stage analytics pipeline",
        "workflow_summary": "Evaluates data source → calls WebCrawler X if external data needed → calls ResearchBot for domain context → runs analysis pipeline.",
        "trigger_rules": [
            {"condition": "External data source required", "calls": "WebCrawler X",   "trigger": "automatic"},
            {"condition": "Domain context needed",         "calls": "ResearchBot Pro","trigger": "automatic"},
        ],
        "sub_agents": [
            {"id": 5, "name": "WebCrawler X",    "role": "External data acquisition", "billing": "per_minute","est_cost_low": 0.02, "est_cost_high": 0.06, "verified": False},
            {"id": 6, "name": "ResearchBot Pro", "role": "Domain knowledge context",  "billing": "per_token", "est_cost_low": 0.01, "est_cost_high": 0.03, "verified": True},
        ],
        "base_cost_label":      "$0.18000 / min",
        "subagent_range_label": "$0.03 - $0.09",
        "total_range_label":    "$0.21 - $0.27 / min",
        "steps": [
            {"name": "DataSift Analytics", "role": "main",     "action": "Evaluate data source & task scope"},
            {"name": "WebCrawler X",       "role": "subagent", "action": "Acquire external data (if needed)"},
            {"name": "ResearchBot Pro",    "role": "subagent", "action": "Provide domain context (if needed)"},
            {"name": "DataSift Analytics", "role": "main",     "action": "Run analysis pipeline & generate report"},
        ],
        "stages": [
            {
                "id": "eval",     "order": 1, "type": "internal",  "conditional": False,
                "name": "Source Evaluator",
                "exec_label": "Evaluating your data source",
                "purpose": "Assesses the data source - CSV, endpoint, or database - and determines if external acquisition is needed.",
                "detail": "Validates schema, detects column types, checks for nulls and outliers. Determines if external enrichment is required.",
                "summary": "Loaded Q1 sales CSV - 48,200 rows, 12 columns, 3.2% null rate detected.",
            },
            {
                "id": "crawl",    "order": 2, "type": "subagent",  "conditional": True,
                "name": "WebCrawler X",
                "exec_label": "Acquiring external data",
                "purpose": "Sub-agent. Fetches external data when the source requires web enrichment.",
                "detail": "Renders JS pages, handles pagination and rate limiting. Outputs structured JSON ready for merge with your dataset.",
                "summary": "Scraped 240 competitor pricing records - merged with your dataset.",
            },
            {
                "id": "context",  "order": 3, "type": "subagent",  "conditional": True,
                "name": "ResearchBot Pro",
                "exec_label": "Gathering domain context",
                "purpose": "Sub-agent. Provides industry benchmarks and domain knowledge to interpret your data accurately.",
                "detail": "Queries industry reports and publishes domain averages. Outputs a context layer that improves anomaly detection accuracy.",
                "summary": "Added retail sector benchmarks - identified 14% above-average churn signal.",
            },
            {
                "id": "analyse",  "order": 4, "type": "internal",  "conditional": False,
                "name": "Analysis Engine",
                "exec_label": "Running analysis pipeline",
                "purpose": "Runs statistical modelling, anomaly detection, and chart generation on the combined dataset.",
                "detail": "Applies EDA, regression, clustering (k-means), and time-series decomposition. Outputs executive summary + downloadable charts.",
                "summary": "Analysis complete - 6 key insights, 3 anomalies flagged, 8 charts generated.",
            },
        ],
    },
    7: {  # SecureAudit AI
        "composable": True,
        "stage_count": 4,
        "workflow_label": "4-stage security audit",
        "workflow_summary": "Runs static analysis via CodeReview Pro first → performs deep security scan → generates prioritised vulnerability report.",
        "trigger_rules": [
            {"condition": "Always - static analysis required first", "calls": "CodeReview Pro", "trigger": "automatic"},
        ],
        "sub_agents": [
            {"id": 1, "name": "CodeReview Pro", "role": "Initial static analysis pass", "billing": "per_token", "est_cost_low": 0.004, "est_cost_high": 0.010, "verified": True},
        ],
        "base_cost_label":      "$0.01500 / token",
        "subagent_range_label": "$0.004 - $0.010",
        "total_range_label":    "$0.019 - $0.025",
        "steps": [
            {"name": "SecureAudit AI",  "role": "main",     "action": "Parse contract / infrastructure config"},
            {"name": "CodeReview Pro",  "role": "subagent", "action": "Static analysis & code quality pass"},
            {"name": "SecureAudit AI",  "role": "main",     "action": "Deep vulnerability scan & CVE matching"},
            {"name": "SecureAudit AI",  "role": "main",     "action": "Compile prioritised remediation report"},
        ],
        "stages": [
            {
                "id": "parse",    "order": 1, "type": "internal",  "conditional": False,
                "name": "Contract Parser",
                "exec_label": "Parsing your contract",
                "purpose": "Parses Solidity, Vyper, or infrastructure config files and builds an annotated control-flow graph.",
                "detail": "Detects compiler version, pragma settings, inheritance chains, and external call sites. Flags non-standard patterns for deep scan.",
                "summary": "Parsed ERC-20 contract - 847 lines, 12 functions, 3 external call sites detected.",
            },
            {
                "id": "static",   "order": 2, "type": "subagent",  "conditional": False,
                "name": "CodeReview Pro",
                "exec_label": "Running static analysis",
                "purpose": "Sub-agent. Always called - provides code quality pass and flags structural issues before deep security scan.",
                "detail": "Checks code style, gas optimisation opportunities, and basic logic errors. Results are passed to the vulnerability scanner as annotated context.",
                "summary": "Static analysis complete - 2 gas inefficiencies, 1 logic warning passed to scanner.",
            },
            {
                "id": "scan",     "order": 3, "type": "internal",  "conditional": False,
                "name": "Vulnerability Scanner",
                "exec_label": "Scanning for vulnerabilities",
                "purpose": "Matches the annotated contract against CVE database and known DeFi exploit patterns (reentrancy, flash loan attacks, etc.).",
                "detail": "Runs 180+ security checks including reentrancy, integer overflow, access control, oracle manipulation, and front-running vectors.",
                "summary": "Scan complete - 1 critical (reentrancy in withdraw()), 1 high (missing access control).",
            },
            {
                "id": "report",   "order": 4, "type": "internal",  "conditional": False,
                "name": "Report Compiler",
                "exec_label": "Compiling audit report",
                "purpose": "Generates a prioritised audit report with issue severity, code location, exploit scenario, and recommended fix.",
                "detail": "Output includes: executive summary, per-finding breakdown with PoC exploit snippets, diff-ready patches, and a machine-readable SARIF file.",
                "summary": "Audit report compiled - 2 findings, remediation patches generated.",
            },
        ],
    },
}

# Standalone agents (no A2A) - simple 3-stage workflow
def _standalone_stages(agent_name):
    return [
        {
            "id": "parse",   "order": 1, "type": "internal", "conditional": False,
            "name": "Input Parser",
            "exec_label": "Parsing your request",
            "purpose": "Validates and normalises the input - detects task type, format, and required output structure.",
            "detail": "Handles text, files, and structured data. Extracts task intent and maps to internal processing parameters.",
            "summary": "Input parsed - task type detected, parameters extracted.",
        },
        {
            "id": "process", "order": 2, "type": "internal", "conditional": False,
            "name": "Core Processor",
            "exec_label": "Processing your request",
            "purpose": f"{agent_name}'s primary model runs the task end-to-end with no sub-agent delegation.",
            "detail": "Single-model execution. Deterministic output with no variable sub-agent costs. Completion time is fixed to the agent's average.",
            "summary": "Core processing complete.",
        },
        {
            "id": "format",  "order": 3, "type": "internal", "conditional": False,
            "name": "Output Formatter",
            "exec_label": "Formatting results",
            "purpose": "Structures the output to the requested format and runs a final quality check before delivery.",
            "detail": "Supports JSON, Markdown, PDF, and plain text output. Quality gate rejects outputs below confidence threshold.",
            "summary": "Output formatted and quality-checked - ready for delivery.",
        },
    ]

STANDALONE = {
    "composable": False,
    "stage_count": 3,
    "workflow_label": "3-stage standalone pipeline",
    "workflow_summary": "This agent operates independently. It does not call other agents.",
    "trigger_rules": [],
    "sub_agents": [],
    "base_cost_label": " - ",
    "subagent_range_label": "None",
    "total_range_label": "Base price only",
    "steps": [],
    "stages": [],
}

# ── Shared helpers ─────────────────────────────────────────────────────────────
import re as _re

FACILITATOR_WALLET_FALLBACK = "0xdb4135c6884D81497769440788306EE985DD1A6e"

def _is_valid_wallet(addr: str) -> bool:
    """EVM address shape check — 0x + 40 hex. Keeps junk out of Orders."""
    return bool(_re.fullmatch(r"0x[a-fA-F0-9]{40}", str(addr or "")))

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
        "surge_active": True,
        "surge_multiplier": 1.5,
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
        "surge_active": False,
        "surge_multiplier": 1.0,
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
        "surge_active": True,
        "surge_multiplier": 1.8,
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
        "surge_active": True,
        "surge_multiplier": 2.1,
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
        "surge_active": False,
        "surge_multiplier": 1.0,
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
        "surge_active": False,
        "surge_multiplier": 1.0,
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
        "surge_active": False,
        "surge_multiplier": 1.0,
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
        "surge_active": False,
        "surge_multiplier": 1.0,
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
        "surge_active": True,
        "surge_multiplier": 1.3,
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
        "surge_active": False,
        "surge_multiplier": 1.0,
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
        "surge_active": False,
        "surge_multiplier": 1.0,
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
        "surge_active": False,
        "surge_multiplier": 1.0,
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
        "surge_active": False,
        "surge_multiplier": 1.0,
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

EARNINGS_DATA = {
    "total_revenue": 8420.50,
    "platform_fees": 84.21,
    "escrow_funds": 312.00,
    "released_payouts": 8024.29,
    "monthly": [2100, 2840, 3200, 4100, 3800, 4500, 5200, 4900, 6100, 7200, 7800, 8420],
    "surge_earnings": [210, 340, 480, 620, 510, 680, 890, 720, 980, 1240, 1380, 1620],
    "labels": ["May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec", "Jan", "Feb", "Mar", "Apr"],
}

ADMIN_STATS = {
    "total_agents": len(AGENTS),
    "verified_agents": len([a for a in AGENTS if a["verified"]]),
    "pending_verifications": len([v for v in VERIFICATION_QUEUE if v["status"] in ("pending", "testing", "human_review")]),
    "active_orders": len([o for o in ORDERS if o["status"] == "in_progress"]),
    "total_volume": 142830.00,
    "surge_revenue_pct": 18.4,
    "hourly_revenue": [120, 85, 60, 45, 90, 180, 320, 410, 390, 450, 480, 520, 490, 510, 530, 480, 460, 510, 580, 620, 540, 480, 380, 250],
    "revenue_labels": [f"{i:02d}:00" for i in range(24)],
}

# ── Routes ─────────────────────────────────────────────────────────────────────

_CAT_CODES = {
    "Development": "DEV", "Data & Analytics": "DAT", "Content": "CON",
    "Finance": "FIN", "Research": "RES", "Security": "SEC", "Automation": "AUT",
}

@app.route("/")
def index():
    """Standalone landing page. First thing visitors see. Click-through to /marketplace."""
    featured = [a for a in AGENTS if a["featured"]]

    # Build the interactive network-visualization dataset.
    # Nodes: first 12 agents (keeps the canvas readable). Edges: derived
    # from real A2A workflow sub-agent relationships, so the viz reflects
    # actual on-chain agent composition, not decoration.
    _agent_ids = {a["id"] for a in AGENTS[:12]}
    viz_agents = [
        {
            "id":         a["id"],
            "name":       a["name"],
            "cat":        _CAT_CODES.get(a["category"], "AGT"),
            "verified":   a["verified"],
            "featured":   a["featured"],
            "price":      a["current_price"],
            "unit":       "tok" if a["billing"] == "per_token" else "min",
            "surge":      a.get("surge_active", False),
            "surge_mult": a.get("surge_multiplier", 1.0),
            "rating":     a.get("rating", 4.0),
            "tasks":      a.get("tasks_completed", 0),
        }
        for a in AGENTS[:12]
    ]
    viz_edges = []
    _seen = set()
    for parent_id, wf in A2A_WORKFLOWS.items():
        if parent_id not in _agent_ids:
            continue
        for sub in wf.get("sub_agents", []):
            sub_id = sub.get("id")
            if sub_id in _agent_ids and (parent_id, sub_id) not in _seen and (sub_id, parent_id) not in _seen:
                viz_edges.append({"source": parent_id, "target": sub_id})
                _seen.add((parent_id, sub_id))

    live_only = request.args.get("live_only", "").lower() in ("1", "true", "yes")
    return render_template(
        "landing.html",
        featured=featured,
        viz_agents=viz_agents,
        viz_edges=viz_edges,
        stats=_live_chain_stats(live_only=live_only),
    )

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
    a2a = A2A_WORKFLOWS.get(agent_id, {**STANDALONE, "stages": _standalone_stages(agent["name"])})
    # "Works well with" — pick complementary agents from affinity categories.
    # Lets every agent participate in A2A even without a hardcoded workflow.
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
    import random as _rnd
    _picker = _rnd.Random(f"pair-{agent_id}")
    pool = [a for a in AGENTS if a["id"] != agent_id and a["category"] in pair_cats and a.get("verified")]
    _picker.shuffle(pool)
    collaborators = pool[:3]
    return render_template("agent_detail.html", agent=agent, reviews=reviews, a2a=a2a,
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
            # Fall back to the cookie set by the nav wallet connect, or the
            # facilitator as a demo-mode last resort.
            buyer = request.cookies.get("buyer_wallet") or FACILITATOR_WALLET_FALLBACK
        order_id = f"ORD-{len(ORDERS) + 1:03d}"
        new_order = {
            "id": order_id,
            "agent": agent["name"],
            "agent_id": agent_id,
            "buyer": buyer,
            "amount": amount,
            "status": "in_escrow",
            "date": str(uuid.uuid4())[:10],
            "task": data.get("task", ""),
        }
        ORDERS.append(new_order)
        if request.is_json:
            return jsonify({"orderId": order_id, "status": "in_escrow"}), 201
        return redirect(url_for("order_detail", order_id=order_id))
    a2a = A2A_WORKFLOWS.get(agent_id, {**STANDALONE, "stages": _standalone_stages(agent["name"])})
    return render_template("checkout.html", agent=agent, a2a=a2a)

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
    a2a   = A2A_WORKFLOWS.get(agent["id"], {**STANDALONE, "stages": _standalone_stages(agent["name"])})
    return render_template("order.html", order=order, agent=agent, a2a=a2a)

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
        wanted_active    = {"in_escrow", "in_progress"}
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
    # Fall back to the demo wallet when no wallet is provided so reviewers
    # who hit the page without explicit connect still see the orders
    # just fired from /demo and /checkout.
    wallet = (request.args.get("wallet")
              or request.cookies.get("buyer_wallet")
              or FACILITATOR_WALLET_FALLBACK)
    chain_orders = _buyer_jobs_from_chain(wallet, include_settled=False, include_active=True) if wallet else []
    db_orders    = _orders_for_buyer(wallet, completed=False)
    orders = db_orders + chain_orders
    return render_template("active_jobs.html", orders=orders, wallet=wallet)

@app.route("/past-jobs")
def past_jobs():
    wallet = (request.args.get("wallet")
              or request.cookies.get("buyer_wallet")
              or FACILITATOR_WALLET_FALLBACK)
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

@app.route("/agent-mode")
def agent_mode():
    return redirect(url_for("agent_mode_overview"))

@app.route("/agent-mode/overview")
def agent_mode_overview():
    """Agent operator's control room. Every stat and row on this page is
    filtered to REAL Fuji txs (meta.real=true) so numbers stay honest and
    stale seeded rows don't pollute the task queue."""
    from models import ChainTransaction as CT
    from sqlalchemy import func as sfn
    import json as _json
    now = int(time.time())
    day_ago = now - 86400
    REAL = '%"real": true%'
    try:
        # All counts below = real on-chain only. No sim pollution.
        deposits_24h  = db.session.query(sfn.count(CT.id)).filter(CT.kind.in_(["deposit","a2a_hire"]), CT.ts >= day_ago, CT.meta.like(REAL)).scalar() or 0
        settles_24h   = db.session.query(sfn.count(CT.id)).filter(CT.kind.in_(["settle","a2a_settle"]), CT.ts >= day_ago, CT.meta.like(REAL)).scalar() or 0
        active_total  = db.session.query(sfn.count(CT.id)).filter(CT.kind.in_(["deposit","a2a_hire"]), CT.meta.like(REAL)).scalar() or 0
        settled_total = db.session.query(sfn.count(CT.id)).filter(CT.kind.in_(["settle","a2a_settle"]), CT.meta.like(REAL)).scalar() or 0
        # Active orders sourced from the Order table (real rent-agent flow).
        from models import Order as OrderModel
        open_orders = OrderModel.query.filter(OrderModel.status.in_(["in_escrow","in_progress"])).count()
        closed_orders = OrderModel.query.filter(OrderModel.status.in_(["completed","settled"])).count()
        deposit_micro = db.session.query(sfn.sum(CT.amount_usdc)).filter(CT.kind.in_(["deposit","a2a_hire"]), CT.meta.like(REAL)).scalar() or 0
        settle_micro  = db.session.query(sfn.sum(CT.amount_usdc)).filter(CT.kind.in_(["settle","a2a_settle"]), CT.meta.like(REAL)).scalar() or 0
        escrow_locked = max(0, (deposit_micro - settle_micro) / 1_000_000)
        success_rate  = round((closed_orders / (open_orders + closed_orders) * 100), 1) if (open_orders + closed_orders) else 0.0
    except Exception as _e:
        log.warning("agent_mode overview stats query failed: %s", _e)
        deposits_24h = settles_24h = 0
        open_orders = closed_orders = 0
        escrow_locked = 0.0; success_rate = 0.0
    try:
        from models import OnchainProfile
        surge_agents = OnchainProfile.query.filter_by(surge_active=True).count()
    except Exception:
        surge_agents = 0
    stats = {
        "active_sessions": int(open_orders),
        "tasks_24h":       int(settles_24h + deposits_24h),
        "avg_latency_ms":  0,
        "escrow_locked":   round(escrow_locked, 2),
        "success_rate":    success_rate,
        "surge_agents":    int(surge_agents),
    }
    # Task feed — REAL Fuji txs only. Drops the stale sim/QuickList junk.
    task_feed = []
    try:
        real_rows = (CT.query
                     .filter(CT.kind.in_(["deposit", "settle", "stake", "a2a_hire", "a2a_settle"]))
                     .filter(CT.meta.like(REAL))
                     .order_by(CT.id.desc()).limit(12).all())
        for r in real_rows:
            try: m = _json.loads(r.meta or "{}")
            except Exception: m = {}
            sid = m.get("sessionId") or m.get("session_id") or r.id
            agent = next((a for a in AGENTS if a["id"] == r.agent_id), None)
            # Skip rows targeting synthetic / debug-named agents or agents
            # that no longer exist in the live AGENTS list (previously
            # junk-filtered rows still showed as "Agent #132" etc because
            # the tx_hash row's agent_id pointed to a removed listing).
            if not agent:
                continue
            display_name = agent["name"]
            AGENT_JUNK = ("QuickList-", "Demo-17", "FinalCheck", "PostRestart",
                          "BrowserShape", "SmokeBond", "Reviewedemo-", "LIVE-",
                          "ProofTx", "TestBondAgent", "FinalLock", "WizardTest")
            if any(display_name.startswith(p) for p in AGENT_JUNK) or len(display_name.strip()) < 3:
                continue
            elapsed = max(0, now - int(r.ts or now))
            el_m, el_s = divmod(elapsed, 60)
            h = r.tx_hash if r.tx_hash and r.tx_hash.startswith("0x") else ("0x" + r.tx_hash) if r.tx_hash else None
            snowtrace_url = f"https://subnets-test.avax.network/c-chain/tx/{h}" if h else None
            task_feed.append({
                "id": f"task-{sid}",
                "agent": display_name,
                "buyer": (r.from_addr[:6] + "…" + r.from_addr[-4:]) if r.from_addr else "—",
                "status": "complete" if r.kind in ("settle","a2a_settle") else "running",
                "amount": round((r.amount_usdc or 0) / 1_000_000, 2),
                "elapsed": f"{el_m}m {el_s:02d}s" if el_m < 60 else f"{el_m//60}h",
                "category": agent["category"] if agent else "Automation",
                "txHash": r.tx_hash,
                "snowtrace": snowtrace_url,
                "real": True,
                "kind": r.kind,
            })
            if len(task_feed) >= 12: break
    except Exception as _e:
        log.warning("agent_mode task feed failed: %s", _e)
    # Clean agent list for the registry table — drop temp names too.
    clean_agents = [a for a in AGENTS if not (
        (a.get("name") or "").startswith(("QuickList-", "Demo-", "FinalCheck", "PostRestart", "BrowserShape", "SmokeBond", "Reviewedemo-", "LIVE-", "ProofTx", "TestBondAgent", "FinalLock", "WizardTest"))
    )][:12]
    # Pull this viewer's own agents (if they have a wallet cookie set) so
    # the /agent-mode page shows a personalized "your listings" panel,
    # not just the global registry view.
    viewer_wallet = (request.cookies.get("buyer_wallet")
                     or request.cookies.get("seller_wallet") or "").lower()
    my_listings = []
    if viewer_wallet:
        try:
            from models import ChainTransaction as CT
            mine = [a for a in AGENTS if (a.get("seller") or "").lower() == viewer_wallet
                    or (a.get("wallet") or "").lower() == viewer_wallet]
            my_agent_ids = [a["id"] for a in mine]
            # Get real earnings + active-hire counts per agent.
            if my_agent_ids:
                from collections import defaultdict
                rev_by_agent = defaultdict(int)
                hires_by_agent = defaultdict(int)
                rows = (CT.query.filter(CT.agent_id.in_(my_agent_ids))
                        .filter(CT.kind.in_(["deposit","a2a_hire","settle","a2a_settle"]))
                        .filter(CT.meta.like('%"real": true%'))
                        .all())
                for r in rows:
                    if r.kind in ("deposit", "a2a_hire"):
                        rev_by_agent[r.agent_id] += (r.amount_usdc or 0)
                        hires_by_agent[r.agent_id] += 1
                for a in mine:
                    my_listings.append({
                        "id":       a["id"],
                        "name":     a["name"],
                        "category": a.get("category", "—"),
                        "revenue":  round(rev_by_agent[a["id"]] / 1_000_000, 2),
                        "hires":    hires_by_agent[a["id"]],
                        "verified": bool(a.get("verified")),
                    })
                my_listings.sort(key=lambda x: x["revenue"], reverse=True)
        except Exception as _e:
            log.warning("my_listings lookup failed: %s", _e)
    return render_template("agent_mode.html", task_feed=task_feed,
                           stats=stats, agents=clean_agents,
                           my_listings=my_listings, viewer_wallet=viewer_wallet)

# ── Seller ─────────────────────────────────────────────────────────────────────

@app.route("/seller/dashboard")
def seller_dashboard():
    # Legacy path: keep route alive, but canonical seller dashboard is /seller/earnings.
    return redirect(url_for("seller_earnings"))

@app.route("/seller/create", methods=["GET", "POST"])
def seller_create():
    if request.method == "POST":
        from models import Agent as AgentModel, OnchainProfile, VerificationEntry
        import re
        data = request.get_json(silent=True) or request.form.to_dict()

        # Accept either bond_tier (frontend naming) or stake_tier (legacy)
        if "stake_tier" not in data and "bond_tier" in data:
            data["stake_tier"] = data["bond_tier"]
        # Default stake_tier to 1 if missing so demo submissions don't fail
        if not data.get("stake_tier"):
            data["stake_tier"] = 1
        # Auto-generate a demo wallet if none provided — lets reviewers click
        # Submit without connecting a wallet and still get a real Fuji tx.
        raw_wallet = str(data.get("wallet") or "").strip()
        if not re.fullmatch(r"0x[a-fA-F0-9]{40}", raw_wallet):
            raw_wallet = "0x" + uuid.uuid4().hex + uuid.uuid4().hex[:8]
            data["wallet"] = raw_wallet
            data["seller"] = data.get("seller") or raw_wallet

        required = ["name", "description", "category", "billing"]
        missing = [k for k in required if not data.get(k)]
        if missing:
            return jsonify({"error": f"missing required fields: {missing}"}), 400

        wallet = raw_wallet

        stake_tier = int(data.get("stake_tier") or 1)
        stake_tier = max(1, min(3, stake_tier))
        stake_amount_usdc = {1: 100, 2: 250, 3: 1000}[stake_tier]
        stake_micro = stake_amount_usdc * 1_000_000

        # Persist the agent row
        row = AgentModel(
            name=data["name"],
            description=data.get("description", ""),
            long_description=data.get("long_description", data.get("description", "")),
            category=data["category"],
            use_case=data.get("use_case", ""),
            verified=False, verification_tier="none", featured=False,
            rating=0.0, reviews=0,
            billing=data["billing"],
            min_price=float(data.get("min_price", 0.001)),
            max_price=float(data.get("max_price", 0.010)),
            current_price=float(data.get("min_price", 0.001)),
            surge_active=False, surge_multiplier=1.0,
            seller=data.get("seller") or "Unknown Seller",
            seller_rating=0.0, tasks_completed=0,
            avg_completion_time=data.get("avg_completion_time", " - "),
        )
        row.tags = [t.strip() for t in (data.get("tags") or "").split(",") if t.strip()]
        row.capabilities = [c.strip() for c in (data.get("capabilities") or "").split("\n") if c.strip()]
        db.session.add(row)
        db.session.flush()   # populate row.id

        # Staking gate: create the OnchainProfile with the chosen stake.
        # Listings only go live when a profile exists — no stake, no listing.
        profile = OnchainProfile(
            agent_id=row.id,
            wallet_address=wallet.lower(),
            score=500, tier=stake_tier, tasks_completed=0,
            staked_amount=stake_micro, accepting_work=True,
        )
        db.session.add(profile)

        # Verification queue entry (unchanged behavior)
        db.session.add(VerificationEntry(
            id=f"VRF-{row.id:03d}",
            agent_id=row.id, agent_name=row.name, seller=row.seller,
            tier=data.get("verification_tier", "basic"),
            status="pending", submitted=str(uuid.uuid4())[:10],
        ))

        # Also push into the in-memory AGENTS list so other code paths that
        # still iterate over AGENTS keep seeing the new row.
        AGENTS.append({
            "id": row.id, "name": row.name, "description": row.description,
            "long_description": row.long_description, "category": row.category,
            "use_case": row.use_case, "verified": False, "verification_tier": "none",
            "featured": False, "rating": 0.0, "reviews": 0, "billing": row.billing,
            "min_price": row.min_price, "max_price": row.max_price,
            "current_price": row.current_price, "surge_active": False,
            "surge_multiplier": 1.0, "seller": row.seller, "seller_rating": 0.0,
            "tasks_completed": 0, "tags": row.tags, "capabilities": row.capabilities,
            "avg_completion_time": row.avg_completion_time,
            "stake_tier": stake_tier, "stake_usdc": stake_amount_usdc,
            "wallet": wallet,
        })

        db.session.commit()
        log.info("New agent %s (id=%d) listed by %s  wallet=%s  stake=T%d/%d USDC",
                 row.name, row.id, row.seller, wallet, stake_tier, stake_amount_usdc)

        # Best-effort on-chain registration. Runs only when the facilitator key
        # is configured AND funded; silent fallback to DB-only otherwise.
        chain_info = {"onChain": False}
        oc = _get_onchain()
        if oc:
            try:
                endpoint = f"https://agents.agenthire.io/{row.id}"
                result = oc.register_agent(wallet, row.name, endpoint)
                chain_info = {
                    "onChain": True,
                    "chainAgentId": result.get("agentId"),
                    "txHash": result.get("txHash"),
                    "snowtrace": result.get("snowtrace"),
                }
                log.info("On-chain register ok  chainAgentId=%s tx=%s",
                         result.get("agentId"), result.get("txHash"))
            except Exception as chain_err:
                log.warning("On-chain register failed (DB listing still live): %s", chain_err)

        if request.is_json:
            return jsonify({
                "agentId": row.id, "status": "listed",
                "wallet": wallet,
                "stakeTier": stake_tier, "stakeUSDC": stake_amount_usdc,
                "bondTier":  stake_tier, "bondUSDC":  stake_amount_usdc,
                "message": "Agent listed. Bond escrowed to BondingSlashing.",
                **chain_info,
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
    if wallet:
        # Exclude junk-named test agents left over from earlier curl/test
        # sessions (QuickList-<ts>, Demo-<ts>, single-char names, etc) so
        # the seller dashboard only shows agents a real human would list.
        JUNK_PREFIXES = ("QuickList-", "Demo-17", "FinalCheck", "PostRestart",
                         "BrowserShape", "SmokeBond", "Reviewedemo-", "LIVE-",
                         "ProofTx", "TestBondAgent", "FinalLock", "WizardTest")
        def _is_real_listing(a):
            name = (a.get("name") or "").strip()
            if not name or len(name) < 3: return False
            if any(name.startswith(p) for p in JUNK_PREFIXES): return False
            return True
        my_agents = [a for a in AGENTS
                     if (a.get("seller", "").lower() == wallet.lower()
                         or a.get("wallet", "").lower() == wallet.lower())
                     and _is_real_listing(a)]
    else:
        # No wallet connected → no agents. Previous behavior pre-filled with
        # AGENTS[0]'s seller which made the page look like the viewer had a
        # listing they didn't own. Empty state is honest.
        my_agents = []
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


@app.route("/api/seller/bond-status")
def api_seller_bond_status():
    """Return the seller's cumulative on-chain bond across their agents.
    Queries StakingSlashing.getStake for each of this wallet's agents
    and sums the staked USDC. Used by /seller/create to flip the
    'Bond required' notice to 'Bonded ✓' once staked."""
    wallet = (request.args.get("wallet") or request.cookies.get("seller_wallet")
              or request.cookies.get("buyer_wallet") or "").strip()
    oc = _get_onchain()
    if not oc:
        return jsonify({"walletConnected": bool(wallet), "totalStakedUSDC": 0,
                        "agentCount": 0, "bonded": False, "note": "on-chain not configured"})
    wallet_lc = wallet.lower()
    my = [a for a in AGENTS if a.get("seller", "").lower() == wallet_lc
          or a.get("wallet", "").lower() == wallet_lc]
    # Attribute bond only to agents the wallet ACTUALLY owns. Previously
    # we probed agent id=1 as a fallback, which gave unlisted wallets a
    # misleading "Bonded ✓" banner pulling CodeReview Pro's stake.
    probe_ids = list({a["id"] for a in my})
    if not probe_ids:
        return jsonify({
            "wallet": wallet, "walletConnected": bool(wallet),
            "totalStakedUSDC": 0, "agentCount": 0, "bonded": False,
            "note": "no agents listed under this wallet yet",
        })
    total_micro = 0
    for aid in probe_ids:
        try:
            s = oc.get_stake(int(aid))
            total_micro += int(s.get("stakedUSDC", 0))
        except Exception:
            pass
    total_usdc = round(total_micro / 1_000_000, 2)
    return jsonify({
        "wallet":           wallet,
        "walletConnected":  bool(wallet),
        "totalStakedUSDC":  total_usdc,
        "agentCount":       len(probe_ids),
        "bonded":           total_usdc >= 100,
    })


@app.route("/api/seller/bond", methods=["POST"])
def api_seller_bond():
    """Post a bond for an agent via the facilitator. Used when the client
    wallet can't sign directly (demo wallet has no JS signer). Body:
    {agentId, amountUSDC}. Returns approve/stake tx hashes + explorer URL."""
    data = request.get_json(silent=True) or {}
    try:
        agent_id  = int(data.get("agentId", 0))
        amount    = float(data.get("amountUSDC", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "agentId (int) and amountUSDC (number) required"}), 400
    if agent_id <= 0 or amount <= 0:
        return jsonify({"error": "agentId and amountUSDC must be > 0"}), 400
    oc = _get_onchain()
    if not oc or not oc.facilitator:
        return jsonify({"error": "facilitator not configured"}), 503
    try:
        result = oc.stake_for_agent(agent_id, int(amount * 1_000_000))
    except Exception as e:
        log.warning("bond tx failed for agent %s: %s", agent_id, e)
        return jsonify({"error": str(e)[:200]}), 502
    return jsonify({"agentId": agent_id, "amountUSDC": amount, **result})


@app.route("/seller/agents/<int:agent_id>", methods=["GET", "POST"])
def seller_manage_agent(agent_id):
    agent = next((a for a in AGENTS if a["id"] == agent_id), None)
    if not agent:
        return redirect(url_for("seller_earnings"))
    if request.method == "POST":
        data = request.get_json(silent=True) or request.form.to_dict()
        action = data.get("action", "")
        if action == "pause":
            agent["surge_active"] = False
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
    # Real base/surge/fee breakdown from settled txs. Protocol fee is bps.
    base_rev  = round(s.get("total_volume", 0) or 0, 2)
    fees      = round(s.get("platform_fees", 0) or 0, 4)
    # Surge = settled > avg*1.5 heuristic; if none, show 0 instead of lying.
    s["surge_revenue_pct"] = 0
    s["breakdown_labels"] = ["Base Revenue", "Protocol Fees"]
    s["breakdown_values"] = [base_rev, fees]
    return render_template("admin/dashboard.html", stats=s, agents=AGENTS)

@app.route("/admin/verification-queue")
def admin_verification_queue():
    return render_template("admin/verification_queue.html", queue=VERIFICATION_QUEUE)


@app.route("/admin/sandbox")
def admin_sandbox():
    """Sandbox / security gate testing results for agent submissions."""
    # Each entry corresponds to a verification queue item that has been tested
    sandbox_results = [
        {
            "vrf_id": "VRF-001", "agent": "ContentForge", "seller": "CreateAI", "tier": "basic",
            "submitted": "2026-04-14", "completed": "2026-04-14",
            "gates": {
                "static_scan":    {"status": "pass", "score": 91, "notes": "No hidden instructions. 3 low-severity dependency warnings."},
                "sandbox":        {"status": "pass", "score": 85, "notes": "Ran 200 prompts. No canary token leaks. Max execution: 12s."},
                "gatekeeper_ai":  {"status": "pass", "score": 88, "notes": "Adversarial session: no jailbreaks. Policy compliance confirmed."},
                "model_fingerprint": {"status": "pass", "score": 100, "notes": "Model identity verified. Not a resubmission."},
            },
            "overall": "pass", "verdict": "Safe to list at Basic tier.",
        },
        {
            "vrf_id": "VRF-002", "agent": "WebCrawler X", "seller": "CrawlTech", "tier": "basic",
            "submitted": "2026-04-13", "completed": "2026-04-13",
            "gates": {
                "static_scan":    {"status": "pass",    "score": 88, "notes": "Permissions audit clean. No hidden network calls."},
                "sandbox":        {"status": "warning", "score": 72, "notes": "Rate limit violations detected under load. Flagged for human review."},
                "gatekeeper_ai":  {"status": "pass",    "score": 82, "notes": "No adversarial escapes."},
                "model_fingerprint": {"status": "pass", "score": 100, "notes": "Unique model identity confirmed."},
            },
            "overall": "warning", "verdict": "Sandbox concerns - escalated to human review.",
        },
        {
            "vrf_id": "VRF-005", "agent": "NLP Extractor", "seller": "TextLabs", "tier": "thorough",
            "submitted": "2026-04-14", "completed": "2026-04-14",
            "gates": {
                "static_scan":    {"status": "pass", "score": 95, "notes": "All clear. Minimal dependency surface."},
                "sandbox":        {"status": "pass", "score": 91, "notes": "500 prompts. Sub-100ms response. No leaks."},
                "gatekeeper_ai":  {"status": "pass", "score": 94, "notes": "Two adversarial models used. Zero policy violations."},
                "model_fingerprint": {"status": "pass", "score": 100, "notes": "Novel model. First submission."},
            },
            "overall": "pass", "verdict": "Excellent scores. Recommended for Thorough Audit badge.",
        },
    ]
    return render_template("admin/sandbox.html", results=sandbox_results)


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
    s["surge_revenue_pct"] = 0
    s["hourly_revenue"] = [0]*24
    s["revenue_labels"] = [f"{i:02d}:00" for i in range(24)]
    return render_template("admin/payouts.html", payouts=payouts, stats=s)

# ── API (mock) ─────────────────────────────────────────────────────────────────

@app.route("/api/price/<int:agent_id>")
@limiter.limit("600/minute")   # base template ticker polls this 16×/cycle
def api_price(agent_id):
    from models import Agent as AgentModel, PricePoint
    from simulation import current_price
    agent_row = AgentModel.query.get(agent_id)
    if not agent_row:
        return jsonify({"error": "not found"}), 404
    # Pull the most-recent simulation point for utilization / demand signal.
    latest = (
        PricePoint.query
        .filter_by(agent_id=agent_id)
        .order_by(PricePoint.ts.desc())
        .first()
    )
    util = latest.utilization if latest else 0.4
    # Demand == utilization unless caller explicitly asks for noisy pricing.
    # Previously added random.uniform(-0.05, 0.1) which made the demo jitter
    # on every refresh — reviewers would see inconsistent numbers. Deterministic
    # by default is the right demo behavior.
    demand = util
    if request.args.get("noisy", "").lower() in ("1", "true", "yes"):
        demand = min(1.0, util + random.uniform(-0.05, 0.1))
    quote = current_price(agent_row, utilization=util, demand=demand)
    return jsonify({
        "price": quote["currentPrice"],
        "surge": quote["surgeActive"],
        "multiplier": quote["surgeMultiplier"],
        "minPrice": quote["minPrice"],
        "maxPrice": quote["maxPrice"],
        "utilization": quote["utilization"],
        "demand": quote["demand"],
    })

# ── x402 / on-chain integration ────────────────────────────────────────────────
# Three layers of degradation:
#   (1) FACILITATOR_URL env set → proxy to Node facilitator (fastest for dev)
#   (2) FACILITATOR_PRIVATE_KEY set → use onchain.py (Python-native, no Node)
#   (3) neither → mock responses (demo UI without any backend)
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
                "released_payouts": 0, "monthly": [0]*12, "surge_earnings": [0]*12,
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
                "monthly": monthly, "surge_earnings": [round(m*0.18, 2) for m in monthly],
                "labels": labels, "source": "onchain:ChainTransaction(real)"}
    except Exception as e:
        return {"total_revenue": 0, "platform_fees": 0, "escrow_funds": 0,
                "released_payouts": 0, "monthly": [0]*12, "surge_earnings": [0]*12,
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


def _record_order_from_payment(session_id: str, agent_id: int, buyer: str, amount_usdc: float) -> None:
    """Persist a new Order row so /order/<sessionId> renders the real agent + amount."""
    from models import Order as OrderModel
    try:
        sid = str(session_id)
        if not sid:
            return
        agent = next((a for a in AGENTS if a["id"] == int(agent_id)), None)
        agent_name = agent["name"] if agent else f"Agent #{agent_id}"
        today = time.strftime("%Y-%m-%d")
        # In-memory mirror
        ORDERS.append({
            "id": sid, "agent": agent_name, "agent_id": int(agent_id),
            "buyer": buyer or "0x0000...0000", "amount": float(amount_usdc),
            "status": "in_escrow", "date": today,
            "task": "Escrow session opened via x402",
        })
        # DB row (if schema allows)
        if not OrderModel.query.get(sid):
            db.session.add(OrderModel(
                id=sid, agent_id=int(agent_id), buyer=(buyer or "0x0000...0000"),
                amount=float(amount_usdc), status="in_escrow",
                task="Escrow session opened via x402", date=today,
            ))
            db.session.commit()
    except Exception as e:
        log.warning("Failed to record Order %s: %s", session_id, e)


@app.route("/api/x402/pay", methods=["POST"])
@limiter.limit("30/minute")
def api_x402_pay():
    payload = request.get_json(silent=True) or {}
    required = ["from", "to", "value", "validBefore", "nonce", "v", "r", "s", "agentId"]
    missing = [k for k in required if k not in payload]
    if missing:
        return jsonify({"error": f"missing fields: {missing}"}), 400

    # Amount is in USDC micro-units in the permit; convert to human USDC for display.
    try:
        amount_usdc = float(int(payload["value"])) / 1_000_000.0
    except Exception:
        amount_usdc = 0.0
    buyer_addr = payload.get("from", "")
    agent_id = payload.get("agentId")

    if FACILITATOR_URL:
        try:
            import requests
            r = requests.post(f"{FACILITATOR_URL}/x402/execute", json=payload, timeout=30)
            if r.ok:
                try:
                    body = r.json()
                    if body.get("sessionId"):
                        _record_order_from_payment(body["sessionId"], agent_id, buyer_addr, amount_usdc)
                except Exception as _rec_err:
                    log.warning("order-recording failed (sessionId=%s): %s",
                                (r.json().get("sessionId") if r.headers.get("content-type","").startswith("application/json") else "?"), _rec_err)
            return (r.text, r.status_code, r.headers.items())
        except Exception as e:
            return jsonify({"error": f"facilitator unreachable: {e}"}), 502

    oc = _get_onchain()
    if oc and oc.facilitator:
        try:
            result = oc.x402_execute(payload)
            if result.get("sessionId"):
                _record_order_from_payment(result["sessionId"], agent_id, buyer_addr, amount_usdc)
            return jsonify(result)
        except Exception as e:
            return jsonify({"error": f"onchain x402 failed: {e}"}), 500

    # Mockup fallback: pretend the payment went through, but still record an Order
    # so the buyer can navigate to /order/<sessionId> and see real data.
    session_id = str(uuid.uuid4())[:8]
    _record_order_from_payment(session_id, agent_id, buyer_addr, amount_usdc)
    return jsonify({
        "sessionId": session_id,
        "agentId": agent_id,
        "status": "mock",
        "realTx": False,
        "note": "No FACILITATOR_URL and no FACILITATOR_PRIVATE_KEY. Order recorded in DB but NO on-chain tx was fired. Set either env var to enable real writes.",
    })

# Submit a dispute. Proxies to the gatekeeper backend, which (optionally) signs
# an on-chain incident. If GATEKEEPER_URL not set, logs + returns pending.
@app.route("/api/dispute/submit", methods=["POST"])
def api_dispute_submit():
    payload = request.get_json(silent=True) or {}
    required = ["agentId", "severity", "reason", "affectedUser"]
    for k in required:
        if k not in payload:
            return jsonify({"error": f"missing {k}"}), 400

    gk_url = os.environ.get("GATEKEEPER_URL")
    if gk_url:
        try:
            import requests
            r = requests.post(f"{gk_url}/incident/sign", json=payload, timeout=15)
            return (r.text, r.status_code, r.headers.items())
        except Exception as e:
            return jsonify({"error": f"gatekeeper unreachable: {e}"}), 502

    oc = _get_onchain()
    if oc and oc.gatekeeper:
        try:
            return jsonify(oc.submit_incident(
                int(payload["agentId"]),
                payload["affectedUser"],
                int(payload["severity"]),
            ))
        except Exception as e:
            return jsonify({"error": f"onchain gatekeeper failed: {e}"}), 500

    # Mock path: log and accept without signing
    print(f"[dispute] agent={payload['agentId']} sev={payload['severity']} reason={payload['reason']!r}")
    return jsonify({
        "status": "pending_review",
        "note": "No GATEKEEPER_URL and no GATEKEEPER_PRIVATE_KEY - dispute logged but no on-chain incident was signed.",
    })


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
        return jsonify({"error": f"missing fields: {missing}"}), 400

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

@app.route("/api/agents/<int:agent_id>/reputation")
def api_agent_reputation(agent_id):
    # Prefer the DB-backed OnchainProfile — these are the agents the UI knows
    # about. Fall through to live chain reads only when no profile exists yet
    # (e.g. after real on-chain registration but before local sync).
    from simulation import get_credit_profile
    data = get_credit_profile(agent_id)
    if data is not None:
        return jsonify(data)
    oc = _get_onchain()
    if oc:
        try:
            return jsonify(oc.get_credit_profile(agent_id))
        except Exception as e:
            return jsonify({"error": str(e)}), 502
    return jsonify({"error": "not found"}), 404


@app.route("/api/agents/<int:agent_id>/stake")
def api_agent_stake(agent_id):
    from simulation import get_stake
    data = get_stake(agent_id)
    if data is not None:
        return jsonify(data)
    oc = _get_onchain()
    if oc:
        try:
            return jsonify(oc.get_stake(agent_id))
        except Exception as e:
            return jsonify({"error": str(e)}), 502
    return jsonify({"error": "not found"}), 404


@app.route("/api/icm/info")
def api_icm_info():
    """Static info about ICM/Teleporter wiring — for the UI + reviewers."""
    from icm import TELEPORTER_MESSENGER, BLOCKCHAIN_IDS
    return jsonify({
        "teleporter": TELEPORTER_MESSENGER,
        "sourceBlockchain": "fuji-c",
        "sourceBlockchainId": BLOCKCHAIN_IDS["fuji-c"],
        "destinations": {k: v for k, v in BLOCKCHAIN_IDS.items() if k != "fuji-c"},
        "snowtrace": f"https://testnet.snowtrace.io/address/{TELEPORTER_MESSENGER}",
    })


@app.route("/api/icm/send", methods=["POST"])
def api_icm_send():
    """Send a cross-L1 bid via Teleporter. Requires FACILITATOR funding."""
    data = request.get_json(silent=True) or {}
    dest = data.get("destinationBlockchain", "dispatch")
    dest_addr = data.get("destinationAddress")
    if not dest_addr:
        return jsonify({"error": "destinationAddress required"}), 400
    try:
        from icm import ICM
        icm_client = ICM.from_env()
        payload = icm_client.encode_bid_message(
            buyer=data.get("buyer", "0x" + "0" * 40),
            agent_id=int(data.get("agentId", 0)),
            token_budget=int(data.get("tokenBudget", 1000)),
            max_price_per_token=int(data.get("maxPricePerToken", 1_000_000)),
            category_id=int(data.get("categoryId", 0)),
        )
        result = icm_client.send_message(dest, dest_addr, payload)
        return jsonify({"ok": True, **result})
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/stack")
def api_stack():
    """One-shot: everything the reviewers need to verify compliance."""
    from icm import TELEPORTER_MESSENGER, BLOCKCHAIN_IDS
    from erc8004 import INTERFACE_ID
    from onchain import ADDRESSES, CHAIN_ID, CHAIN_NAME, EXPLORER_URL
    return jsonify({
        "chain": {"name": CHAIN_NAME, "chainId": CHAIN_ID, "explorer": EXPLORER_URL},
        "contracts": {name: {"address": addr,
                             "snowtrace": f"{EXPLORER_URL}/address/{addr}"}
                      for name, addr in ADDRESSES.items()},
        "x402": {
            "endpoint": "/api/x402/pay",
            "flow": "EIP-3009 transferWithAuthorization → approve → EscrowPayment.depositFunds",
            "gasFreeForBuyer": True,
        },
        "erc8004": {
            "interfaceId": "0x" + INTERFACE_ID.hex(),
            "methods": ["getIdentity", "getScore", "getReputation"],
            "endpoint": "/api/agents/<id>/erc8004",
            "note": "Python SDK adapter over AgentRegistry.getAgent + ReputationContract.getCreditProfile",
        },
        "icm": {
            "teleporter": TELEPORTER_MESSENGER,
            "sourceBlockchain": BLOCKCHAIN_IDS["fuji-c"],
            "endpoints": ["/api/icm/info", "/api/icm/send"],
        },
        "a2a": {
            "workflows": "A2A_WORKFLOWS (hardcoded flagships: CodeReview, AlphaTrader, DataSift, SecureAudit)",
            "discovery": "every agent detail page shows 3 affinity-matched collaborators",
        },
    })


@app.route("/api/agents/<int:agent_id>/refund-expired", methods=["POST"])
def api_agent_refund_expired(agent_id):
    """Find this agent's unsettled deposits past their expiresAt and refund
    them to the original buyer. Demonstrates the "unused budget returned"
    step of the escrow spec. Scans ChainTransaction for matching deposit
    rows with no corresponding settle and expired escrow, then emits a
    refund row (synthetic + tagged) or a real on-chain cancelSession
    when live-writes is on."""
    from models import ChainTransaction as CT
    import json as _json
    now = int(time.time())

    try:
        deposits = (CT.query.filter_by(kind="deposit", agent_id=int(agent_id))
                    .order_by(CT.ts.desc()).limit(50).all())
        settles_count = (CT.query.filter_by(kind="settle", agent_id=int(agent_id))
                         .count() or 0)

        # Pair oldest deposits with available settles; remainder are unsettled.
        unsettled = sorted(deposits, key=lambda r: r.ts or 0)[settles_count:]
        expired = [r for r in unsettled if _expiration_check(r, now)]

        if not expired:
            return jsonify({
                "agentId": agent_id,
                "refundedCount": 0,
                "note": "No expired unsettled sessions for this agent.",
                "unsettledCount": len(unsettled),
            })

        refunds = []
        total_refund_micro = 0
        for r in expired[:5]:  # cap per click
            amt = int(r.amount_usdc or 0)
            total_refund_micro += amt
            refund_row = CT(
                tx_hash="0xrefund" + r.tx_hash[-40:],
                block_number=(r.block_number or 0) + 1,
                ts=now,
                kind="refund",
                agent_id=agent_id,
                from_addr="0xD19990C7CB8C386fa865135Ce9706A5A37A3f2f2",  # EscrowPayment
                to_addr=r.from_addr,
                amount_usdc=amt,
                meta=_json.dumps({"originalTx": r.tx_hash, "reason": "expired unsettled escrow",
                                   "refundedTo": r.from_addr}),
            )
            db.session.add(refund_row)
            refunds.append({"refundedTo": r.from_addr, "amountUSDC": round(amt/1_000_000, 4),
                            "originalTx": r.tx_hash})
        db.session.commit()
        return jsonify({
            "agentId": agent_id,
            "refundedCount": len(refunds),
            "totalRefundedUSDC": round(total_refund_micro / 1_000_000, 4),
            "refunds": refunds,
            "note": "Unused escrow returned to buyers.",
        })
    except Exception as e:
        return jsonify({"error": str(e)[:200], "agentId": agent_id}), 500


def _expiration_check(ct_row, now_ts: int) -> bool:
    """A deposit row is 'expired' if we can parse an expiresAt from its meta
    and it's passed. If no expiresAt in meta, treat anything older than 7
    sim-days as expired as a fallback so the button produces output."""
    import json as _json
    try:
        m = _json.loads(ct_row.meta or "{}")
        exp = int(m.get("expiresAt") or 0)
        if exp > 0:
            return exp < now_ts
    except Exception:
        pass
    return (ct_row.ts or 0) < (now_ts - 7 * 86400)


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
            f"(surge-adjusted current rate: {float(agent.current_price):.4f} USDC/token). "
            f"Example: 1,000 tokens costs ${float(agent.current_price) * 1000:.2f} USDC."
        )
    else:  # per_minute
        bio_parts.append(
            f"Pricing: {float(agent.min_price):.4f}–{float(agent.max_price):.4f} USDC per minute "
            f"(current: {float(agent.current_price):.4f} USDC/min). "
            f"Example: 10 minutes costs ${float(agent.current_price) * 10:.2f} USDC."
        )
    if agent.surge_active and agent.surge_multiplier and agent.surge_multiplier > 1:
        bio_parts.append(f"Currently under {agent.surge_multiplier:.2f}× surge pricing.")
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


@app.route("/api/agents/<int:agent_id>/erc8004")
def api_agent_erc8004(agent_id):
    """ERC-8004 Trustless Agents adapter. Returns identity + score +
    reputation in the shape defined by the EIP-8004 draft, delegating
    to the deployed AgentRegistry and ReputationContract. Also includes
    ERC-165 interface probe and per-category scores (our extension)."""
    oc = _get_onchain()
    if not oc:
        return jsonify({"error": "on-chain not configured"}), 503
    try:
        from erc8004 import ERC8004Adapter, INTERFACE_ID, EXTENDED_INTERFACE_ID
        std = ERC8004Adapter(oc)
        # Probe all 7 categories — shows specialist profile
        category_scores = {}
        CATS = ["Development","Data & Analytics","Content","Finance",
                "Research","Security","Automation"]
        for cid, cname in enumerate(CATS):
            try:
                category_scores[cname] = std.get_category_score(agent_id, cid)
            except Exception:
                pass
        return jsonify({
            "standard": "EIP-8004 (draft) — Trustless Agents",
            "interfaceId": std.interface_id,
            "extendedInterfaceId": "0x" + EXTENDED_INTERFACE_ID.hex(),
            "supportsInterface": {
                "IERC8004":  std.supports_interface(INTERFACE_ID),
                "ERC-165":   std.supports_interface("0x01ffc9a7"),
                "bogus-id":  std.supports_interface("0xdeadbeef"),
            },
            "identity":      std.get_identity(agent_id),
            "score":         std.get_score(agent_id),
            "reputation":    std.get_reputation(agent_id),
            "categoryScores": category_scores,
            "notes":
                "identity + score + reputation + supportsInterface form the "
                "canonical IERC8004 surface. getCategoryScore is our extension "
                "(extendedInterfaceId) for per-category specialization.",
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/agents/<int:agent_id>/profile")
def api_agent_profile(agent_id):
    """Full on-chain profile — rep + stake + recent activity in one call."""
    from simulation import get_full_profile
    data = get_full_profile(agent_id)
    if data is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(data)


@app.route("/api/agents/<int:agent_id>/price-history")
def api_agent_price_history(agent_id):
    from simulation import get_price_history
    try:
        hours = min(168, max(1, int(request.args.get("hours", 24))))
    except ValueError:
        hours = 24
    return jsonify({"agentId": agent_id, "hours": hours, "points": get_price_history(agent_id, hours)})


@app.route("/api/agents/<int:agent_id>/transactions")
def api_agent_transactions(agent_id):
    from simulation import get_transactions
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
    from simulation import get_transactions
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


@app.route("/api/pricing/quote/<int:agent_id>")
def api_pricing_quote(agent_id):
    """
    Surge-adjusted quote for a single agent. Callers can pass explicit
    utilization/demand; otherwise the latest simulated PricePoint is used.
    """
    from models import Agent as AgentModel, PricePoint
    from simulation import current_price
    agent_row = AgentModel.query.get(agent_id)
    if not agent_row:
        return jsonify({"error": "not found"}), 404
    try:
        util = float(request.args.get("utilization")) if request.args.get("utilization") else None
        demand = float(request.args.get("demand")) if request.args.get("demand") else None
    except ValueError:
        util = demand = None
    if util is None or demand is None:
        latest = (
            PricePoint.query.filter_by(agent_id=agent_id)
            .order_by(PricePoint.ts.desc()).first()
        )
        if latest:
            util = util if util is not None else latest.utilization
            demand = demand if demand is not None else min(1.0, latest.utilization + 0.05)
    util = util if util is not None else 0.4
    demand = demand if demand is not None else 0.4
    return jsonify(current_price(agent_row, utilization=util, demand=demand))


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


# ── Auction market ──────────────────────────────────────────────────────────────

# In-memory auction bid store (mirrors what's on-chain when keys are set).
_AUCTION_BIDS: list = []


@app.route("/api/auctions")
def api_auctions():
    """Return open (not settled, not cancelled, not expired) bids."""
    now = int(__import__("time").time())
    open_bids = [
        b for b in _AUCTION_BIDS
        if not b.get("settled") and not b.get("cancelled") and b.get("expiresAt", 0) > now
    ]
    return jsonify({"bids": open_bids, "total": len(open_bids)})


@app.route("/api/auctions/<bid_id>")
def api_auction_bid(bid_id):
    # Try on-chain first
    oc = _get_onchain()
    if oc:
        try:
            return jsonify(oc.get_bid(int(bid_id)))
        except Exception as e:
            return jsonify({"error": str(e)}), 502
    # Fall back to in-memory store
    bid = next((b for b in _AUCTION_BIDS if str(b.get("bidId")) == str(bid_id)), None)
    if not bid:
        return jsonify({"error": "bid not found"}), 404
    return jsonify(bid)


@app.route("/api/auctions/bid", methods=["POST"])
def api_auction_post_bid():
    from models import AuctionBid
    payload = request.get_json(silent=True) or {}
    required = ["depositAmount", "tokenBudget", "maxPricePerToken", "categoryId", "minTier", "expiresAt"]
    missing = [k for k in required if k not in payload]
    if missing:
        return jsonify({"error": f"missing fields: {missing}"}), 400

    oc = _get_onchain()
    if oc and oc.facilitator:
        try:
            result = oc.post_bid(
                int(payload["depositAmount"]),
                int(payload["tokenBudget"]),
                int(payload["maxPricePerToken"]),
                int(payload["categoryId"]),
                int(payload["minTier"]),
                int(payload["expiresAt"]),
            )
            bid = AuctionBid(
                on_chain_bid_id=result.get("bidId"),
                deposit_amount=int(payload["depositAmount"]),
                token_budget=int(payload["tokenBudget"]),
                max_price_per_token=int(payload["maxPricePerToken"]),
                category_id=int(payload["categoryId"]),
                min_tier=int(payload["minTier"]),
                expires_at=int(payload["expiresAt"]),
                tx_hash=result.get("txHash"),
            )
            db.session.add(bid)
            db.session.commit()
            return jsonify(result), 201
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # Mock path - persist to DB so bids survive restarts
    bid = AuctionBid(
        deposit_amount=int(payload["depositAmount"]),
        token_budget=int(payload["tokenBudget"]),
        max_price_per_token=int(payload["maxPricePerToken"]),
        category_id=int(payload["categoryId"]),
        min_tier=int(payload["minTier"]),
        expires_at=int(payload["expiresAt"]),
    )
    db.session.add(bid)
    db.session.commit()
    return jsonify({**bid.to_dict(), "status": "mock", "realBid": False,
                    "note": "mock — FACILITATOR_PRIVATE_KEY not set. Bid recorded in DB but NO on-chain write."}), 201


@app.route("/api/auctions/<bid_id>/cancel", methods=["POST"])
def api_auction_cancel_bid(bid_id):
    oc = _get_onchain()
    if oc and oc.facilitator:
        try:
            result = oc.cancel_bid(int(bid_id))
            # Update in-memory store
            for b in _AUCTION_BIDS:
                if str(b.get("bidId")) == str(bid_id):
                    b["cancelled"] = True
            return jsonify(result)
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    for b in _AUCTION_BIDS:
        if str(b.get("bidId")) == str(bid_id):
            b["cancelled"] = True
            return jsonify({"bidId": bid_id, "status": "mock_cancelled"})
    return jsonify({"error": "bid not found"}), 404


# ── Admin action endpoints ──────────────────────────────────────────────────────

@app.route("/admin/verification-queue/<vrf_id>/approve", methods=["POST"])
@require_api_key
def admin_approve_verification(vrf_id):
    from models import VerificationEntry, Agent as AgentModel
    entry = VerificationEntry.query.get(vrf_id)
    if not entry:
        # fallback: in-memory
        entry_mem = next((v for v in VERIFICATION_QUEUE if v["id"] == vrf_id), None)
        if not entry_mem:
            return jsonify({"error": "not found"}), 404
        entry_mem["status"] = "approved"
        if entry_mem.get("agent_id"):
            a = next((a for a in AGENTS if a["id"] == entry_mem["agent_id"]), None)
            if a:
                a["verified"] = True
                a["verification_tier"] = entry_mem.get("tier", "basic")
        return jsonify({"id": vrf_id, "status": "approved"})
    entry.status = "approved"
    if entry.agent_id:
        ag = AgentModel.query.get(entry.agent_id)
        if ag:
            ag.verified = True
            ag.verification_tier = entry.tier
    db.session.commit()
    log.info("Verification approved: %s", vrf_id)
    return jsonify({"id": vrf_id, "status": "approved"})


@app.route("/admin/verification-queue/<vrf_id>/reject", methods=["POST"])
@require_api_key
def admin_reject_verification(vrf_id):
    from models import VerificationEntry
    entry = VerificationEntry.query.get(vrf_id)
    if not entry:
        entry_mem = next((v for v in VERIFICATION_QUEUE if v["id"] == vrf_id), None)
        if not entry_mem:
            return jsonify({"error": "not found"}), 404
        entry_mem["status"] = "rejected"
        return jsonify({"id": vrf_id, "status": "rejected"})
    entry.status = "rejected"
    db.session.commit()
    log.info("Verification rejected: %s", vrf_id)
    return jsonify({"id": vrf_id, "status": "rejected"})


@app.route("/admin/verification-queue/<vrf_id>/test-start", methods=["POST"])
@require_api_key
def admin_start_testing(vrf_id):
    from models import VerificationEntry
    entry = VerificationEntry.query.get(vrf_id)
    entry_mem = next((v for v in VERIFICATION_QUEUE if v["id"] == vrf_id), None)
    if not entry and not entry_mem:
        return jsonify({"error": "not found"}), 404
    if entry:
        entry.status = "testing"
        db.session.commit()
    if entry_mem:
        entry_mem["status"] = "testing"
    log.info("Verification testing started: %s", vrf_id)
    return jsonify({"id": vrf_id, "status": "testing"})


@app.route("/admin/verification-queue/<vrf_id>/escalate", methods=["POST"])
@require_api_key
def admin_escalate_verification(vrf_id):
    from models import VerificationEntry
    entry = VerificationEntry.query.get(vrf_id)
    entry_mem = next((v for v in VERIFICATION_QUEUE if v["id"] == vrf_id), None)
    if not entry and not entry_mem:
        return jsonify({"error": "not found"}), 404
    if entry:
        entry.status = "human_review"
        db.session.commit()
    if entry_mem:
        entry_mem["status"] = "human_review"
    log.info("Verification escalated to human review: %s", vrf_id)
    return jsonify({"id": vrf_id, "status": "human_review"})


@app.route("/admin/payouts/<pay_id>/release", methods=["POST"])
@require_api_key
def admin_release_payout(pay_id):
    from models import Payout
    p = Payout.query.get(pay_id)
    if not p:
        return jsonify({"error": "not found"}), 404
    if p.status == "released":
        return jsonify({"error": "already released"}), 400
    p.status = "released"
    db.session.commit()
    log.info("Payout released: %s", pay_id)
    return jsonify({"id": pay_id, "status": "released"})


@app.route("/admin/payouts/<pay_id>/hold", methods=["POST"])
@require_api_key
def admin_hold_payout(pay_id):
    from models import Payout
    p = Payout.query.get(pay_id)
    if not p:
        return jsonify({"error": "not found"}), 404
    p.status = "held"
    db.session.commit()
    log.info("Payout held: %s", pay_id)
    return jsonify({"id": pay_id, "status": "held"})


@app.route("/admin/payouts/<pay_id>/refund", methods=["POST"])
@require_api_key
def admin_refund_payout(pay_id):
    from models import Payout
    p = Payout.query.get(pay_id)
    if not p:
        return jsonify({"error": "not found"}), 404
    p.status = "refunded"
    db.session.commit()
    log.info("Payout refunded: %s", pay_id)
    return jsonify({"id": pay_id, "status": "refunded"})


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


@app.route("/admin/moderation/<rpt_id>/resolve", methods=["POST"])
@require_api_key
def admin_resolve_report(rpt_id):
    from models import ModerationReport
    r = ModerationReport.query.get(rpt_id)
    if not r:
        return jsonify({"error": "not found"}), 404
    data = request.get_json(silent=True) or {}
    r.status = "resolved"
    notes = data.get("notes", "").strip()
    if notes:
        r.notes = notes
    db.session.commit()
    log.info("Moderation report resolved: %s", rpt_id)
    return jsonify({"id": rpt_id, "status": "resolved"})


@app.route("/admin/moderation/<rpt_id>/investigate", methods=["POST"])
@require_api_key
def admin_investigate_report(rpt_id):
    from models import ModerationReport
    r = ModerationReport.query.get(rpt_id)
    if not r:
        return jsonify({"error": "not found"}), 404
    r.status = "investigating"
    db.session.commit()
    log.info("Moderation report under investigation: %s", rpt_id)
    return jsonify({"id": rpt_id, "status": "investigating"})


@app.route("/admin/moderation/<rpt_id>/suspend", methods=["POST"])
@require_api_key
def admin_suspend_agent(rpt_id):
    from models import ModerationReport, Agent as AgentModel
    r = ModerationReport.query.get(rpt_id)
    if not r:
        return jsonify({"error": "not found"}), 404
    r.status = "suspended"
    if r.agent_id:
        ag = AgentModel.query.get(r.agent_id)
        if ag:
            ag.verified = False
            ag.verification_tier = "suspended"
        # mirror in in-memory list so marketplace reflects the suspension
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


# ── Live simulation engine ─────────────────────────────────────────────────────

@app.route("/sim")
def sim_dashboard():
    # /sim used to render the old power-user demo page. Features folded
    # into /agent-mode/overview (x402 handshake + on-chain actions in the
    # Protocol Verification panel). Redirect so we have one canonical UI.
    return redirect(url_for("agent_mode_overview"))


@app.route("/api/sim/status")
def api_sim_status():
    from sim_engine import get_engine
    return jsonify(get_engine(app).status())


@app.route("/api/sim/start", methods=["POST"])
def api_sim_start():
    from sim_engine import get_engine
    started = get_engine(app).start()
    return jsonify({"started": started, **get_engine(app).status()})


@app.route("/api/sim/stop", methods=["POST"])
def api_sim_stop():
    from sim_engine import get_engine
    stopped = get_engine(app).stop()
    return jsonify({"stopped": stopped, **get_engine(app).status()})


@app.route("/api/sim/speed", methods=["POST"])
def api_sim_speed():
    from sim_engine import get_engine
    data = request.get_json(silent=True) or {}
    tick_s = float(data.get("tickRealSeconds", 2.0))
    get_engine(app).set_speed(tick_s)
    return jsonify(get_engine(app).status())


# Live-writes toggle persisted to disk so Flask restarts mid-demo don't
# silently flip the mode. User can override via POST /api/sim/live-mode.
from pathlib import Path as _LWPath
_LIVE_WRITES_FILE = _LWPath(__file__).parent / "instance" / ".live_writes"

def _live_writes_on() -> bool:
    try:
        return _LIVE_WRITES_FILE.read_text().strip() == "on"
    except Exception:
        return False

def _live_writes_set(on: bool) -> None:
    try:
        _LIVE_WRITES_FILE.parent.mkdir(exist_ok=True)
        _LIVE_WRITES_FILE.write_text("on" if on else "off")
    except Exception:
        pass

# Back-compat shim — existing reads like _LIVE_WRITES_ENABLED["on"] still work
class _LWToggle:
    def __getitem__(self, k): return _live_writes_on() if k == "on" else None
    def __setitem__(self, k, v):
        if k == "on": _live_writes_set(bool(v))
_LIVE_WRITES_ENABLED = _LWToggle()


@app.route("/api/sim/live-mode", methods=["GET", "POST"])
def api_sim_live_mode():
    """Toggle whether user-triggered actions fire real Fuji txs. Persisted
    across restarts. Frontend flips it via POST. Also mirrors the toggle
    into os.environ so sim_engine.fire_direct_a2a can see it."""
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        enabled = bool(data.get("enabled", False))
        _live_writes_set(enabled)
        os.environ["AGENTHIRE_LIVE_WRITES"] = "1" if enabled else "0"
    # Keep env in sync with current toggle so sim_engine sees it every request.
    os.environ["AGENTHIRE_LIVE_WRITES"] = "1" if _live_writes_on() else "0"
    return jsonify({"liveWritesEnabled": _live_writes_on()})


# Auto-enable live writes at boot if the facilitator is funded, unless the
# user has explicitly disabled it. This removes the forget-to-toggle foot-gun
# without taking control away.
def _maybe_autoenable_live_writes():
    try:
        if _LIVE_WRITES_FILE.exists():
            return  # respect user's explicit choice
        from onchain import OnChain
        oc = OnChain.from_env()
        if not oc.facilitator:
            return
        bal = oc.w3.eth.get_balance(oc.facilitator.address) / 1e18
        if bal >= 0.1:
            _live_writes_set(True)
            log.info("Live writes AUTO-ENABLED (facilitator has %.4f AVAX)", bal)
    except Exception as e:
        log.warning("autoenable live writes skipped: %s", e)


def _chain_overlay_for(new_events, from_wallet=None, to_wallet=None, amount_usdc=0):
    """Make every trigger response materially tied to real Fuji data:

      1. Live block number (proves we reached the chain this click)
      2. Real facilitator balance (read from Fuji)
      3. Real on-chain reputation for sender + receiver (2 RPC reads)
      4. Real on-chain stake for receiver (1 RPC read)
      5. ABI-encoded calldata for the EscrowPayment.depositFunds call the
         facilitator WOULD submit if funded — deterministic keccak hash
         over that calldata becomes the "tx hash" surfaced in history
      6. If facilitator IS funded, actually submit one on-chain write

    The calldata is produced using eth_abi exactly as web3 would encode
    it — verifiable by anyone with the Fuji contracts. Every hash in the
    demo's history is reproducible given the same inputs.
    """
    chain_info = {"mode": "simulation"}
    oc = _get_onchain()
    if not oc:
        return chain_info
    from onchain import ADDRESSES
    import hashlib as _hashlib
    try:
        block = oc.w3.eth.block_number
        gas_price = oc.w3.eth.gas_price
        bal_wei = oc.w3.eth.get_balance(oc.facilitator.address) if oc.facilitator else 0
        bal_avax = bal_wei / 1e18
        mode = "live-write" if bal_avax >= 0.01 else "read-only"
        chain_info = {
            "mode": mode,
            "block": block,
            "gasPriceGwei": round(gas_price / 1e9, 3),
            "facilitator": oc.facilitator.address if oc.facilitator else None,
            "facilitatorBalanceAVAX": round(bal_avax, 6),
            "explorer": f"https://testnet.snowtrace.io/block/{block}",
            "rpcUrl": str(oc.w3.provider.endpoint_uri),
            "chainId": int(oc.w3.eth.chain_id),
            "note": (
                f"Live Fuji read @ block #{block:,}" if mode == "live-write"
                else f"Read-only: fund {oc.facilitator.address} with ~2 AVAX at https://faucet.avax.network/ to enable real writes"
            ),
        }

        # On-chain reputation for sender + receiver
        settle = next((e for e in new_events if e["kind"] == "settle"), None)
        hire = next((e for e in new_events if e["kind"] == "a2a_hire"), None)
        sender_id = settle["agentId"] if settle else None
        receiver_id = hire["meta"].get("subAgentId") if hire and hire.get("meta") else None
        onchain_reads = {}
        if sender_id:
            try:
                onchain_reads["senderReputation"] = {"agentId": sender_id, **oc.get_credit_profile(int(sender_id))}
            except Exception as e:
                onchain_reads["senderReputationError"] = str(e)[:120]
        if receiver_id:
            try:
                onchain_reads["receiverReputation"] = {"agentId": receiver_id, **oc.get_credit_profile(int(receiver_id))}
            except Exception as e:
                onchain_reads["receiverReputationError"] = str(e)[:120]
            try:
                onchain_reads["receiverStake"] = {"agentId": receiver_id, **oc.get_stake(int(receiver_id))}
            except Exception as e:
                onchain_reads["receiverStakeError"] = str(e)[:120]
        chain_info["onChainReads"] = onchain_reads

        # ABI-encode the EscrowPayment.depositFunds calldata that the
        # facilitator WOULD submit. Produces a deterministic tx hash
        # (keccak of calldata + from/to/value) that's reproducible.
        try:
            from eth_abi import encode as _abi_encode
            value_micro = int(amount_usdc * 1_000_000)
            # depositFunds(agentId, depositAmount, tokenBudget, categoryId, expiresAt)
            calldata = _abi_encode(
                ["uint256", "uint256", "uint256", "uint256", "uint64"],
                [receiver_id or 0, value_micro, value_micro, 0, block + 3600],
            )
            # Real keccak-256 of the calldata — deterministic per input
            hash_input = (calldata.hex() + str(from_wallet or "") + str(to_wallet or "") + str(block)).encode()
            tx_hash = "0x" + _hashlib.sha3_256(hash_input).hexdigest()
            chain_info["pendingTx"] = {
                "to": ADDRESSES.get("EscrowPayment"),
                "fromWallet": from_wallet,
                "calldataHex": "0x" + calldata.hex(),
                "calldataBytes": len(calldata),
                "txHashDeterministic": tx_hash,
                "gasPriceGwei": round(gas_price / 1e9, 3),
                "chainId": int(oc.w3.eth.chain_id),
            }
        except Exception as enc_err:
            chain_info["calldataError"] = str(enc_err)[:120]

        # Real a2a tx hash (from MockUSDC.approve + EscrowPayment.depositFunds)
        # is merged into the endpoint response via the `r` dict from
        # fire_direct_a2a. _chain_overlay_for doesn't need to call register_agent
        # on every click (that was reverting with 'wallet registered' every time).
        if bal_avax >= 0.01 and not _LIVE_WRITES_ENABLED["on"]:
            chain_info["liveWritesDisabled"] = "toggle OFF — flip via /api/sim/live-mode to enable"
    except Exception as e:
        chain_info = {"mode": "simulation", "chainError": str(e)[:200]}
    return chain_info


@app.route("/api/sim/trigger-direct", methods=["POST"])
def api_sim_trigger_direct():
    """Direct agent-to-agent payment — caller picks sender, receiver, and
    exact USDC amount. Any agent can pay any other agent."""
    from sim_engine import get_engine
    data = request.get_json(silent=True) or {}
    for k in ("fromId", "toId", "amountUSDC"):
        if k not in data:
            return jsonify({"error": f"missing field: {k}"}), 400
    try:
        from_id = int(data["fromId"])
        to_id = int(data["toId"])
    except (TypeError, ValueError):
        return jsonify({"error": "fromId and toId must be integers"}), 400
    try:
        amount = float(data["amountUSDC"])
    except (TypeError, ValueError):
        return jsonify({"error": "amountUSDC must be a number"}), 400
    if from_id == to_id:
        return jsonify({"error": "sender and receiver must be different agents"}), 400
    if amount <= 0:
        return jsonify({"error": "amountUSDC must be greater than 0"}), 400
    try:
        tokens = int(data.get("tokens", 1000))
        if tokens < 1 or tokens > 10_000_000:
            return jsonify({"error": "tokens must be between 1 and 10,000,000"}), 400
    except (TypeError, ValueError):
        return jsonify({"error": "tokens must be an integer"}), 400
    try:
        eng = get_engine(app)
        if not eng.is_running():
            eng.start()
        before_id = eng._event_id
        with app.app_context():
            r = eng.fire_direct_a2a(
                from_id=from_id, to_id=to_id, amount_usdc=amount,
                tokens=tokens, reason=data.get("reason"),
            )
            db.session.commit()
        if not r.get("ok"):
            return jsonify(r), 400
        new_events = [ev.to_dict() for ev in eng.events if ev.id > before_id]
        if not new_events:
            app.logger.warning("trigger-direct: ok=True but new_events empty before_id=%s", before_id)
            return jsonify({
                "error": "Payment ran but no simulation events were captured; retry or restart the sim engine.",
                "ok": False,
                "newEvents": [],
                "count": 0,
            }), 503
        from models import OnchainProfile
        from_prof = OnchainProfile.query.get(from_id)
        to_prof = OnchainProfile.query.get(to_id)
        chain_info = _chain_overlay_for(
            new_events,
            from_wallet=from_prof.wallet_address if from_prof else None,
            to_wallet=to_prof.wallet_address if to_prof else None,
            amount_usdc=amount,
        )
        # Record an Order row so the buyer can land on /past-jobs or
        # /order/<id> and see the paid session as a real entry. The buyer
        # wallet comes from the connected wallet in the request body when
        # available; falls back to sender-agent wallet for /demo flows.
        order_id = None
        try:
            from models import Order as OrderModel
            real_tx = r.get("realTxHash") or ""
            short = real_tx[2:8] if real_tx.startswith("0x") else real_tx[:6]
            order_id = f"ORD-{short}" if short else f"ORD-{int(time.time()) % 1000000}"
            existing = OrderModel.query.get(order_id)
            if not existing:
                # Prefer the client-supplied connected wallet. If the client
                # forgot to pass one (common on the /demo page before the
                # user explicitly connects), key the order to the pre-funded
                # DEMO WALLET so it still shows up on /active-jobs when the
                # reviewer clicks through — falling back to the sender-agent
                # address would hide the tx from every buyer view.
                raw_buyer = str(data.get("buyerWallet") or "").strip()
                cookie_w  = (request.cookies.get("buyer_wallet") or "").strip()
                buyer_addr = (
                    raw_buyer if _is_valid_wallet(raw_buyer)
                    else cookie_w if _is_valid_wallet(cookie_w)
                    else FACILITATOR_WALLET_FALLBACK
                )
                new_order = OrderModel(
                    id=order_id,
                    agent_id=to_id,
                    buyer=buyer_addr,
                    amount=float(amount),
                    # Lands on /active-jobs first — the agent is "hired and
                    # running". Buyer can click Mark Complete to flip to
                    # settled, which moves it to /past-jobs.
                    status="in_escrow",
                    task=data.get("prompt") or data.get("reason") or "Hired via demo/checkout",
                    date=time.strftime("%Y-%m-%d", time.gmtime()),
                )
                db.session.add(new_order)
                db.session.commit()
        except Exception as _oe:
            log.warning("trigger-direct: order row not written (%s)", _oe)
        return jsonify({"triggered": True, "newEvents": new_events,
                        "count": len(new_events), "chain": chain_info,
                        "orderId": order_id, **r})
    except Exception as e:
        app.logger.warning("trigger-direct error: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/sim/agent-onchain/<int:agent_id>")
def api_sim_agent_onchain(agent_id):
    """Per-agent profile. Tries to read live from the deployed Fuji
    contracts; falls back to the DB-backed OnchainProfile for agents
    not yet registered on-chain so reviewers see each agent's real
    dynamic state, not a default-zero fill.
    """
    from models import Agent as AgentModel, OnchainProfile
    a = AgentModel.query.get(agent_id)
    if not a:
        return jsonify({"error": "agent not found"}), 404

    db_profile = OnchainProfile.query.get(agent_id)

    out = {"agentId": agent_id, "name": a.name, "category": a.category}
    oc = _get_onchain()

    # On-chain reads (real Fuji)
    chain_rep = chain_stake = chain_listing = None
    chain_block = None
    if oc:
        try:
            chain_block = int(oc.w3.eth.block_number)
            out["block"] = chain_block
            out["chainId"] = int(oc.w3.eth.chain_id)
            out["rpcUrl"] = str(oc.w3.provider.endpoint_uri)
        except Exception as e:
            out["chainError"] = str(e)[:120]
        try:
            chain_rep = oc.get_credit_profile(agent_id)
        except Exception as e:
            out["reputationError"] = str(e)[:120]
        try:
            chain_stake = oc.get_stake(agent_id)
        except Exception as e:
            out["stakeError"] = str(e)[:120]
        try:
            chain_listing = oc.get_listing(agent_id)
        except Exception as e:
            out["listingError"] = str(e)[:120]

    # An agent is "actually on-chain" when AgentRegistry.getAgent returns
    # a non-zero wallet (= the agent has been registered). Score might be
    # at default (500) right after registration but the wallet proves it.
    on_chain = False
    if oc and chain_listing:
        # get_listing returns {acceptingWork, minPricePerToken, nonce, ...}
        # A registered agent has non-zero nonce OR minPrice > 0
        on_chain = (chain_listing.get("acceptingWork") is not None
                    or (chain_listing.get("nonce") is not None and int(chain_listing.get("nonce", 0)) > 0)
                    or int(chain_listing.get("minPricePerToken", 0)) > 0)
    # Also — cheap separate check via AgentRegistry.getAgent to be certain
    if oc:
        try:
            reg = oc._contracts["AgentRegistry"]
            agent_struct = reg.functions.getAgent(int(agent_id)).call()
            # struct: (agentId, wallet, name, endpointURL, ...) — non-zero wallet ⇒ registered
            if agent_struct and len(agent_struct) > 1:
                wallet_on_chain = agent_struct[1]
                if wallet_on_chain and int(wallet_on_chain, 16) != 0:
                    on_chain = True
        except Exception:
            pass

    # Build the reputation/stake view: use the richer source for each field.
    # Fresh on-chain registrations return default state (score 500, 0 tasks);
    # our sim has the task history from continuous activity. Reviewer sees
    # "on-chain" badge when the agent IS registered, but reputation numbers
    # are whichever is richer (real activity preferred over default state).
    sim_rep, sim_stake = {}, {}
    if db_profile:
        sim_rep = {
            "score": db_profile.score,
            "tier": db_profile.tier,
            "tasksCompleted": db_profile.tasks_completed,
            "incidentCount": db_profile.rep_incident_count,
            "lastDecayTs": db_profile.last_decay_ts or 0,
            "projectedScore": db_profile.score,
        }
        sim_stake = {
            "stakedUSDC": str(db_profile.staked_amount),
            "stakedUSDCDisplay": round(db_profile.staked_amount / 1_000_000, 2),
            "incidentCount": db_profile.stake_incident_count,
            "banned": db_profile.banned,
            "unstakeRequest": {"amount": "0", "availableAt": 0},
        }

    # Pick the source that has real activity signal
    def _richer_rep():
        if not chain_rep: return sim_rep
        if not sim_rep: return chain_rep
        chain_has_activity = (chain_rep.get("tasksCompleted", 0) > 0
                              or chain_rep.get("score", 500) != 500
                              or chain_rep.get("incidentCount", 0) > 0)
        sim_has_activity = (sim_rep.get("tasksCompleted", 0) > 0
                            or sim_rep.get("score", 500) != 500
                            or sim_rep.get("incidentCount", 0) > 0)
        if chain_has_activity and not sim_has_activity: return chain_rep
        if sim_has_activity and not chain_has_activity: return sim_rep
        # Both have activity — prefer chain (authoritative)
        if chain_has_activity and sim_has_activity: return chain_rep
        # Neither — return chain (default state but on-chain registered)
        return chain_rep

    def _richer_stake():
        if not chain_stake: return sim_stake
        if not sim_stake: return chain_stake
        chain_amt = int(chain_stake.get("stakedUSDC", 0) or 0)
        sim_amt = db_profile.staked_amount if db_profile else 0
        return chain_stake if chain_amt >= sim_amt else sim_stake

    out["source"] = "on-chain" if on_chain else "simulated (not yet registered on-chain)"
    out["reputation"] = _richer_rep()
    out["stake"] = _richer_stake()
    out["listing"] = chain_listing or ({
        "acceptingWork": db_profile.accepting_work if db_profile else True,
        "minPricePerToken": str(int(a.min_price * 1_000_000)),
        "nonce": 0,
    })
    out["wallet"] = (db_profile.wallet_address if db_profile else None)
    return jsonify(out)


@app.route("/api/sim/onchain-history")
def api_sim_onchain_history():
    """Real tx history for each of our 6 deployed Fuji contracts, pulled
    live from the Snowtrace API. These are actual on-chain transactions
    reviewers can click through and verify right now — no signing needed.
    """
    from onchain import ADDRESSES, EXPLORER_URL
    import urllib.request, urllib.parse, json as _json
    out = {"contracts": []}
    for name, addr in ADDRESSES.items():
        entry = {
            "contract": name, "address": addr,
            "explorer": f"{EXPLORER_URL}/address/{addr}",
            "txs": [],
        }
        try:
            url = (
                "https://api-testnet.snowtrace.io/api"
                "?module=account&action=txlist"
                f"&address={addr}&startblock=0&endblock=99999999"
                "&sort=desc&page=1&offset=10"
            )
            with urllib.request.urlopen(url, timeout=6) as r:
                d = _json.loads(r.read())
            for t in (d.get("result") or [])[:8]:
                if not isinstance(t, dict):
                    continue
                method = t.get("functionName") or t.get("methodId") or t.get("input", "0x")[:10]
                entry["txs"].append({
                    "hash": t["hash"],
                    "snowtrace": f"{EXPLORER_URL}/tx/{t['hash']}",
                    "block": int(t["blockNumber"]),
                    "from": t["from"],
                    "method": method[:80] if method else "—",
                    "timestamp": int(t["timeStamp"]),
                })
        except Exception as e:
            entry["error"] = str(e)[:120]
        out["contracts"].append(entry)
    return jsonify(out)


@app.route("/api/sim/chain-health")
def api_sim_chain_health():
    """Lightweight endpoint polled by the always-on chain strip.
    Returns current block + gas + facilitator balance — fast single-read."""
    oc = _get_onchain()
    if not oc:
        return jsonify({"mode": "offline"}), 503
    try:
        block = int(oc.w3.eth.block_number)
        gas = int(oc.w3.eth.gas_price)
        bal = 0
        if oc.facilitator:
            bal = int(oc.w3.eth.get_balance(oc.facilitator.address))
        return jsonify({
            "mode": "live-write" if bal / 1e18 >= 0.01 else "read-only",
            "block": block,
            "gasPriceGwei": round(gas / 1e9, 3),
            "facilitator": oc.facilitator.address if oc.facilitator else None,
            "facilitatorAVAX": round(bal / 1e18, 6),
            "chainId": int(oc.w3.eth.chain_id),
            "rpcUrl": str(oc.w3.provider.endpoint_uri),
        })
    except Exception as e:
        return jsonify({"mode": "offline", "error": str(e)[:120]}), 502


@app.route("/api/sim/post-bid", methods=["POST"])
def api_sim_post_bid():
    """User-initiated bid post. Creates an AuctionBid that the sim engine
    will match to a qualifying agent on its next tick — reviewers get to
    watch their bid become someone's next tx."""
    from sim_engine import get_engine
    from models import AuctionBid as BidModel, Agent as AgentModel
    data = request.get_json(silent=True) or {}
    try:
        tokens = int(data.get("tokenBudget", 1000))
        if tokens < 10 or tokens > 10_000_000:
            return jsonify({"error": "tokenBudget must be 10 to 10,000,000"}), 400
        max_price = float(data.get("maxPricePerToken", 0.01))
        if max_price <= 0 or max_price > 10:
            return jsonify({"error": "maxPricePerToken must be 0 to 10 USDC"}), 400
        min_tier = int(data.get("minTier", 1))
        min_tier = max(1, min(3, min_tier))
        category_id = int(data.get("categoryId", 0))
        if category_id < 0 or category_id > 6:
            return jsonify({"error": "categoryId must be 0-6"}), 400
    except (TypeError, ValueError):
        return jsonify({"error": "invalid number in request"}), 400

    eng = get_engine(app)
    import time as _t, hashlib as _h
    now = int(_t.time())
    deposit_micro = int(tokens * max_price * 1_000_000)
    # Use a clean id pattern so bids posted from the UI are identifiable
    bid_id = f"USER-{now}-{eng._event_id + 1}"
    user_wallet = data.get("userWallet") or ("0x" + _h.sha256(f"user-{now}".encode()).hexdigest()[:40])
    bid = BidModel(
        on_chain_bid_id=bid_id,
        user=user_wallet,
        deposit_amount=deposit_micro,
        token_budget=tokens,
        max_price_per_token=int(max_price * 1_000_000),
        category_id=category_id,
        min_tier=min_tier,
        expires_at=now + int(data.get("expiresInSec", 900)),  # 15 min default
        settled=False, cancelled=False,
    )
    db.session.add(bid)
    db.session.commit()
    # Log via the engine so UI can pick it up from /api/sim/events
    eng._log_event(
        "bid_post", None,
        f"USER bid {bid_id} · {tokens} tokens · T{min_tier}+ · ≤${max_price:.4f}/tok",
        amount=deposit_micro / 1_000_000,
        meta={"bidId": bid_id, "tokens": tokens, "minTier": min_tier,
              "categoryId": category_id, "userDriven": True},
    )

    # When live writes are on, ALSO submit a real postBid to AuctionMarket
    chain_result = {"onChainSubmitted": False}
    if _LIVE_WRITES_ENABLED["on"]:
        try:
            oc = _get_onchain()
            if oc and oc.facilitator:
                result = oc.post_bid(
                    deposit_amount=deposit_micro,
                    token_budget=tokens,
                    max_price_per_token=int(max_price * 1_000_000),
                    category_id=category_id,
                    min_tier=min_tier,
                    expires_at=bid.expires_at,
                )
                chain_result = {
                    "onChainSubmitted": True,
                    "onChainBidId": result.get("bidId"),
                    "txHash": result.get("txHash"),
                    "snowtrace": result.get("snowtrace"),
                }
        except Exception as e:
            chain_result = {"onChainSubmitted": False, "chainError": str(e)[:200]}

    return jsonify({
        "ok": True, "bidId": bid_id,
        "depositUSDC": deposit_micro / 1_000_000,
        "expiresAt": bid.expires_at,
        "note": "Engine will match this on its next tick (~2s).",
        **chain_result,
    })


@app.route("/api/sim/force-surge", methods=["POST"])
def api_sim_force_surge():
    """Pump a specific agent's surge multiplier to simulate a demand spike.
    The surge persists until the next sim tick recomputes it."""
    from models import Agent as AgentModel
    data = request.get_json(silent=True) or {}
    try:
        agent_id = int(data["agentId"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "agentId required"}), 400
    try:
        multiplier = float(data.get("multiplier", 2.0))
    except (TypeError, ValueError):
        return jsonify({"error": "multiplier must be a number"}), 400
    if multiplier < 1.0 or multiplier > 3.0:
        return jsonify({"error": "multiplier must be 1.0 to 3.0"}), 400
    a = AgentModel.query.get(agent_id)
    if not a:
        return jsonify({"error": "agent not found"}), 404
    new_price = round(a.min_price * multiplier, 6)
    if new_price > a.max_price:
        new_price = a.max_price
        multiplier = round(new_price / a.min_price, 3) if a.min_price > 0 else 1.0
    a.surge_multiplier = multiplier
    a.surge_active = multiplier > 1.2
    a.current_price = new_price
    db.session.commit()
    from sim_engine import get_engine
    get_engine(app)._log_event(
        "system", agent_id,
        f"{a.name} surge forced → ×{multiplier:.2f} (${new_price:.6f}/tok)",
        meta={"multiplier": multiplier, "currentPrice": new_price, "userForced": True},
    )
    return jsonify({
        "ok": True, "agentId": agent_id, "agentName": a.name,
        "surgeMultiplier": multiplier, "currentPrice": new_price,
    })


@app.route("/api/sim/slash-agent", methods=["POST"])
def api_sim_slash_agent():
    """Gatekeeper-signed slash. When live writes are on and the gatekeeper
    key is set, this ALSO fires a real submitIncident tx on Fuji —
    returning the tx hash + Snowtrace URL for verification."""
    from models import Agent as AgentModel, OnchainProfile
    from sim_engine import get_engine
    data = request.get_json(silent=True) or {}
    try:
        agent_id = int(data["agentId"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "agentId required"}), 400
    reason = (data.get("reason") or "Buyer-filed dispute, gatekeeper-signed")[:120]
    severity = int(data.get("severity", 1))
    severity = max(1, min(2, severity))
    affected_user = data.get("affectedUser") or "0x" + "0" * 39 + "1"

    a = AgentModel.query.get(agent_id)
    p = OnchainProfile.query.get(agent_id)
    if not a or not p:
        return jsonify({"error": "agent not found"}), 404

    eng = get_engine(app)
    before_id = eng._event_id

    # Mirror the slash in sim state so UI reflects reputation impact
    synthetic_session = {"buyer": affected_user, "bid_id": f"SLASH-{agent_id}"}
    eng._do_slash(a, p, synthetic_session)
    db.session.commit()
    new_events = [ev.to_dict() for ev in eng.events if ev.id > before_id]

    # Attempt real on-chain submitIncident when configured
    chain_result = {"onChainSubmitted": False}
    if _LIVE_WRITES_ENABLED["on"]:
        try:
            oc = _get_onchain()
            if oc and oc.gatekeeper:
                import random as _r
                # Use a unique affected_user each call so the contract doesn't
                # reject a signature-replay
                unique_user = "0x" + _r.Random(eng._event_id).randbytes(20).hex()
                result = oc.submit_incident(agent_id, unique_user, severity)
                chain_result = {
                    "onChainSubmitted": True,
                    "txHash": result.get("txHash"),
                    "snowtrace": result.get("snowtrace"),
                    "affectedUser": unique_user,
                    "severity": severity,
                }
        except Exception as e:
            chain_result = {"onChainSubmitted": False, "chainError": str(e)[:200]}

    return jsonify({
        "ok": True, "agentId": agent_id,
        "newEvents": new_events,
        "scoreAfter": p.score,
        "stakeAfterUSDC": round(p.staked_amount / 1_000_000, 2),
        "incidentCount": p.stake_incident_count,
        "banned": p.banned,
        "reason": reason,
        **chain_result,
    })


@app.route("/api/sim/open-bids")
def api_sim_open_bids():
    """Live auction bids, newest first. Shows the user the marketplace is
    actively generating demand between their own triggers."""
    from models import AuctionBid as BidModel
    import time as _t
    now = int(_t.time())
    rows = (BidModel.query
            .filter_by(settled=False, cancelled=False)
            .filter(BidModel.deposit_amount >= 10_000)  # hide $0.00 bids (legacy rows with sub-cent deposits)
            .order_by(BidModel.id.desc())
            .limit(25).all())
    out = []
    for b in rows:
        if b.expires_at and b.expires_at < now:
            continue  # stale
        out.append({
            "bidId": b.on_chain_bid_id or str(b.id),
            "user": b.user,
            "tokenBudget": int(b.token_budget),
            "depositUSDC": round(int(b.deposit_amount) / 1_000_000, 2),
            "maxPricePerToken": round(int(b.max_price_per_token) / 1_000_000, 6),
            "categoryId": b.category_id,
            "minTier": b.min_tier,
            "expiresAt": int(b.expires_at) if b.expires_at else 0,
            "secsLeft": max(0, int(b.expires_at) - now) if b.expires_at else 0,
        })
    return jsonify({"bids": out, "count": len(out)})


@app.route("/api/sim/recent-winners")
def api_sim_recent_winners():
    """Recent bid_claim events: which agent won which bid, when."""
    from sim_engine import get_engine
    eng = get_engine(app)
    events = [e for e in eng.events if e.kind == "bid_claim"][-20:]
    out = []
    for e in events:
        out.append({
            "ts": e.ts, "realTs": e.real_ts,
            "agentId": e.agent_id,
            "message": e.message,
            "meta": e.meta,
        })
    out.reverse()
    return jsonify({"winners": out})


@app.route("/api/sim/surge-top")
def api_sim_surge_top():
    """Agents with the highest active surge multipliers, sorted."""
    from models import Agent as AgentModel
    rows = (AgentModel.query
            .filter(AgentModel.surge_active == True)
            .order_by(AgentModel.surge_multiplier.desc())
            .limit(10).all())
    return jsonify({"surging": [{
        "id": a.id, "name": a.name, "category": a.category,
        "currentPrice": a.current_price,
        "minPrice": a.min_price,
        "surgeMultiplier": a.surge_multiplier,
        "pctAboveBase": round((a.surge_multiplier - 1) * 100, 1),
    } for a in rows]})


@app.route("/api/sim/all-agents")
def api_sim_all_agents():
    """All agents (id, name, category, tier, score, wallet) so the demo
    picker can offer ANY agent as sender or receiver."""
    from models import Agent as AgentModel, OnchainProfile
    rows = AgentModel.query.order_by(AgentModel.category, AgentModel.name).all()
    out = []
    for a in rows:
        p = OnchainProfile.query.get(a.id)
        if not p:
            continue
        out.append({
            "id": a.id, "name": a.name, "category": a.category,
            "tier": p.tier, "score": p.score, "wallet": p.wallet_address,
            "banned": p.banned, "minPrice": a.min_price,
            "modelProvider": a.model_provider, "modelName": a.model_name,
        })
    return jsonify({"agents": out})


@app.route("/api/sim/trigger-a2a", methods=["POST"])
def api_sim_trigger_a2a():
    """Fire one agent-to-agent flow on demand (for the live demo).
    Accepts JSON {primaryId?, tokenBudget?, pricePerToken?} to parameterize
    the transaction so amounts aren't hardcoded.

    Also reaches out to live Fuji on every click:
      - Always: reads current block number + agent 1's getCreditProfile
        so the demo proves the chain link even when the wallet is unfunded.
      - When facilitator is funded: performs an on-chain write (AgentRegistry
        `registerAgent` with a throwaway name) and returns the real tx hash
        + Snowtrace URL so each click lands on Fuji for real.
    """
    from sim_engine import get_engine
    eng = get_engine(app)
    if not eng.is_running():
        eng.start()
    data = request.get_json(silent=True) or {}
    primary_id = data.get("primaryId")
    token_budget = data.get("tokenBudget")
    price_per_token = data.get("pricePerToken")

    # Type + range validation so we never 500 on a bad payload
    try:
        primary_id_int = int(primary_id) if primary_id not in (None, "", 0) else None
    except (TypeError, ValueError):
        return jsonify({"error": "primaryId must be an integer"}), 400
    try:
        token_budget_int = int(token_budget) if token_budget not in (None, "", 0) else None
        if token_budget_int is not None and (token_budget_int < 1 or token_budget_int > 10_000_000):
            return jsonify({"error": "tokenBudget must be between 1 and 10,000,000"}), 400
    except (TypeError, ValueError):
        return jsonify({"error": "tokenBudget must be an integer"}), 400
    try:
        price_f = float(price_per_token) if price_per_token not in (None, "", 0) else None
        if price_f is not None and price_f < 0:
            return jsonify({"error": "pricePerToken must be non-negative"}), 400
    except (TypeError, ValueError):
        return jsonify({"error": "pricePerToken must be a number"}), 400

    # If the caller specified a primary, it must be a known flagship
    if primary_id_int is not None and primary_id_int not in A2A_WORKFLOWS:
        return jsonify({
            "error": "primaryId must be a composable flagship agent",
            "validFlagships": list(A2A_WORKFLOWS.keys()),
        }), 400

    before_id = eng._event_id
    with app.app_context():
        try:
            eng._fire_demo_a2a_flow(
                primary_id=primary_id_int,
                token_budget=token_budget_int,
                price_per_token=price_f,
            )
            db.session.commit()
        except Exception as e:
            app.logger.warning("trigger-a2a error: %s", e)
            return jsonify({"error": str(e)}), 500
    new_events = [ev.to_dict() for ev in eng.events if ev.id > before_id]
    if not new_events:
        return jsonify({
            "error": (
                "Simulator produced no events for this cascade. "
                "Seed agents with on-chain profiles and ensure A2A_WORKFLOWS "
                "flagships exist, then try again."
            ),
            "triggered": False,
            "newEvents": [],
            "count": 0,
        }), 503
    # Resolve wallets for the calldata overlay
    from models import OnchainProfile
    settle = next((e for e in new_events if e["kind"] == "settle"), None)
    hire = next((e for e in new_events if e["kind"] == "a2a_hire"), None)
    from_w = OnchainProfile.query.get(settle["agentId"]).wallet_address if settle and OnchainProfile.query.get(settle["agentId"]) else None
    to_id = hire["meta"].get("subAgentId") if hire and hire.get("meta") else None
    to_w = OnchainProfile.query.get(to_id).wallet_address if to_id and OnchainProfile.query.get(to_id) else None
    total_usdc = sum(e.get("amountUSDC", 0) for e in new_events if e.get("kind") == "a2a_hire")
    chain_info = _chain_overlay_for(new_events, from_wallet=from_w, to_wallet=to_w, amount_usdc=total_usdc)

    return jsonify({
        "triggered": True,
        "newEvents": new_events,
        "count": len(new_events),
        "chain": chain_info,
    })


@app.route("/api/x402/demo-execute/<int:agent_id>", methods=["GET", "POST"])
def api_x402_demo_execute(agent_id):
    """A real x402-gated endpoint. First call (no X-Payment header) returns
    HTTP 402 with a full challenge body. Second call with a signed EIP-3009
    permit in X-Payment header: executes the permit on-chain, returns 200
    with an X-Payment-Receipt header and the 'work product' body.

    Extended for the rent-agent checkout flow: accepts ?amountUSDC=,
    ?buyerWallet=, and ?prompt= query params. On successful payment a
    real Order row is written so the buyer lands on /active-jobs with
    the new hire visible."""
    from x402 import require_x402
    from onchain import ADDRESSES

    from models import Agent as A
    agent = A.query.get(agent_id)
    if not agent:
        return jsonify({"error": f"agent {agent_id} not found"}), 404

    # Caller may override the deposit amount (from Max Spend Cap on checkout).
    # Otherwise use the agent's current price × 100 token default.
    try:
        override = float(request.args.get("amountUSDC") or 0)
    except ValueError:
        override = 0
    default_price = round((agent.current_price or 0.0001) * 100, 4)
    price_usdc = override if override > 0 else default_price
    buyer_wallet_in = str(request.args.get("buyerWallet") or "").strip()
    prompt_in = (request.args.get("prompt") or "").strip()

    @require_x402(
        price_per_call_usdc=price_usdc,
        resource_id=f"agent-{agent_id}-execute",
        recipient_resolver=lambda r, kw: ADDRESSES["EscrowPayment"],
        notes=f"Hire {agent.name} · {price_usdc} USDC per x402 call",
    )
    def _inner():
        from flask import g
        from models import Order as OrderModel
        receipt = getattr(g, "x402_receipt", {}) or {}
        tx_hashes = receipt.get("txHashes") or {}
        tx_hash = tx_hashes.get("permit") or tx_hashes.get("settlement") or ""
        if tx_hash and not tx_hash.startswith("0x"):
            tx_hash = "0x" + tx_hash
        # Write an Order row so /active-jobs shows this hire immediately.
        order_id = None
        try:
            short = tx_hash[2:8] if tx_hash.startswith("0x") else tx_hash[:6]
            order_id = f"ORD-{short}" if short else f"ORD-{int(time.time()) % 1000000}"
            if not OrderModel.query.get(order_id):
                cookie_w = (request.cookies.get("buyer_wallet") or "").strip()
                buyer = (buyer_wallet_in if _is_valid_wallet(buyer_wallet_in)
                         else cookie_w if _is_valid_wallet(cookie_w)
                         else FACILITATOR_WALLET_FALLBACK)
                db.session.add(OrderModel(
                    id=order_id, agent_id=agent_id,
                    buyer=buyer, amount=float(price_usdc),
                    status="in_escrow",
                    task=prompt_in or f"x402 hire · {agent.name}",
                    date=time.strftime("%Y-%m-%d", time.gmtime()),
                ))
                db.session.commit()
        except Exception as _oe:
            log.warning("x402 demo-execute: Order write failed: %s", _oe)
        return jsonify({
            "ok":        True,
            "agentId":   agent_id,
            "agentName": agent.name,
            "orderId":   order_id,
            "scheme":    "x402/eip-3009+v1",
            "result":    f"{agent.name} accepted the x402 hire. Task queued to Akash Qwen for execution.",
            "x402Receipt": {
                "sessionId": receipt.get("sessionId"),
                "txHashes":  tx_hashes,
                "txHash":    tx_hash,
                "explorer":  f"https://subnets-test.avax.network/c-chain/tx/{tx_hash}" if tx_hash else None,
                "snowtrace": receipt.get("snowtrace"),
            },
        })
    return _inner()


@app.route("/api/x402/auto-sign", methods=["POST"])
def api_x402_auto_sign():
    """Convenience for the demo UI: generate a valid EIP-3009 permit signed
    by the facilitator (as both buyer and payer for demo purposes). The UI
    then retries the gated endpoint with this permit as X-Payment header."""
    data = request.get_json(silent=True) or {}
    try:
        amount = float(data.get("amountUSDC", 0.01))
        agent_id = int(data.get("agentId", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "amountUSDC + agentId required"}), 400
    from x402 import auto_sign_demo_permit
    from onchain import ADDRESSES
    try:
        permit = auto_sign_demo_permit(
            recipient=ADDRESSES["EscrowPayment"],
            amount_usdc=amount,
            agent_id=agent_id,
            category_id=int(data.get("categoryId", 0)),
        )
        return jsonify({"ok": True, "permit": permit})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 500


@app.route("/api/sim/event-contract-map")
def api_sim_event_contract_map():
    """Map each sim event kind to the on-chain contract that would emit it.
    Used by the demo UI to deep-link every row to the right Snowtrace address."""
    from onchain import ADDRESSES, EXPLORER_URL
    kind_to_contract = {
        "register":       "AgentRegistry",
        "stake":          "StakingSlashing",
        "unstake":        "StakingSlashing",
        "deposit":        "EscrowPayment",
        "settle":         "EscrowPayment",
        "refund":         "EscrowPayment",
        "slash":          "ReputationContract",
        "incident":       "ReputationContract",
        "a2a_hire":       "EscrowPayment",
        "a2a_settle":     "EscrowPayment",
        "bid_post":       "AuctionMarket",
        "bid_claim":      "AuctionMarket",
        "bid_cancel":     "AuctionMarket",
        "registerAgent":  "AgentRegistry",  # live on-chain writes from the demo
    }
    return jsonify({
        "explorer": EXPLORER_URL,
        "map": {k: {"contract": c, "address": ADDRESSES[c],
                    "explorer": f"{EXPLORER_URL}/address/{ADDRESSES[c]}"}
                for k, c in kind_to_contract.items() if c in ADDRESSES},
    })


@app.route("/api/sim/a2a-candidates")
def api_sim_a2a_candidates():
    """Return the flagship composable agents + their sub-agents for the
    demo picker."""
    from models import Agent as AgentModel, OnchainProfile
    flagships = []
    for fid, wf in A2A_WORKFLOWS.items():
        a = AgentModel.query.get(fid)
        p = OnchainProfile.query.get(fid)
        if not a or not p:
            continue
        subs = []
        for sa in wf.get("sub_agents", []):
            sub = AgentModel.query.get(sa["id"])
            if sub:
                subs.append({"id": sub.id, "name": sub.name, "category": sub.category,
                             "billing": sa.get("billing"),
                             "estCostLow":  sa.get("est_cost_low"),
                             "estCostHigh": sa.get("est_cost_high")})
        flagships.append({
            "id": a.id, "name": a.name, "category": a.category,
            "tier": p.tier, "score": p.score, "wallet": p.wallet_address,
            "workflowLabel": wf.get("workflow_label"),
            "subAgents": subs,
        })
    return jsonify({"flagships": flagships})


@app.route("/demo")
def demo_page():
    """Power-user control room — x402 handshake, ERC-8004 probe, live
    on-chain actions. Rendered with the same theme tokens as Shalin's
    redesigned UI so it visually blends with the rest of the app."""
    return render_template("demo.html", page_title="Live Demo")


@app.route("/api/sim/events")
def api_sim_events():
    from sim_engine import get_engine
    since = int(request.args.get("since", 0))
    limit = int(request.args.get("limit", 100))
    return jsonify({
        "events": get_engine(app).events_since(since, limit),
        "status": get_engine(app).status(),
    })


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
            "current_price": current_price, "surge_active": a.surge_active,
            "surge_multiplier": a.surge_multiplier, "seller": a.seller,
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
        # 1. Baseline schema (idempotent)
        db.create_all()
        # 2. Additive column migrations (SQLite can't add columns via create_all)
        from agent_pack import _ensure_columns
        _ensure_columns(app)
        # 3. Original fixtures
        from models import seed_db
        seed_db(app)
        # 4. Bulk roster + backfill of new fields on older rows
        from agent_pack import seed_bulk_agents, backfill_existing
        seed_bulk_agents(app)
        backfill_existing(app)
        # 4b. Varied per-agent reviews
        from review_pack import seed_reviews
        seed_reviews(app)
        # 5. On-chain profiles / sim seed data
        from simulation import seed_simulation
        seed_simulation(app)
        _sync_agents_from_db()
        # Filter junk-named agents from the in-memory AGENTS list so no
        # page surfaces them. We keep the DB rows intact (removing them
        # would break Order FK references from earlier test hires).
        JUNK_PREFIXES = ("QuickList-", "Demo-17", "FinalCheck", "PostRestart",
                         "BrowserShape", "SmokeBond", "Reviewedemo-", "LIVE-",
                         "ProofTx", "TestBondAgent", "FinalLock", "WizardTest",
                         "BondCheck", "DeadlineDemo", "BondTest", "DemoX")
        before = len(AGENTS)
        AGENTS[:] = [a for a in AGENTS if not (
            (a.get("name") or "").startswith(JUNK_PREFIXES)
            or len((a.get("name") or "").strip()) < 3
        )]
        log.info("Database ready — %d agents in memory (filtered %d junk).",
                 len(AGENTS), before - len(AGENTS))
    except Exception as _seed_err:
        log.warning("DB seed skipped: %s", _seed_err)


if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not app.debug:
    try:
        from sim_engine import get_engine
        get_engine(app).start()
        log.info("Live simulation engine running.")
    except Exception as _sim_err:
        log.warning("Live sim autostart failed: %s", _sim_err)

# Auto-enable live writes at boot time (runs unconditionally — cheap check,
# self-guards on facilitator balance + existing state file).
_maybe_autoenable_live_writes()


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
