"""
config.py - Centralized configuration for AgentHire.

Usage:
    from config import config
    app.config.from_object(config["development"])

Environment variables:
    FLASK_ENV             development | production (default: development)
    SECRET_KEY            Flask secret key (required in production)
    DATABASE_URL          SQLAlchemy DB URI (default: sqlite:///agenthire.db)
    API_KEY               API key for protected admin/seller mutation routes
    CORS_ORIGINS          Comma-separated allowed origins (default: *)
    RATELIMIT_DEFAULT     Default rate limit string (default: 60/minute)
    FACILITATOR_URL       Optional: Node facilitator service URL
    FACILITATOR_PRIVATE_KEY  Optional: private key for on-chain x402 flow
    GATEKEEPER_PRIVATE_KEY   Optional: private key for dispute signing
    RPC_URL               Optional: Avalanche RPC URL (default: Fuji)
"""
from __future__ import annotations
import os
from typing import Optional


class Config:
    """Base configuration - shared across all environments."""

    # ── Flask ──────────────────────────────────────────────────────────────
    SECRET_KEY: str = os.environ.get("SECRET_KEY", "dev-secret-change-in-production")
    DEBUG: bool = False
    TESTING: bool = False
    ENV_NAME: str = os.environ.get("FLASK_ENV", "development")

    # ── Database ───────────────────────────────────────────────────────────
    SQLALCHEMY_DATABASE_URI: str = os.environ.get(
        "DATABASE_URL", "sqlite:///agenthire.db"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS: bool = False
    SQLALCHEMY_ECHO: bool = False  # Set True to log all SQL in development

    # ── CORS ───────────────────────────────────────────────────────────────
    CORS_ORIGINS: list = [
        o.strip()
        for o in os.environ.get("CORS_ORIGINS", "*").split(",")
        if o.strip()
    ]

    # ── Rate limiting ──────────────────────────────────────────────────────
    # Demo mode runs lots of client-side polling (ticker, sim status, agent
    # metadata); 120/min hits the wall fast. 600/min = 10/sec still protects
    # against abuse while letting the demo breathe.
    RATELIMIT_DEFAULT: str = os.environ.get("RATELIMIT_DEFAULT", "600/minute")
    RATELIMIT_STORAGE_URI: str = "memory://"  # swap to redis:// in production
    RATELIMIT_HEADERS_ENABLED: bool = True

    # ── Auth ───────────────────────────────────────────────────────────────
    API_KEY: Optional[str] = os.environ.get("API_KEY")  # None → auth disabled (local dev)

    # ── On-chain ────────────────────────────────────────────────────────────
    FACILITATOR_URL: Optional[str] = os.environ.get("FACILITATOR_URL")
    FACILITATOR_PRIVATE_KEY: Optional[str] = os.environ.get("FACILITATOR_PRIVATE_KEY")
    GATEKEEPER_PRIVATE_KEY: Optional[str] = os.environ.get("GATEKEEPER_PRIVATE_KEY")
    RPC_URL: str = os.environ.get("RPC_URL", "https://api.avax-test.network/ext/C/rpc")

    # ── Runtime behavior controls ──────────────────────────────────────────
    # Keep development convenient while requiring explicit opt-in in production.
    AUTO_SEED_DATA: bool = os.environ.get("AUTO_SEED_DATA", "1").strip().lower() in {"1", "true", "yes", "on"}
    ENABLE_SIM_ENGINE: bool = os.environ.get("ENABLE_SIM_ENGINE", "1").strip().lower() in {"1", "true", "yes", "on"}
    STRICT_PROD_VALIDATION: bool = os.environ.get("STRICT_PROD_VALIDATION", "1").strip().lower() in {"1", "true", "yes", "on"}


class DevelopmentConfig(Config):
    """Development - verbose, SQLite, no strict auth."""
    DEBUG = True
    SQLALCHEMY_ECHO = False  # flip to True to debug queries
    AUTO_SEED_DATA = True
    ENABLE_SIM_ENGINE = True


class TestingConfig(Config):
    """Testing - in-memory SQLite, no rate limiting."""
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    RATELIMIT_ENABLED = False
    AUTO_SEED_DATA = True
    ENABLE_SIM_ENGINE = False


class ProductionConfig(Config):
    """Production - strict, no debug, env-driven secrets."""
    DEBUG = False
    AUTO_SEED_DATA = os.environ.get("AUTO_SEED_DATA", "0").strip().lower() in {"1", "true", "yes", "on"}
    ENABLE_SIM_ENGINE = os.environ.get("ENABLE_SIM_ENGINE", "0").strip().lower() in {"1", "true", "yes", "on"}

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
        log = logging.getLogger("agenthire.config")
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
