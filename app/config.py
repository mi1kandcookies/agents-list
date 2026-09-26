"""
config.py - Centralized configuration for Agent's List.

Usage:
    from app.config import config
    app.config.from_object(config["development"])

Environment variables:
    FLASK_ENV             development | production (default: development)
    SECRET_KEY            Flask secret key (required in production)
    DATABASE_URL          SQLAlchemy DB URI (default: sqlite:///agents_list.db)
    API_KEY               API key for protected admin/seller mutation routes
    CORS_ORIGINS          Comma-separated allowed origins (default: *)
    RATELIMIT_DEFAULT     Default rate limit string (default: 600/minute)
    AUTO_MIGRATE          Run Alembic upgrade at boot (default on outside production)
    TRUST_PROXY           Trust one proxy's X-Forwarded-For/-Proto/-Host (default on
                          when VERCEL is set, else off)
    SESSION_COOKIE_SECURE Send the session cookie over HTTPS only (default on in
                          production)
    RATELIMIT_STORAGE_URI Flask-Limiter storage (default memory://, per process)

On Vercel (the platform sets VERCEL=1) the default config is production, SQLite
is refused and MANDATE_SIGNING_KEY is required; see docs/deploy/vercel.md.

Chain settings (RPC_URL, CHAIN_ID, EXPLORER_URL, contract addresses, signer
keys) live in chain/config.py and chain/client.py and default to Ethereum
Sepolia.
"""
from __future__ import annotations
import os
from typing import Optional


def _flag(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def on_vercel() -> bool:
    """True inside a Vercel build or function (the platform sets VERCEL=1)."""
    return bool(os.environ.get("VERCEL", "").strip())


def default_config_name() -> str:
    """FLASK_ENV, else production on Vercel, else development."""
    return os.environ.get("FLASK_ENV", "").strip() or ("production" if on_vercel() else "development")


def _database_url() -> str:
    """DATABASE_URL, defaulting to SQLite in the instance folder.

    Bare postgres:// and postgresql:// URLs are pinned to the psycopg (v3)
    driver so the result does not depend on SQLAlchemy's default dialect.
    """
    url = os.environ.get("DATABASE_URL", "").strip() or "sqlite:///agents_list.db"
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            url = "postgresql+psycopg://" + url[len(prefix):]
    return url


def _engine_options(url: str) -> dict:
    """Engine options for Postgres on serverless: drop dead pooled connections
    after a frozen instance resumes, and skip server-side prepared statements
    so a transaction-mode pooler (PgBouncer and most hosted poolers) works."""
    if not (on_vercel() and url.startswith("postgresql")):
        return {}
    return {"pool_pre_ping": True, "pool_recycle": 300,
            "connect_args": {"prepare_threshold": None}}


class Config:
    """Base configuration - shared across all environments."""

    # ── Flask ──────────────────────────────────────────────────────────────
    SECRET_KEY: str = os.environ.get("SECRET_KEY", "dev-secret-change-in-production")
    DEBUG: bool = False
    TESTING: bool = False
    ENV_NAME: str = os.environ.get("FLASK_ENV", "development")

    # ── Session cookie ─────────────────────────────────────────────────────
    # Lax keeps the cookie on the identity provider's top-level GET redirect to
    # /auth/world/callback (the provider only supports response_mode=query).
    SESSION_COOKIE_HTTPONLY: bool = True
    SESSION_COOKIE_SAMESITE: str = "Lax"
    SESSION_COOKIE_SECURE: bool = _flag("SESSION_COOKIE_SECURE", "0")

    # ── Reverse proxy ──────────────────────────────────────────────────────
    # Behind one proxy (Vercel, a load balancer) that sets X-Forwarded-*.
    # Never enable it when clients can reach the app directly: the headers
    # would then be client-controlled.
    TRUST_PROXY: bool = _flag("TRUST_PROXY", "1" if on_vercel() else "0")

    # ── Database ───────────────────────────────────────────────────────────
    SQLALCHEMY_DATABASE_URI: str = _database_url()
    SQLALCHEMY_ENGINE_OPTIONS: dict = _engine_options(SQLALCHEMY_DATABASE_URI)
    SQLALCHEMY_TRACK_MODIFICATIONS: bool = False
    SQLALCHEMY_ECHO: bool = False  # Set True to log all SQL in development

    # ── CORS ───────────────────────────────────────────────────────────────
    CORS_ORIGINS: list = [
        o.strip()
        for o in os.environ.get("CORS_ORIGINS", "*").split(",")
        if o.strip()
    ]

    # ── Rate limiting ──────────────────────────────────────────────────────
    RATELIMIT_DEFAULT: str = os.environ.get("RATELIMIT_DEFAULT", "600/minute")
    # memory:// counts per process; serverless instances each keep their own.
    RATELIMIT_STORAGE_URI: str = os.environ.get("RATELIMIT_STORAGE_URI", "") or "memory://"
    RATELIMIT_HEADERS_ENABLED: bool = True

    # ── Auth ───────────────────────────────────────────────────────────────
    API_KEY: Optional[str] = os.environ.get("API_KEY")  # None → auth disabled (local dev)

    # ── Names sidecar (ens/) ───────────────────────────────────────────────
    # Unset URL/token → names stay "pending" and can be retried later.
    ENS_SIDECAR_URL: str = os.environ.get("ENS_SIDECAR_URL", "")
    ENS_SIDECAR_TOKEN: str = os.environ.get("ENS_SIDECAR_TOKEN", "")
    ENS_SIDECAR_TIMEOUT: float = float(os.environ.get("ENS_SIDECAR_TIMEOUT", "15") or 15)
    ENS_ROOT_NAME: str = os.environ.get("ENS_ROOT_NAME", "") or "agentslist-app.eth"
    # Public https origin of this app. When set, agent names publish their
    # listing page as the ENSIP-26 web endpoint.
    PUBLIC_BASE_URL: str = os.environ.get("PUBLIC_BASE_URL", "")
    # Payee resolution (app/names/service.py resolve_payee): read the agent's
    # x402 payout record through the Universal Resolver and fail closed when it
    # disagrees with the profile. Off → payees come from the profile. When on,
    # an active ENS name and a non-empty x402-payto record are required.
    ENS_RESOLVE_PAYEES: bool = _flag("ENS_RESOLVE_PAYEES", "0")
    ENS_UNIVERSAL_RESOLVER: str = os.environ.get("ENS_UNIVERSAL_RESOLVER", "")

    # ── Agent tasks paid over x402 v2 (app/api/tasks.py) ──────────────────
    AGENT_TASK_PRICE_USDC: str = os.environ.get("AGENT_TASK_PRICE_USDC", "") or "0.05"
    X402_MAX_PAYMENT_USDC: str = os.environ.get("X402_MAX_PAYMENT_USDC", "") or "10"

    # ── Runtime behavior controls ──────────────────────────────────────────
    # AUTO_MIGRATE: run `alembic upgrade head` at boot. On by default for local
    # development so a fresh checkout just works; production runs
    # `flask --app wsgi db upgrade` explicitly (see docker-compose.yml).
    # Never by default on Vercel: every cold start would race to migrate.
    AUTO_MIGRATE: bool = _flag("AUTO_MIGRATE", "0" if on_vercel() else "1")
    STRICT_PROD_VALIDATION: bool = _flag("STRICT_PROD_VALIDATION", "1")


class DevelopmentConfig(Config):
    """Development - verbose, SQLite, no strict auth."""
    DEBUG = True
    SQLALCHEMY_ECHO = False  # flip to True to debug queries


class TestingConfig(Config):
    """Testing - in-memory SQLite, no rate limiting."""
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS: dict = {}
    RATELIMIT_ENABLED = False
    AUTO_MIGRATE = False  # tests build the schema with create_all()
    ENS_SIDECAR_URL = ""  # tests install a fake names client
    ENS_RESOLVE_PAYEES = False  # tests install a fake resolver


class ProductionConfig(Config):
    """Production - strict, no debug, env-driven secrets."""
    DEBUG = False
    ENV_NAME = "production"
    AUTO_MIGRATE = _flag("AUTO_MIGRATE", "0")
    SESSION_COOKIE_SECURE = _flag("SESSION_COOKIE_SECURE", "1")
    PREFERRED_URL_SCHEME = "https"

    @classmethod
    def init_app(cls, app):
        # Warn if secret key is still the default
        if cls.SECRET_KEY == "dev-secret-change-in-production":
            import warnings
            warnings.warn(
                "SECRET_KEY is set to the default value. "
                "Set the SECRET_KEY environment variable in production.",
                stacklevel=2,
            )


def validate_runtime_config(app) -> None:
    """
    Fail fast in production when critical config is unsafe.
    """
    if on_vercel() and not app.testing:
        validate_serverless_config(app)
    env = (app.config.get("ENV_NAME") or os.environ.get("FLASK_ENV", "development")).lower()
    if env != "production":
        return

    errors = []
    warnings = []
    strict = bool(app.config.get("STRICT_PROD_VALIDATION", True))
    secret_key = str(app.config.get("SECRET_KEY") or "")
    if not secret_key or secret_key == "dev-secret-change-in-production" or secret_key == "change-me":
        errors.append("SECRET_KEY must be set to a strong non-default value in production.")

    db_uri = str(app.config.get("SQLALCHEMY_DATABASE_URI") or "")
    if not db_uri:
        errors.append("DATABASE_URL / SQLALCHEMY_DATABASE_URI must be set in production.")
    elif db_uri.startswith("sqlite:"):
        if strict:
            errors.append("SQLite is not allowed in production when STRICT_PROD_VALIDATION=1. Use Postgres/MySQL.")
        else:
            warnings.append("SQLite is configured in production; use Postgres/MySQL for reliability.")

    if not (app.config.get("API_KEY") or os.environ.get("API_KEY")):
        if strict:
            errors.append("API_KEY must be set in production when STRICT_PROD_VALIDATION=1.")
        else:
            warnings.append("API_KEY is not set; protected mutation endpoints may be open in production.")

    cors_origins = app.config.get("CORS_ORIGINS", [])
    if cors_origins == ["*"] or cors_origins == "*":
        if strict:
            errors.append("CORS_ORIGINS wildcard is not allowed in production when STRICT_PROD_VALIDATION=1.")
        else:
            warnings.append("CORS_ORIGINS is wildcard in production; restrict to trusted domains.")

    if warnings:
        import logging
        log = logging.getLogger("agents_list.config")
        for msg in warnings:
            log.warning("config warning: %s", msg)

    if errors:
        raise RuntimeError("Production configuration invalid: " + " ".join(errors))


def validate_serverless_config(app) -> None:
    """Refuse settings that cannot work on stateless serverless instances,
    whatever FLASK_ENV says: a SQLite file is per instance (and read-only),
    and a mandate key generated into instance/ would differ per instance."""
    errors = []
    db_uri = str(app.config.get("SQLALCHEMY_DATABASE_URI") or "")
    if not db_uri or db_uri.startswith("sqlite:"):
        errors.append("DATABASE_URL must point at Postgres on Vercel (SQLite is per instance "
                      "and not persisted); see docs/deploy/vercel.md.")
    if not (app.config.get("MANDATE_SIGNING_KEY") or os.environ.get("MANDATE_SIGNING_KEY")):
        errors.append("MANDATE_SIGNING_KEY must be set on Vercel (a generated key would "
                      "differ per instance).")
    if errors:
        raise RuntimeError("Serverless configuration invalid: " + " ".join(errors))


# Map FLASK_ENV → config class
config: dict = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig,
    "default": DevelopmentConfig,
}
