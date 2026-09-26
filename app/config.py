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

Chain settings (RPC_URL, CHAIN_ID, EXPLORER_URL, contract addresses, signer
keys) live in chain/config.py and chain/client.py and default to Ethereum
Sepolia.
"""
from __future__ import annotations
import os
from typing import Optional


def _flag(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


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


class Config:
    """Base configuration - shared across all environments."""

    # ── Flask ──────────────────────────────────────────────────────────────
    SECRET_KEY: str = os.environ.get("SECRET_KEY", "dev-secret-change-in-production")
    DEBUG: bool = False
    TESTING: bool = False
    ENV_NAME: str = os.environ.get("FLASK_ENV", "development")

    # ── Database ───────────────────────────────────────────────────────────
    SQLALCHEMY_DATABASE_URI: str = _database_url()
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
    RATELIMIT_STORAGE_URI: str = "memory://"  # swap to redis:// in production
    RATELIMIT_HEADERS_ENABLED: bool = True

    # ── Auth ───────────────────────────────────────────────────────────────
    API_KEY: Optional[str] = os.environ.get("API_KEY")  # None → auth disabled (local dev)

    # ── Names sidecar (ens/) ───────────────────────────────────────────────
    # Unset URL/token → names stay "pending" and can be retried later.
    ENS_SIDECAR_URL: str = os.environ.get("ENS_SIDECAR_URL", "")
    ENS_SIDECAR_TOKEN: str = os.environ.get("ENS_SIDECAR_TOKEN", "")
    # A live child name can require several Sepolia confirmations. Keep this
    # above the normal RPC timeout so the app does not mark an idempotent
    # sidecar write failed while the sidecar is still completing it.
    ENS_SIDECAR_TIMEOUT: float = float(os.environ.get("ENS_SIDECAR_TIMEOUT", "120") or 120)
    ENS_ROOT_NAME: str = os.environ.get("ENS_ROOT_NAME", "") or "agentslist-app.eth"
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
    AUTO_MIGRATE: bool = _flag("AUTO_MIGRATE", "1")
    STRICT_PROD_VALIDATION: bool = _flag("STRICT_PROD_VALIDATION", "1")


class DevelopmentConfig(Config):
    """Development - verbose, SQLite, no strict auth."""
    DEBUG = True
    SQLALCHEMY_ECHO = False  # flip to True to debug queries


class TestingConfig(Config):
    """Testing - in-memory SQLite, no rate limiting."""
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    RATELIMIT_ENABLED = False
    AUTO_MIGRATE = False  # tests build the schema with create_all()
    ENS_SIDECAR_URL = ""  # tests install a fake names client
    ENS_RESOLVE_PAYEES = False  # tests install a fake resolver


class ProductionConfig(Config):
    """Production - strict, no debug, env-driven secrets."""
    DEBUG = False
    AUTO_MIGRATE = _flag("AUTO_MIGRATE", "0")

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


# Map FLASK_ENV → config class
config: dict = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig,
    "default": DevelopmentConfig,
}
